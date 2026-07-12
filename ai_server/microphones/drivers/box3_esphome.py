from __future__ import annotations

import asyncio
import array
from contextlib import suppress
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import logging
import queue
import socket
import threading
import time
import uuid
from typing import Any

from ai_server.config import MicrophoneConfig
from ai_server.microphones.messages import AudioChunk, AudioProgress, Close, CueFinished, CueType
from ai_server.microphones.messages import ListeningMode, ListeningStarted, ListeningStopped, MicrophoneCommand
from ai_server.microphones.messages import MicrophoneEvent, PlaybackBegin, PlaybackChunk, PlaybackEnd, PlaybackFinished
from ai_server.microphones.messages import PlayCue, ResetWakeCandidate, SetVisualState, SpeechEnded, SpeechStarted
from ai_server.microphones.messages import StartListening, StopListening, VisualState
from ai_server.microphones.interfaces import MicrophoneUnavailable
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget

try:
    import aioesphomeapi
    import aioesphomeapi.host_resolver
except ModuleNotFoundError:
    aioesphomeapi = None


API_PORT = 6053
STREAM_PATH = "/tts.wav"
WAV_HEADER_BYTES = 44
STREAM_REQUEST_TIMEOUT_SECONDS = 5.0
STREAM_DONE_TIMEOUT_SECONDS = 30.0
API_SERVICE_TIMEOUT_SECONDS = 5.0
PLAYBACK_DRAIN_GRACE_SECONDS = 0.2
SPEECH_PEAK_THRESHOLD = 500
SPEECH_START_SECONDS = 0.2
VOICE_ASSISTANT_REARM_DELAY_SECONDS = 0.2
PLAY_MESSAGE_END_CUE_SERVICE = "play_message_end_cue"
PLAY_CONVERSATION_TIMEOUT_CUE_SERVICE = "play_conversation_timeout_cue"
START_FOLLOW_UP_LISTENING_SERVICE = "start_follow_up_listening"
START_OPEN_MIC_LISTENING_SERVICE = "start_open_mic_listening"
RESET_OPEN_MIC_WAKE_CANDIDATE_SERVICE = "reset_open_mic_wake_candidate"
VISUAL_STATE_SERVICES = {
    VisualState.IDLE: "set_visual_idle",
    VisualState.LISTENING: "set_visual_listening",
    VisualState.PROCESSING: "set_visual_processing",
}
FOLLOW_UP_WAKE_WORD = "follow_up"
OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL = 50


