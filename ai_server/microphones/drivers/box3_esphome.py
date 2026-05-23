from __future__ import annotations

import asyncio
import array
from contextlib import suppress
from dataclasses import replace
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import queue
import socket
import threading
import time
from typing import Any

from ai_server.config import MicrophoneConfig
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioEvent, AudioStart
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
SPEECH_PEAK_THRESHOLD = 500
END_SILENCE_SECONDS = 0.9
START_GRACE_SECONDS = 0.5
VOICE_ASSISTANT_STOP_TIMEOUT_SECONDS = 5.0
VOICE_ASSISTANT_REARM_DELAY_SECONDS = 0.2


class Box3EsphomeMicrophone:
    def __init__(
        self,
        context: MicrophoneContext,
        playback_target: PlaybackTarget,
        client_info: str = "ai-server-box3-mic",
    ) -> None:
        self.context = context
        self.playback_target = playback_target
        self._logger = logging.getLogger(f"{__name__}.Box3EsphomeMicrophone[{context.instance_id}]")
        self._client_info = client_info
        self._client = None
        self._unsubscribe = None
        self._stream_started = asyncio.Event()
        self._stream_done = asyncio.Event()
        self._events: asyncio.Queue[AudioEvent] = asyncio.Queue()
        self._chunks: list[bytes] = []
        self._wake_word: str | None = None
        self._byte_count = 0
        self._audio_chunk_count = 0
        self._speech_started = False
        self._last_speech_at: float | None = None
        self._stream_started_at: float | None = None
        self._speech_done = asyncio.Event()
        self._audio_ended = False
        self._late_audio_chunk_count = 0
        self._run_end_sent = False
        self._rearm_task: asyncio.Task[None] | None = None
        self._playback_stream: Box3PlaybackStream | None = None

    @classmethod
    def from_config(cls, config: MicrophoneConfig) -> Box3EsphomeMicrophone:
        expected_name = config.options.get("expected_name")
        if expected_name is not None and not isinstance(expected_name, str):
            raise ValueError(f"microphone {config.name} expected_name must be a string when provided")

        context = MicrophoneContext(type=config.type, name=config.name, location=config.location)
        playback_target = PlaybackTarget(
            type=config.type,
            name=config.name,
            address=config.options["address"],
            api_key=config.options["api_key"],
            expected_name=expected_name,
        )
        return cls(context=context, playback_target=playback_target)

    async def wait_for_event(self) -> AudioEvent:
        await self._ensure_connected()
        return await self._events.get()

    async def send_audio_event(self, event: AudioEvent) -> None:
        if isinstance(event, AudioStart):
            self._logger.debug("sending audio start event to BOX-3")
            await self._start_playback(event)
            return
        if isinstance(event, AudioChunk):
            self._logger.debug("sending audio chunk event to BOX-3")
            if self._playback_stream is None:
                raise RuntimeError("cannot send audio chunk before audio start")
            self._playback_stream.write(event.data)
            return
        if isinstance(event, AudioEnd):
            self._logger.debug("sending audio end event to BOX-3")
            await self._finish_playback()
            return
        raise ValueError(f"unsupported audio event: {type(event).__name__}")

    async def close(self) -> None:
        await self._finish_playback()
        if self._rearm_task is not None:
            self._rearm_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._rearm_task
            self._rearm_task = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def _ensure_connected(self) -> None:
        if self._client is not None:
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
        self._logger.info("connecting to BOX-3 address=%s", self.playback_target.address)
        await self._client.connect(login=True)
        connected_address = self._client.connected_address
        if isinstance(connected_address, str) and connected_address:
            self.playback_target = replace(self.playback_target, address=connected_address)
        elif isinstance(connected_address, tuple) and connected_address:
            self.playback_target = replace(self.playback_target, address=str(connected_address[0]))
        self._logger.info("connected to BOX-3 address=%s", self.playback_target.address)
        self._subscribe_voice_assistant()

    def _subscribe_voice_assistant(self) -> None:
        if self._unsubscribe is not None:
            return
        self._unsubscribe = self._client.subscribe_voice_assistant(
            handle_start=self._handle_start,
            handle_audio=self._handle_audio,
            handle_stop=self._handle_stop,
        )
        self._logger.info("subscribed to BOX-3 voice assistant")

    def _unsubscribe_voice_assistant(self) -> None:
        if self._unsubscribe is None:
            return
        self._unsubscribe()
        self._unsubscribe = None
        self._logger.debug("unsubscribed from BOX-3 voice assistant")

    async def _start_playback(self, event: AudioStart) -> None:
        if event.rate is None or event.width is None or event.channels is None:
            raise ValueError("playback AudioStart requires rate, width, and channels")

        await self._finish_playback()
        await self._ensure_connected()
        connect_host = await _resolve_connect_host(self.playback_target.address)
        local_ip = _local_ip_for(connect_host)
        playback_stream = Box3PlaybackStream(
            local_ip=local_ip,
            rate=event.rate,
            width=event.width,
            channels=event.channels,
        )
        playback_stream.start()
        self._playback_stream = playback_stream
        url = f"http://{local_ip}:{playback_stream.port}{STREAM_PATH}"
        key = await _media_player_key(self._client)
        if event.volume is not None:
            self._client.media_player_command(key, volume=event.volume)
            await asyncio.sleep(0.1)
        self._client.media_player_command(key, media_url=url, announcement=True)
        self._logger.info("playback stream started url=%s", url)

    async def _finish_playback(self) -> None:
        if self._playback_stream is None:
            return

        playback_stream = self._playback_stream
        self._playback_stream = None
        playback_stream.finish()
        request_seen = await asyncio.to_thread(
            playback_stream.wait_for_request,
            STREAM_REQUEST_TIMEOUT_SECONDS,
        )
        if not request_seen:
            self._logger.warning(
                "BOX-3 did not request playback stream within %.1fs",
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
                "BOX-3 playback stream did not finish within %.1fs",
                STREAM_DONE_TIMEOUT_SECONDS,
            )
        self._logger.info("playback stream finished bytes=%s", playback_stream.byte_count)
        playback_stream.close()

    async def _handle_start(
        self,
        _conversation_id: str,
        _flags: int,
        _audio_settings: Any,
        wake_word_phrase: str | None,
    ) -> int:
        self._chunks.clear()
        self._wake_word = wake_word_phrase
        self._byte_count = 0
        self._audio_chunk_count = 0
        self._speech_started = False
        self._last_speech_at = None
        self._stream_started_at = time.monotonic()
        self._speech_done.clear()
        self._audio_ended = False
        self._late_audio_chunk_count = 0
        self._run_end_sent = False
        self._stream_done.clear()
        self._stream_started.set()
        self._events.put_nowait(AudioStart(wake_word=wake_word_phrase))
        self._logger.info(
            "voice assistant stream started conversation_id=%s wake_word=%r flags=%s audio_settings=%s",
            _conversation_id,
            wake_word_phrase,
            _flags,
            _audio_settings,
        )
        return 0

    async def _handle_audio(self, data: bytes, data2: bytes | None = None) -> None:
        for chunk in (data, data2):
            if not chunk:
                continue
            if self._audio_ended:
                self._drop_late_audio_chunk()
                continue
            self._chunks.append(chunk)
            self._events.put_nowait(AudioChunk(data=chunk))
            self._byte_count += len(chunk)
            self._audio_chunk_count += 1
            self._observe_audio_level(chunk)

        if self._audio_chunk_count == 1 or self._audio_chunk_count % 50 == 0:
            self._logger.debug(
                "received audio chunks=%s bytes=%s",
                self._audio_chunk_count,
                self._byte_count,
            )

    async def _handle_stop(self, _aborted: bool) -> None:
        self._logger.info(
            "voice assistant stream stop aborted=%s chunks=%s bytes=%s",
            _aborted,
            self._audio_chunk_count,
            self._byte_count,
        )
        self._stream_done.set()
        self._finish_audio_stream()

    def _drop_late_audio_chunk(self) -> None:
        self._late_audio_chunk_count += 1
        if self._late_audio_chunk_count == 1 or self._late_audio_chunk_count % 50 == 0:
            self._logger.debug(
                "dropped audio chunks after AudioEnd chunks=%s",
                self._late_audio_chunk_count,
            )

    def _send_voice_assistant_event(self, event_name: str) -> None:
        _require_aioesphomeapi()

        event = getattr(aioesphomeapi.VoiceAssistantEventType, event_name)
        self._client.send_voice_assistant_event(event, None)

    def _send_run_end_once(self) -> None:
        if self._run_end_sent:
            return
        self._send_voice_assistant_event("VOICE_ASSISTANT_RUN_END")
        self._run_end_sent = True
        self._logger.debug("sent VOICE_ASSISTANT_RUN_END")

    def _finish_audio_stream(self) -> None:
        if self._audio_ended:
            return
        self._audio_ended = True
        if not self._stream_done.is_set():
            self._send_voice_assistant_event("VOICE_ASSISTANT_STT_VAD_END")
            self._logger.debug("sent VOICE_ASSISTANT_STT_VAD_END")
        self._events.put_nowait(AudioEnd())
        self._start_rearm_task()

    def _start_rearm_task(self) -> None:
        if self._rearm_task is not None and not self._rearm_task.done():
            return
        self._rearm_task = asyncio.create_task(self._rearm_after_audio_end())

    async def _rearm_after_audio_end(self) -> None:
        try:
            if not self._stream_done.is_set():
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stream_done.wait(),
                        timeout=VOICE_ASSISTANT_STOP_TIMEOUT_SECONDS,
                    )
            if not self._stream_done.is_set():
                self._logger.debug(
                    "BOX-3 stream stop was not observed within %.1fs; forcing run end",
                    VOICE_ASSISTANT_STOP_TIMEOUT_SECONDS,
                )
            self._send_run_end_once()
            await asyncio.sleep(VOICE_ASSISTANT_REARM_DELAY_SECONDS)
            if self._client is not None:
                self._unsubscribe_voice_assistant()
                self._subscribe_voice_assistant()
                self._logger.info("BOX-3 voice assistant rearmed")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._logger.exception("failed to rearm BOX-3 voice assistant")

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
            self._logger.info(
                "max capture time reached seconds=%s chunks=%s bytes=%s",
                capture_seconds,
                self._audio_chunk_count,
                self._byte_count,
            )

    def _observe_audio_level(self, data: bytes) -> None:
        peak = _pcm16_peak(data)
        now = time.monotonic()
        if peak >= SPEECH_PEAK_THRESHOLD:
            if not self._speech_started:
                self._logger.info("speech detected peak=%s", peak)
            self._speech_started = True
            self._last_speech_at = now
            return

        if not self._speech_started:
            return
        if self._stream_started_at is not None and now - self._stream_started_at < START_GRACE_SECONDS:
            return
        if self._last_speech_at is None:
            return

        silence_seconds = now - self._last_speech_at
        if silence_seconds >= END_SILENCE_SECONDS and not self._speech_done.is_set():
            self._logger.info(
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


class Box3PlaybackStream:
    def __init__(self, local_ip: str, rate: int, width: int, channels: int) -> None:
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self._request_event = threading.Event()
        self._done_event = threading.Event()
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
            ),
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.byte_count = 0

    @property
    def port(self) -> int:
        return int(self._server.server_port)

    def start(self) -> None:
        self._thread.start()

    def write(self, data: bytes) -> None:
        self._chunks.put(data)

    def finish(self) -> None:
        self._chunks.put(None)

    def wait_for_request(self, timeout: float) -> bool:
        return self._request_event.wait(timeout)

    def wait_for_done(self, timeout: float) -> bool:
        return self._done_event.wait(timeout)

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
        **kwargs: Any,
    ) -> None:
        self._chunks = chunks
        self._rate = rate
        self._width = width
        self._channels = channels
        self._request_event = request_event
        self._done_event = done_event
        self._byte_counter = byte_counter
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
            while True:
                chunk = self._chunks.get()
                if chunk is None:
                    return
                self.wfile.write(chunk)
                self.wfile.flush()
                self._byte_counter.byte_count += len(chunk)
        finally:
            self._done_event.set()


async def _resolve_connect_host(host: str, port: int = API_PORT) -> str:
    _require_aioesphomeapi()

    results = await aioesphomeapi.host_resolver.async_resolve_host([host], port, timeout=10.0)
    for result in results:
        sockaddr = result.sockaddr
        if isinstance(sockaddr, tuple) and len(sockaddr) >= 2:
            return str(sockaddr[0])

    return host


def _local_ip_for(host: str, port: int = API_PORT) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, port))
        return sock.getsockname()[0]


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
        raise RuntimeError("aioesphomeapi package is required for BOX-3 microphone support")