class Box3EsphomeMicrophone:
    def __init__(
        self,
        context: MicrophoneContext,
        playback_target: PlaybackTarget,
        initial_silence_seconds: float,
        end_silence_seconds: float,
        speech_peak_threshold: int,
        post_speech_ignore_seconds: float,
        client_info: str = "ai-server-esphome-satellite",
    ) -> None:
        self.context = context
        self.playback_target = playback_target
        self._logger = logging.getLogger(f"{__name__}.Box3EsphomeMicrophone[{context.instance_id}]")
        self._initial_silence_seconds = initial_silence_seconds
        self._end_silence_seconds = end_silence_seconds
        self._speech_peak_threshold = speech_peak_threshold
        self._post_speech_ignore_seconds = post_speech_ignore_seconds
        self._client_info = client_info
        self._client = None
        self._unsubscribe = None
        self._stream_started = asyncio.Event()
        self._stream_done = asyncio.Event()
        self._events: asyncio.Queue[MicrophoneEvent] = asyncio.Queue()
        self._chunks: list[bytes] = []
        self._pending_audio_chunks: list[bytes] = []
        self._wake_word: str | None = None
        self._byte_count = 0
        self._audio_chunk_count = 0
        self._last_audio_progress_chunk_count = 0
        self._speech_started = False
        self._speech_candidate_started_at: float | None = None
        self._last_speech_at: float | None = None
        self._stream_started_at: float | None = None
        self._ignore_audio_until: float | None = None
        self._ignored_audio_chunk_count = 0
        self._ignored_audio_byte_count = 0
        self._speech_done = asyncio.Event()
        self._audio_ended = False
        self._late_audio_chunk_count = 0
        self._run_end_sent = False
        self._voice_assistant_run_active = False
        self._listen_id: str | None = None
        self._utterance_id: str | None = None
        self._listening_mode: ListeningMode | None = None
        self._playback_id: str | None = None
        self._playback_stream: Box3PlaybackStream | None = None

    @classmethod
    def from_config(cls, config: MicrophoneConfig) -> Box3EsphomeMicrophone:
        expected_name = config.options.get("expected_name")
        if expected_name is not None and not isinstance(expected_name, str):
            raise ValueError(f"microphone {config.name} expected_name must be a string when provided")

        context = MicrophoneContext(type=config.type, name=config.name, area=config.area)
        playback_target = PlaybackTarget(
            type=config.type,
            name=config.name,
            address=config.options["address"],
            api_key=config.options["api_key"],
            expected_name=expected_name,
        )
        return cls(
            context=context,
            playback_target=playback_target,
            initial_silence_seconds=config.initial_silence_seconds,
            end_silence_seconds=config.end_silence_seconds,
            speech_peak_threshold=config.speech_peak_threshold,
            post_speech_ignore_seconds=config.post_speech_ignore_seconds,
        )

    async def wait_for_event(self) -> MicrophoneEvent:
        await self._ensure_connected()
        self._logger.debug(
            "waiting for ESPHome voice assistant event state=%s",
            self._voice_assistant_state(),
        )
        return await self._events.get()

    async def send_output_event(self, event: MicrophoneCommand) -> None:
        if isinstance(event, PlaybackBegin):
            self._logger.debug(
                "sending audio start event to ESPHome satellite rate=%s width=%s channels=%s volume=%s state=%s",
                event.rate,
                event.width,
                event.channels,
                event.volume,
                self._voice_assistant_state(),
            )
            await self._start_playback(event)
            return
        if isinstance(event, PlaybackChunk):
            assert event.playback_id == self._playback_id
            if self._playback_stream is None:
                raise RuntimeError("cannot send audio chunk before audio start")
            self._playback_stream.write(event.data)
            return
        if isinstance(event, PlaybackEnd):
            assert event.playback_id == self._playback_id
            self._logger.debug(
                "sending audio end event to ESPHome satellite state=%s",
                self._voice_assistant_state(),
            )
            await self._finish_playback()
            self._events.put_nowait(PlaybackFinished(event.playback_id))
            self._playback_id = None
            return
        if isinstance(event, StartListening):
            assert self._listen_id is None
            self._listen_id = event.listen_id
            self._listening_mode = event.mode
            if event.mode is ListeningMode.WAKE_WORD:
                await self._start_wake_word_listening()
            else:
                await self._finish_voice_assistant_run()
                self._drain_queued_events(f"{event.mode.value} arm")
                service = (
                    START_OPEN_MIC_LISTENING_SERVICE
                    if event.mode is ListeningMode.OPEN_MIC
                    else START_FOLLOW_UP_LISTENING_SERVICE
                )
                await self._execute_api_service(service)
            self._events.put_nowait(ListeningStarted(event.listen_id, event.mode))
            return
        if isinstance(event, StopListening):
            assert event.listen_id == self._listen_id
            await self._finish_voice_assistant_run()
            self._listen_id = None
            self._listening_mode = None
            self._utterance_id = None
            self._events.put_nowait(ListeningStopped(event.listen_id, event.reason))
            return
        if isinstance(event, PlayCue):
            service = (
                PLAY_CONVERSATION_TIMEOUT_CUE_SERVICE
                if event.cue_type is CueType.FOLLOW_UP_TIMEOUT
                else PLAY_MESSAGE_END_CUE_SERVICE
            )
            await self._execute_api_service(service)
            self._events.put_nowait(CueFinished(event.cue_id))
            return
        if isinstance(event, ResetWakeCandidate):
            assert event.listen_id == self._listen_id
            self._logger.debug(
                "requesting ESPHome satellite open-mic wake-candidate reset state=%s",
                self._voice_assistant_state(),
            )
            await self._execute_optional_api_service(RESET_OPEN_MIC_WAKE_CANDIDATE_SERVICE)
            return
        if isinstance(event, SetVisualState):
            self._logger.debug("setting visual state state=%s", event.state.value)
            await self._execute_api_service(VISUAL_STATE_SERVICES[event.state])
            return
        if isinstance(event, Close):
            await self.close()
            return
        raise ValueError(f"unsupported microphone output event: {type(event).__name__}")

    async def close(self) -> None:
        self._logger.debug("closing ESPHome microphone state=%s", self._voice_assistant_state())
        await self._finish_playback()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
        self._listen_id = None
        self._utterance_id = None
        self._listening_mode = None
        self._playback_id = None
        self._drain_queued_events("driver close")

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            self._logger.debug("reusing ESPHome satellite connection state=%s", self._voice_assistant_state())
            return

        _require_aioesphomeapi()

        self._client = aioesphomeapi.APIClient(
            self.playback_target.address,
            API_PORT,
            password=None,
            client_info=self._client_info,
            noise_psk=self.playback_target.api_key,
            expected_name=self.playback_target.expected_name,
        )
        self._logger.debug("connecting to ESPHome satellite address=%s", self.playback_target.address)
        try:
            await self._client.connect(login=True)
        except _expected_unavailable_errors() as error:
            if self._playback_stream is not None:
                self._playback_stream.close()
                self._playback_stream = None
            await self._mark_disconnected()
            raise MicrophoneUnavailable(f"address={self.playback_target.address} error={error}") from error
        connected_address = self._client.connected_address
        if isinstance(connected_address, str) and _is_ipv4_address(connected_address):
            self.playback_target = replace(self.playback_target, address=connected_address)
        elif isinstance(connected_address, tuple) and connected_address and _is_ipv4_address(str(connected_address[0])):
            self.playback_target = replace(self.playback_target, address=str(connected_address[0]))
        self._logger.debug("connected to ESPHome satellite address=%s", self.playback_target.address)
        self._subscribe_voice_assistant()

    def _subscribe_voice_assistant(self) -> None:
        if self._unsubscribe is not None:
            self._logger.debug(
                "ESPHome satellite voice assistant already subscribed state=%s",
                self._voice_assistant_state(),
            )
            return
        self._unsubscribe = self._client.subscribe_voice_assistant(
            handle_start=self._handle_start,
            handle_audio=self._handle_audio,
            handle_stop=self._handle_stop,
        )
        self._logger.debug("subscribed to ESPHome satellite voice assistant state=%s", self._voice_assistant_state())

    def _unsubscribe_voice_assistant(self) -> None:
        if self._unsubscribe is None:
            self._logger.debug(
                "ESPHome satellite voice assistant already unsubscribed state=%s",
                self._voice_assistant_state(),
            )
            return
        self._unsubscribe()
        self._unsubscribe = None
        self._logger.debug(
            "unsubscribed from ESPHome satellite voice assistant state=%s",
            self._voice_assistant_state(),
        )

    async def _start_playback(self, event: PlaybackBegin) -> None:
        assert self._playback_id is None
        self._playback_id = event.playback_id

        try:
            await self._finish_playback()
            assert self._listen_id is None, "playback requires a disarmed microphone"
            await self._ensure_connected()
            connect_host = await _resolve_connect_host(self.playback_target.address)
            local_ip = _local_ip_for(connect_host)
            playback_stream = Box3PlaybackStream(
                local_ip=local_ip,
                rate=event.rate,
                width=event.width,
                channels=event.channels,
                logger=self._logger,
            )
            playback_stream.start()
            self._playback_stream = playback_stream
            url = f"http://{_host_for_url(local_ip)}:{playback_stream.port}{STREAM_PATH}"
            key = await _media_player_key(self._client)
            if event.volume is not None:
                self._client.media_player_command(key, volume=event.volume)
                await asyncio.sleep(0.1)
            self._client.media_player_command(key, media_url=url, announcement=True)
            self._logger.debug("playback stream started url=%s state=%s", url, self._voice_assistant_state())
        except _expected_unavailable_errors() as error:
            if self._playback_stream is not None:
                self._playback_stream.close()
                self._playback_stream = None
            await self._mark_disconnected()
            raise MicrophoneUnavailable(f"address={self.playback_target.address} error={error}") from error

    async def _finish_playback(self) -> None:
        if self._playback_stream is None:
            self._logger.debug("no playback stream to finish state=%s", self._voice_assistant_state())
            return

        playback_stream = self._playback_stream
        self._playback_stream = None
        self._logger.debug(
            "finishing playback stream queued_chunks=%s queued_bytes=%s state=%s",
            playback_stream.queued_chunks,
            playback_stream.queued_bytes,
            self._voice_assistant_state(),
        )
        playback_stream.finish()
        request_seen = await asyncio.to_thread(
            playback_stream.wait_for_request,
            STREAM_REQUEST_TIMEOUT_SECONDS,
        )
        if not request_seen:
            self._logger.warning(
                "ESPHome satellite did not request playback stream within %.1fs",
                STREAM_REQUEST_TIMEOUT_SECONDS,
            )
            playback_stream.close()
            return

        done_seen = await asyncio.to_thread(
            playback_stream.wait_for_done,
            STREAM_DONE_TIMEOUT_SECONDS,
        )
        if not done_seen:
            self._logger.warning(
                "ESPHome satellite playback stream did not finish within %.1fs",
                STREAM_DONE_TIMEOUT_SECONDS,
            )
        remaining_seconds = playback_stream.remaining_playback_seconds()
        if remaining_seconds > 0:
            self._logger.debug(
                "waiting for speaker playback drain seconds=%.2f audio_seconds=%.2f",
                remaining_seconds,
                playback_stream.audio_seconds,
            )
            await asyncio.sleep(remaining_seconds)
        self._logger.info(
            "playback stream finished queued_chunks=%s queued_bytes=%s drained_chunks=%s drained_bytes=%s "
            "audio_seconds=%.2f",
            playback_stream.queued_chunks,
            playback_stream.queued_bytes,
            playback_stream.drained_chunks,
            playback_stream.byte_count,
            playback_stream.audio_seconds,
        )
        playback_stream.close()

    async def _handle_start(
        self,
        _conversation_id: str,
        _flags: int,
        _audio_settings: Any,
        wake_word_phrase: str | None,
    ) -> int:
        self._drain_queued_events("new voice assistant stream start")
        self._chunks.clear()
        self._pending_audio_chunks.clear()
        self._wake_word = wake_word_phrase
        self._byte_count = 0
        self._audio_chunk_count = 0
        self._last_audio_progress_chunk_count = 0
        self._speech_started = False
        self._speech_candidate_started_at = None
        self._last_speech_at = None
        now = time.monotonic()
        ignore_seconds = self._post_speech_ignore_seconds if self._listening_mode is ListeningMode.FOLLOW_UP else 0.0
        self._stream_started_at = now + ignore_seconds
        self._ignore_audio_until = now + ignore_seconds if ignore_seconds > 0 else None
        self._ignored_audio_chunk_count = 0
        self._ignored_audio_byte_count = 0
        self._speech_done.clear()
        self._audio_ended = False
        self._late_audio_chunk_count = 0
        self._run_end_sent = False
        self._voice_assistant_run_active = True
        self._stream_done.clear()
        self._stream_started.set()
        self._logger.debug(
            "ESPHome voice assistant stream state initialized state=%s queue_size=%s",
            self._voice_assistant_state(),
            self._events.qsize(),
        )
        self._logger.debug(
            "voice assistant stream started conversation_id=%s wake_word=%r flags=%s audio_settings=%s "
            "post_speech_ignore_seconds=%.2f listening_mode=%s initial_silence_enabled=%s",
            _conversation_id,
            wake_word_phrase,
            _flags,
            _audio_settings,
            ignore_seconds,
            self._listening_mode,
            self._initial_silence_enabled(),
        )
        return 0

    async def _handle_audio(self, data: bytes, data2: bytes | None = None) -> None:
        for chunk in (data, data2):
            if not chunk:
                continue
            if self._audio_ended:
                self._drop_late_audio_chunk()
                continue
            if self._is_audio_ignored(chunk):
                continue
            self._chunks.append(chunk)
            self._byte_count += len(chunk)
            self._audio_chunk_count += 1
            if self._speech_started:
                self._queue_audio_chunk(chunk)
                self._observe_audio_level(chunk)
                continue

            self._pending_audio_chunks.append(chunk)
            self._observe_audio_level(chunk)
            if self._speech_started:
                self._flush_pending_audio_chunks()
                continue
            if self._audio_ended:
                self._pending_audio_chunks.clear()

        if (
            self._listening_mode is ListeningMode.OPEN_MIC
            and self._audio_chunk_count > 0
            and self._audio_chunk_count - self._last_audio_progress_chunk_count
            >= OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL
        ):
            self._last_audio_progress_chunk_count = self._audio_chunk_count
            self._events.put_nowait(
                AudioProgress(
                    listen_id=self._required_listen_id(),
                    utterance_id=self._required_utterance_id(),
                    chunks=self._audio_chunk_count,
                    bytes=self._byte_count,
                )
            )
        if self._audio_chunk_count > 0 and (self._audio_chunk_count == 1 or self._audio_chunk_count % 50 == 0):
            self._logger.debug(
                "received audio chunks=%s bytes=%s",
                self._audio_chunk_count,
                self._byte_count,
            )

    def _flush_pending_audio_chunks(self) -> None:
        if not self._pending_audio_chunks:
            return
        for chunk in self._pending_audio_chunks:
            self._queue_audio_chunk(chunk)
        self._logger.debug(
            "flushed speech pre-roll chunks=%s bytes=%s",
            len(self._pending_audio_chunks),
            sum(len(chunk) for chunk in self._pending_audio_chunks),
        )
        self._pending_audio_chunks.clear()

    def _is_audio_ignored(self, chunk: bytes) -> bool:
        if self._ignore_audio_until is None:
            return False
        now = time.monotonic()
        if now >= self._ignore_audio_until:
            if self._ignored_audio_chunk_count > 0:
                self._logger.debug(
                    "finished post-speech audio ignore window ignored_chunks=%s ignored_bytes=%s",
                    self._ignored_audio_chunk_count,
                    self._ignored_audio_byte_count,
                )
            self._ignore_audio_until = None
            return False

        self._ignored_audio_chunk_count += 1
        self._ignored_audio_byte_count += len(chunk)
        if self._ignored_audio_chunk_count == 1 or self._ignored_audio_chunk_count % 50 == 0:
            remaining_seconds = self._ignore_audio_until - now
            self._logger.debug(
                "ignored post-speech audio chunks=%s bytes=%s remaining_seconds=%.2f",
                self._ignored_audio_chunk_count,
                self._ignored_audio_byte_count,
                remaining_seconds,
            )
        return True

    async def _handle_stop(self, _aborted: bool) -> None:
        self._logger.debug(
            "ESPHome voice assistant stop callback aborted=%s state=%s",
            _aborted,
            self._voice_assistant_state(),
        )
        self._logger.debug(
            "voice assistant stream stop aborted=%s chunks=%s bytes=%s",
            _aborted,
            self._audio_chunk_count,
            self._byte_count,
        )
        self._stream_done.set()
        self._voice_assistant_run_active = False
        self._finish_audio_stream()
        self._logger.debug("ESPHome voice assistant stop handled state=%s", self._voice_assistant_state())

    def _drop_late_audio_chunk(self) -> None:
        self._late_audio_chunk_count += 1
        if self._late_audio_chunk_count == 1 or self._late_audio_chunk_count % 50 == 0:
            self._logger.debug(
                "dropped audio chunks after SpeechEnded chunks=%s",
                self._late_audio_chunk_count,
            )

    def _send_voice_assistant_event(self, event_name: str) -> None:
        _require_aioesphomeapi()

        event = getattr(aioesphomeapi.VoiceAssistantEventType, event_name)
        self._logger.debug(
            "sending ESPHome voice assistant event=%s state=%s",
            event_name,
            self._voice_assistant_state(),
        )
        self._client.send_voice_assistant_event(event, None)

    async def _execute_api_service(self, service_name: str) -> None:
        await self._ensure_connected()
        try:
            _, services = await asyncio.wait_for(
                self._client.list_entities_services(),
                timeout=API_SERVICE_TIMEOUT_SECONDS,
            )
            for service in services:
                if service.name == service_name:
                    await asyncio.wait_for(
                        self._client.execute_service(service, {}),
                        timeout=API_SERVICE_TIMEOUT_SECONDS,
                    )
                    self._logger.debug("executed ESPHome satellite API service=%s", service_name)
                    return
        except TimeoutError as error:
            await self._mark_disconnected()
            raise MicrophoneUnavailable(
                f"address={self.playback_target.address} service={service_name} "
                f"timed out after {API_SERVICE_TIMEOUT_SECONDS:.1f}s"
            ) from error
        except _expected_unavailable_errors() as error:
            await self._mark_disconnected()
            raise MicrophoneUnavailable(f"address={self.playback_target.address} error={error}") from error

        raise RuntimeError(f"ESPHome satellite API service not found: {service_name}")

    async def _execute_optional_api_service(self, service_name: str) -> None:
        try:
            await self._execute_api_service(service_name)
        except RuntimeError as error:
            if str(error) != f"ESPHome satellite API service not found: {service_name}":
                raise
            self._logger.warning(
                "ESPHome satellite optional API service unavailable service=%s state=%s",
                service_name,
                self._voice_assistant_state(),
            )

    async def _start_wake_word_listening(self) -> None:
        self._logger.debug("starting ESPHome wake-word re-arm state=%s", self._voice_assistant_state())
        await self._finish_voice_assistant_run()
        self._drain_queued_events("wake-word re-arm")
        if self._client is not None:
            self._unsubscribe_voice_assistant()
            self._subscribe_voice_assistant()
            self._logger.debug("ESPHome satellite wake-word listening armed state=%s", self._voice_assistant_state())

    async def _finish_voice_assistant_run(self) -> None:
        await self._ensure_connected()
        self._logger.debug("finishing ESPHome voice assistant run requested state=%s", self._voice_assistant_state())
        if self._voice_assistant_run_active:
            try:
                self._send_run_end_once()
            except _expected_unavailable_errors() as error:
                await self._mark_disconnected()
                raise MicrophoneUnavailable(f"address={self.playback_target.address} error={error}") from error
            self._voice_assistant_run_active = False
            self._logger.debug(
                "ESPHome voice assistant run marked inactive; waiting for stop before re-arm timeout_seconds=%.2f "
                "state=%s",
                VOICE_ASSISTANT_REARM_DELAY_SECONDS,
                self._voice_assistant_state(),
            )
            try:
                await asyncio.wait_for(self._stream_done.wait(), timeout=VOICE_ASSISTANT_REARM_DELAY_SECONDS)
            except TimeoutError:
                self._logger.debug(
                    "ESPHome voice assistant stop did not arrive before re-arm timeout state=%s",
                    self._voice_assistant_state(),
                )
            else:
                self._logger.debug(
                    "ESPHome voice assistant stop observed before re-arm state=%s",
                    self._voice_assistant_state(),
                )
            await self._recover_stale_voice_assistant_stream_before_rearm()
        else:
            self._logger.debug("ESPHome voice assistant run already inactive state=%s", self._voice_assistant_state())
            await self._recover_stale_voice_assistant_stream_before_rearm()

    async def _recover_stale_voice_assistant_stream_before_rearm(self) -> None:
        if not self._run_end_sent or self._stream_done.is_set():
            return

        self._logger.debug(
            "recovering stale ESPHome voice assistant stream before re-arm state=%s",
            self._voice_assistant_state(),
        )
        await self._mark_disconnected()
        self._voice_assistant_run_active = False
        self._run_end_sent = False
        self._stream_done.set()
        self._audio_ended = True
        self._pending_audio_chunks.clear()
        self._logger.debug(
            "recovered stale ESPHome voice assistant stream before re-arm state=%s",
            self._voice_assistant_state(),
        )

    def _drain_queued_events(self, reason: str) -> None:
        counts: dict[str, int] = {}
        while True:
            try:
                event = self._events.get_nowait()
            except asyncio.QueueEmpty:
                break
            counts[type(event).__name__] = counts.get(type(event).__name__, 0) + 1
        if counts:
            self._logger.debug(
                "drained stale ESPHome voice assistant events reason=%s counts=%s state=%s",
                reason,
                counts,
                self._voice_assistant_state(),
            )

    def _send_run_end_once(self) -> None:
        if self._run_end_sent:
            self._logger.debug("ESPHome voice assistant run-end already sent state=%s", self._voice_assistant_state())
            return
        self._send_voice_assistant_event("VOICE_ASSISTANT_RUN_END")
        self._run_end_sent = True
        self._logger.debug("sent VOICE_ASSISTANT_RUN_END state=%s", self._voice_assistant_state())

    async def _mark_disconnected(self) -> None:
        self._logger.debug("marking ESPHome satellite disconnected state=%s", self._voice_assistant_state())
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        client = self._client
        self._client = None
        if client is not None:
            with suppress(Exception):
                await client.disconnect()

    def _finish_audio_stream(self) -> None:
        if self._audio_ended:
            self._logger.debug("ESPHome audio stream already finished state=%s", self._voice_assistant_state())
            return
        if self._listening_mode is ListeningMode.OPEN_MIC and not self._stream_done.is_set():
            self._logger.debug(
                "finishing open-mic speech segment without ending ESPHome stream state=%s",
                self._voice_assistant_state(),
            )
            if not self._speech_started:
                self._pending_audio_chunks.clear()
            self._queue_speech_ended("completed")
            self._logger.debug(
                "queued open-mic audio segment end event state=%s queue_size=%s",
                self._voice_assistant_state(),
                self._events.qsize(),
            )
            self._reset_open_mic_segment_state()
            return
        self._logger.debug("finishing ESPHome audio stream state=%s", self._voice_assistant_state())
        self._audio_ended = True
        if not self._speech_started:
            self._pending_audio_chunks.clear()
        if not self._stream_done.is_set():
            self._send_voice_assistant_event("VOICE_ASSISTANT_STT_VAD_END")
            self._logger.debug("sent VOICE_ASSISTANT_STT_VAD_END state=%s", self._voice_assistant_state())
        self._queue_speech_ended("aborted" if self._stream_done.is_set() else "completed")
        self._logger.debug(
            "queued ESPHome audio end event state=%s queue_size=%s",
            self._voice_assistant_state(),
            self._events.qsize(),
        )

    def _reset_open_mic_segment_state(self) -> None:
        self._chunks.clear()
        self._pending_audio_chunks.clear()
        self._byte_count = 0
        self._audio_chunk_count = 0
        self._last_audio_progress_chunk_count = 0
        self._speech_started = False
        self._speech_candidate_started_at = None
        self._last_speech_at = None
        self._speech_done.clear()
        self._late_audio_chunk_count = 0
        self._ignored_audio_chunk_count = 0
        self._ignored_audio_byte_count = 0
        self._stream_started_at = time.monotonic()
        self._logger.debug("reset open-mic segment state state=%s", self._voice_assistant_state())

    def _queue_speech_started(self) -> None:
        assert self._utterance_id is None, "nested speech segment"
        listen_id = self._required_listen_id()
        utterance_id = str(uuid.uuid4())
        self._utterance_id = utterance_id
        wake_word = self._wake_word if self._listening_mode is ListeningMode.WAKE_WORD else None
        self._events.put_nowait(
            SpeechStarted(
                listen_id=listen_id,
                utterance_id=utterance_id,
                rate=16000,
                width=2,
                channels=1,
                wake_word=wake_word,
            )
        )

    def _queue_audio_chunk(self, data: bytes) -> None:
        self._events.put_nowait(
            AudioChunk(
                listen_id=self._required_listen_id(),
                utterance_id=self._required_utterance_id(),
                data=data,
            )
        )

    def _queue_speech_ended(self, reason: str) -> None:
        if self._utterance_id is None:
            return
        listen_id = self._required_listen_id()
        utterance_id = self._required_utterance_id()
        self._events.put_nowait(SpeechEnded(listen_id, utterance_id, reason))
        self._utterance_id = None
        if self._listening_mode is not ListeningMode.OPEN_MIC:
            self._listen_id = None
            self._listening_mode = None

    def _required_listen_id(self) -> str:
        assert self._listen_id is not None, "driver event without active listen_id"
        return self._listen_id

    def _required_utterance_id(self) -> str:
        assert self._utterance_id is not None, "audio event without active utterance_id"
        return self._utterance_id

    def _voice_assistant_state(self) -> str:
        return (
            f"connected={self._client is not None} "
            f"subscribed={self._unsubscribe is not None} "
            f"run_active={self._voice_assistant_run_active} "
            f"listening_mode={self._listening_mode} "
            f"run_end_sent={self._run_end_sent} "
            f"stream_done={self._stream_done.is_set()} "
            f"audio_ended={self._audio_ended} "
            f"speech_started={self._speech_started} "
            f"chunks={self._audio_chunk_count} "
            f"bytes={self._byte_count}"
        )

    def _initial_silence_enabled(self) -> bool:
        return self._listening_mode is not ListeningMode.OPEN_MIC

    async def _wait_for_end_of_speech(self, capture_seconds: float) -> None:
        stream_done_task = asyncio.create_task(self._stream_done.wait())
        speech_done_task = asyncio.create_task(self._speech_done.wait())
        tasks = {stream_done_task, speech_done_task}

        done, pending = await asyncio.wait(
            tasks,
            timeout=capture_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task

        if not done:
            self._logger.debug(
                "max capture time reached seconds=%s chunks=%s bytes=%s",
                capture_seconds,
                self._audio_chunk_count,
                self._byte_count,
            )

    def _observe_audio_level(self, data: bytes) -> None:
        peak = _pcm16_peak(data)
        now = time.monotonic()
        if peak >= self._speech_peak_threshold:
            if not self._speech_started:
                if self._speech_candidate_started_at is None:
                    self._speech_candidate_started_at = now
                    return
                candidate_seconds = now - self._speech_candidate_started_at
                if candidate_seconds < SPEECH_START_SECONDS:
                    return
                self._logger.debug("speech detected peak=%s", peak)
                self._queue_speech_started()
            self._speech_started = True
            self._last_speech_at = now
            return

        if not self._speech_started:
            self._speech_candidate_started_at = None
            if (
                self._initial_silence_enabled()
                and self._stream_started_at is not None
                and now - self._stream_started_at >= self._initial_silence_seconds
                and not self._speech_done.is_set()
            ):
                self._logger.debug(
                    "initial silence timeout seconds=%.2f peak=%s",
                    self._initial_silence_seconds,
                    peak,
                )
                self._speech_done.set()
                self._finish_audio_stream()
            return
        if self._last_speech_at is None:
            return

        silence_seconds = now - self._last_speech_at
        if silence_seconds >= self._end_silence_seconds and not self._speech_done.is_set():
            self._logger.debug(
                "end of speech detected silence_seconds=%.2f peak=%s",
                silence_seconds,
                peak,
            )
            self._speech_done.set()
            self._finish_audio_stream()


def _pcm16_peak(data: bytes) -> int:
    if len(data) < 2:
        return 0

    samples = array.array("h")
    samples.frombytes(data[: len(data) - (len(data) % 2)])
    if not samples:
        return 0

    return max(abs(sample) for sample in samples)


def _expected_unavailable_errors() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (OSError, TimeoutError)
    api_connection_error = getattr(aioesphomeapi, "APIConnectionError", None)
    if api_connection_error is not None:
        errors = (api_connection_error, *errors)
    return errors


class Box3PlaybackStream:
    def __init__(self, local_ip: str, rate: int, width: int, channels: int, logger: logging.Logger) -> None:
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self._request_event = threading.Event()
        self._done_event = threading.Event()
        self._rate = rate
        self._width = width
        self._channels = channels
        self._logger = logger
        self._server = ThreadingHTTPServer(
            (local_ip, 0),
            partial(
                Box3PlaybackStreamHandler,
                chunks=self._chunks,
                rate=rate,
                width=width,
                channels=channels,
                request_event=self._request_event,
                done_event=self._done_event,
                byte_counter=self,
                logger=logger,
            ),
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.byte_count = 0
        self.queued_chunks = 0
        self.queued_bytes = 0
        self.drained_chunks = 0
        self.first_audio_drained_at: float | None = None
        self.done_at: float | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    def start(self) -> None:
        self._thread.start()

    def write(self, data: bytes) -> None:
        self.queued_chunks += 1
        self.queued_bytes += len(data)
        if self.queued_chunks == 1 or self.queued_chunks % 50 == 0:
            self._logger.debug(
                "queued playback audio chunks=%s bytes=%s",
                self.queued_chunks,
                self.queued_bytes,
            )
        self._chunks.put(data)

    def finish(self) -> None:
        self._chunks.put(None)

    def wait_for_request(self, timeout: float) -> bool:
        return self._request_event.wait(timeout)

    def wait_for_done(self, timeout: float) -> bool:
        return self._done_event.wait(timeout)

    @property
    def audio_seconds(self) -> float:
        byte_rate = self._rate * self._width * self._channels
        if byte_rate <= 0:
            return 0.0
        return self.queued_bytes / byte_rate

    def remaining_playback_seconds(self, now: float | None = None) -> float:
        if self.first_audio_drained_at is None:
            return 0.0
        observed_at = now if now is not None else time.monotonic()
        played_seconds = observed_at - self.first_audio_drained_at
        return max(0.0, self.audio_seconds - played_seconds + PLAYBACK_DRAIN_GRACE_SECONDS)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class Box3PlaybackStreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args: Any,
        chunks: queue.Queue[bytes | None],
        rate: int,
        width: int,
        channels: int,
        request_event: threading.Event,
        done_event: threading.Event,
        byte_counter: Box3PlaybackStream,
        logger: logging.Logger,
        **kwargs: Any,
    ) -> None:
        self._chunks = chunks
        self._rate = rate
        self._width = width
        self._channels = channels
        self._request_event = request_event
        self._done_event = done_event
        self._byte_counter = byte_counter
        self._logger = logger
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != STREAM_PATH:
            self.send_error(404)
            return

        self._request_event.set()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            header = wav_header(self._rate, self._width, self._channels)
            self.wfile.write(header)
            self.wfile.flush()
            self._byte_counter.byte_count += len(header)
            self._logger.debug(
                "playback HTTP stream requested rate=%s width=%s channels=%s",
                self._rate,
                self._width,
                self._channels,
            )
            while True:
                chunk = self._chunks.get()
                if chunk is None:
                    self._logger.debug(
                        "playback HTTP stream reached end drained_chunks=%s bytes=%s",
                        self._byte_counter.drained_chunks,
                        self._byte_counter.byte_count,
                    )
                    return
                if self._byte_counter.first_audio_drained_at is None:
                    self._byte_counter.first_audio_drained_at = time.monotonic()
                self.wfile.write(chunk)
                self.wfile.flush()
                self._byte_counter.byte_count += len(chunk)
                self._byte_counter.drained_chunks += 1
                if self._byte_counter.drained_chunks == 1 or self._byte_counter.drained_chunks % 50 == 0:
                    self._logger.debug(
                        "drained playback audio chunks=%s bytes=%s",
                        self._byte_counter.drained_chunks,
                        self._byte_counter.byte_count,
                    )
        finally:
            self._byte_counter.done_at = time.monotonic()
            self._done_event.set()


async def _resolve_connect_host(host: str, port: int = API_PORT) -> str:
    _require_aioesphomeapi()

    results = await aioesphomeapi.host_resolver.async_resolve_host([host], port, timeout=10.0)
    first_host = host
    for result in results:
        sockaddr = result.sockaddr
        if isinstance(sockaddr, tuple) and len(sockaddr) >= 2:
            result_host = str(sockaddr[0])
            if _is_ipv4_address(result_host):
                return result_host
            first_host = result_host

    return first_host


def _local_ip_for(host: str, port: int = API_PORT) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, port))
        return sock.getsockname()[0]


def _is_ipv4_address(value: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except ValueError:
        return False


def _host_for_url(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


async def _media_player_key(client) -> int:
    entities, _ = await client.list_entities_services()
    for entity in entities:
        if type(entity).__name__ == "MediaPlayerInfo":
            return entity.key
    raise RuntimeError("No media player entity exposed by microphone")


def wav_header(rate: int, width: int, channels: int) -> bytes:
    data_size = 0x7FFFFFF0
    byte_rate = rate * channels * width
    block_align = channels * width
    riff_size = data_size + WAV_HEADER_BYTES - 8
    return (
        b"RIFF"
        + riff_size.to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + channels.to_bytes(2, "little")
        + rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + (width * 8).to_bytes(2, "little")
        + b"data"
        + data_size.to_bytes(4, "little")
    )


def _require_aioesphomeapi() -> None:
    if aioesphomeapi is None:
        raise RuntimeError("aioesphomeapi package is required for ESPHome satellite microphone support")
