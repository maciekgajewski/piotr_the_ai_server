from __future__ import annotations

import asyncio
import logging
import random
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable

from ai_server.agent import Agent
from ai_server.config import ConversationConfig, DEFAULT_AUDIO_EVENT_TIMEOUT_SECONDS, DEFAULT_AUDIO_START_TIMEOUT_SECONDS
from ai_server.config import MicrophoneConfig
from ai_server.config import SpeakerRecognitionConfig, SttConfig, TtsConfig
from ai_server.conversations.bridge import BridgeSettings, FatalTerminationController
from ai_server.conversations.context_provider import ConfigContextProvider, ContextProvider
from ai_server.conversations.id_factory import new_id
from ai_server.conversations.supervision import supervise_input
from ai_server.microphones.conversation_adapter import VoiceInputAdapter, VoiceInputSession
from ai_server.microphones.drivers import create_microphone
from ai_server.microphones.interfaces import Microphone, MicrophoneUnavailable, TextToSpeech
from ai_server.microphones.messages import AudioChunk, AudioProgress, CueFinished, CueType, ListeningMode
from ai_server.microphones.messages import ListeningStarted, ListeningStopped, MicrophoneCommand, MicrophoneEvent
from ai_server.microphones.messages import PlaybackBegin, PlaybackChunk, PlaybackEnd, PlaybackFinished, PlayCue
from ai_server.microphones.messages import ResetWakeCandidate, SetVisualState, SpeechEnded, SpeechStarted
from ai_server.microphones.messages import StartListening, StopListening, SynthesizedAudioChunk, SynthesizedAudioEnd
from ai_server.microphones.messages import SynthesizedAudioStart, VisualState
from ai_server.microphones.protocol import DriverState, MicrophoneProtocolState
from ai_server.microphones.tts import PiperTextToSpeech
from ai_server.speaker_recognition.client import SpeakerRecognitionAudioFormat, SpeakerRecognitionClient
from ai_server.speaker_recognition.client import SpeakerRecognitionResult, SpeakerRecognitionStream
from ai_server.speaker_recognition.client import voice_profiles_from_users
from ai_server.speech_to_text.faster_whisper import FasterWhisperSpeechToText
from ai_server.speech_to_text.interfaces import SpeechToText, StreamingSttSession, SttSession
from ai_server.speech_to_text.messages import TextEnd, TextFragment, TextPartial
from ai_server.speech_to_text.types import PcmAudioChunk


DEFAULT_PROCESSING_UPDATE_CUES = ("Hmm...", "Myslę....", "momencik...")
PROCESSING_UPDATE_VOLUME = 0.7


@dataclass(frozen=True)
class CapturedUtterance:
    captured: bool
    text_fragments: tuple[str, ...]
    speaker_result: SpeakerRecognitionResult | None = None


class MicrophoneManager:
    def __init__(
        self,
        microphones: list[Microphone],
        stt: SpeechToText,
        tts: TextToSpeech,
        agent: Agent,
        follow_up_timeout_seconds: float,
        bridge_settings: BridgeSettings,
        microphone_assistant_text_buffers: dict[str, int],
        microphone_follow_up_timeouts: dict[str, float] | None = None,
        microphone_audio_start_timeouts: dict[str, float] | None = None,
        microphone_audio_event_timeouts: dict[str, float] | None = None,
        open_microphones: set[str] | None = None,
        user_settings: dict[str, dict[str, Any]] | None = None,
        context_provider: ContextProvider | None = None,
        fatal_termination: FatalTerminationController | None = None,
        speaker_recognition: SpeakerRecognitionClient | None = None,
        processing_update_spoken_cues: tuple[str, ...] = DEFAULT_PROCESSING_UPDATE_CUES,
        open_mic_wake_phrase: str = "Ryszardzie",
    ) -> None:
        self._microphones = microphones
        self._stt = stt
        self._tts = tts
        self._agent = agent
        self._follow_up_timeout_seconds = follow_up_timeout_seconds
        self._microphone_follow_up_timeouts = dict(microphone_follow_up_timeouts or {})
        self._microphone_audio_start_timeouts = dict(microphone_audio_start_timeouts or {})
        self._microphone_audio_event_timeouts = dict(microphone_audio_event_timeouts or {})
        self._open_microphones = set(open_microphones or ())
        self._user_settings = dict(user_settings or {})
        self._context_provider = context_provider or ConfigContextProvider(self._user_settings)
        self._bridge_settings = bridge_settings
        self._microphone_assistant_text_buffers = dict(microphone_assistant_text_buffers)
        microphone_names = {microphone.context.name for microphone in microphones}
        missing_text_buffers = microphone_names - self._microphone_assistant_text_buffers.keys()
        if missing_text_buffers:
            raise ValueError(
                "assistant text buffer is required for microphones: "
                + ", ".join(sorted(missing_text_buffers))
            )
        if any(value <= 0 for value in self._microphone_assistant_text_buffers.values()):
            raise ValueError("assistant text buffers must be positive")
        self._fatal_termination = fatal_termination
        self._processing_update_spoken_cues = processing_update_spoken_cues
        self._speaker_recognition = speaker_recognition or SpeakerRecognitionClient(
            url=None,
            timeout_seconds=1.0,
            profiles={},
        )
        self._open_mic_wake_phrase = open_mic_wake_phrase
        self._tasks: list[asyncio.Task[None]] = []
        self._closing = False
        self._input_sessions: dict[str, VoiceInputSession] = {}
        self._protocols = {
            microphone.context.name: MicrophoneProtocolState()
            for microphone in microphones
        }
        self._visual_states: dict[str, VisualState | None] = {
            microphone.context.name: None
            for microphone in microphones
        }

    async def start(self) -> None:
        try:
            await self._stt.start()
            await self._tts.start()
            for microphone in self._microphones:
                logger = _microphone_logger(microphone)
                logger.info("starting persistent microphone session")
                self._tasks.append(asyncio.create_task(self._run_microphone(microphone)))
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        self._closing = True
        await asyncio.gather(*(session.close() for session in tuple(self._input_sessions.values())))
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        for microphone in self._microphones:
            await microphone.close()
        await self._tts.close()
        await self._stt.close()

    async def _run_microphone(self, microphone: Microphone) -> None:
        logger = _microphone_logger(microphone)
        availability_logger = _MicrophoneAvailabilityLogger(logger)
        while not self._closing:
            session = VoiceInputSession(
                manager=self,
                microphone=microphone,
                assistant_text_buffer_characters=self._microphone_assistant_text_buffers[
                    microphone.context.name
                ],
            )
            self._input_sessions[microphone.context.name] = session
            try:
                await supervise_input(
                    input_adapter=VoiceInputAdapter(session),
                    agent=self._agent,
                    context_provider=self._context_provider,
                    bridge_settings=self._bridge_settings,
                    fatal_termination=self._fatal_termination,
                )
                if session.unavailable is None:
                    availability_logger.available()
                    return
                raise session.unavailable
            except asyncio.CancelledError:
                raise
            except MicrophoneUnavailable as exc:
                availability_logger.unavailable(exc)
                await self._recover_microphone_boundary(microphone, logger, exc)
            except Exception as exc:
                logger.exception("microphone adapter failed; recreating boundary")
                await self._recover_microphone_boundary(microphone, logger, exc)
            finally:
                self._input_sessions.pop(microphone.context.name, None)
                await session.close()
                logger.info("microphone session ended")
            if not self._closing:
                await asyncio.sleep(0.5)

    async def _capture_utterance(
        self,
        microphone: Microphone,
        logger: logging.Logger,
        starts_new_conversation: bool,
        timeout_seconds: float | None,
        listen_id: str,
        speech_start_deadline: float | None = None,
    ) -> CapturedUtterance:
        event = await self._wait_for_speech_start(
            microphone,
            logger,
            listen_id,
            timeout_seconds,
            deadline=speech_start_deadline,
        )
        if event is None:
            return CapturedUtterance(captured=False, text_fragments=())

        logger.info("wake_word=%r audio stream started", event.wake_word)
        await self._set_visual_state(microphone, VisualState.LISTENING, "speech_started", logger)
        captured = await self._capture_transcript_and_speaker(
            microphone,
            logger,
            first_event=None,
            audio_start=event,
            recognize_speaker=starts_new_conversation,
        )
        if not captured.captured:
            logger.info("audio stream ended without transcript")
            return captured

        await self._set_visual_state(microphone, VisualState.PROCESSING, "utterance_accepted", logger)
        if starts_new_conversation:
            await self._play_cue(microphone, CueType.UTTERANCE_ACCEPTED, logger)
        return captured

    async def _begin_new_conversation_listening(
        self,
        microphone: Microphone,
        logger: logging.Logger,
    ) -> StartListening:
        await self._set_visual_state(
            microphone, VisualState.IDLE, "ready_for_conversation", logger
        )
        output_event = self._new_conversation_listening_event(microphone)
        logger.debug(
            "opening microphone for new conversation listening mode=%s",
            output_event.mode.value,
        )
        await self._send_command(microphone, output_event, logger)
        await self._await_listening_started(microphone, output_event, logger)
        return output_event

    async def _capture_open_mic_utterance(
        self,
        microphone: Microphone,
        logger: logging.Logger,
        listen_id: str,
    ) -> CapturedUtterance:
        event = await self._wait_for_speech_start(
            microphone,
            logger,
            listen_id,
            timeout_seconds=None,
        )
        if event is None:
            return CapturedUtterance(captured=False, text_fragments=())

        logger.debug(
            "open-mic audio stream started wake_phrase=%r",
            self._open_mic_wake_phrase,
        )
        stt_session = await self._stt.create_streaming_session(microphone.context.name)
        wake_candidate = asyncio.Event()
        partial_task = asyncio.create_task(
            self._collect_open_mic_partials(stt_session, wake_candidate, microphone, logger)
        )
        audio_event_timeout_seconds = self._audio_event_timeout_for(microphone)
        try:
            while True:
                audio_chunks: list[AudioChunk] = []
                final_text = ""
                accepted_text = ""
                while True:
                    try:
                        next_event = await asyncio.wait_for(
                            self._receive_event(microphone, logger),
                            timeout=audio_event_timeout_seconds,
                        )
                    except TimeoutError as error:
                        logger.debug(
                            "open-mic audio stream event timed out timeout_seconds=%.2f chunks=%s bytes=%s "
                            "wake_candidate=%s",
                            audio_event_timeout_seconds,
                            len(audio_chunks),
                            sum(len(chunk.data) for chunk in audio_chunks),
                            wake_candidate.is_set(),
                        )
                        raise MicrophoneUnavailable(
                            f"open-mic audio stream event timed out after {audio_event_timeout_seconds:.2f}s"
                        ) from error
                    if isinstance(next_event, AudioChunk):
                        audio_chunks.append(next_event)
                        await stt_session.send_audio(PcmAudioChunk(data=next_event.data))
                        continue
                    if isinstance(next_event, AudioProgress):
                        logger.debug(
                            "open-mic audio stream progress chunks=%s bytes=%s",
                            next_event.chunks,
                            next_event.bytes,
                        )
                        continue
                    if isinstance(next_event, SpeechEnded):
                        await stt_session.end_audio()
                        break
                    if isinstance(next_event, SpeechStarted):
                        logger.warning("received nested open-mic audio start; ending current stream")
                        await stt_session.end_audio()
                        break
                    logger.warning(
                        "ignored microphone event in open-mic audio stream event=%s",
                        type(next_event).__name__,
                    )

                logger.debug(
                    "open-mic speech segment ended chunks=%s bytes=%s wake_candidate=%s",
                    len(audio_chunks),
                    sum(len(chunk.data) for chunk in audio_chunks),
                    wake_candidate.is_set(),
                )

                partial_had_wake = await partial_task

                final_text = await stt_session.transcribe_final()
                accepted_text = _text_after_wake_phrase(final_text, self._open_mic_wake_phrase) or ""
                if not accepted_text.strip():
                    logger.debug(
                        "open-mic speech discarded wake_phrase_detected=%s final_chars=%s; continuing stream",
                        partial_had_wake,
                        len(final_text),
                    )
                    if partial_had_wake:
                        await self._send_command(
                            microphone,
                            ResetWakeCandidate(listen_id, event.utterance_id),
                            logger,
                        )
                        await self._set_visual_state(
                            microphone, VisualState.IDLE, "wake_candidate_rejected", logger
                        )
                    await stt_session.close()
                    stt_session = await self._stt.create_streaming_session(microphone.context.name)
                    wake_candidate = asyncio.Event()
                    partial_task = asyncio.create_task(
                        self._collect_open_mic_partials(stt_session, wake_candidate, microphone, logger)
                    )
                    event = await self._wait_for_speech_start(microphone, logger, listen_id, None)
                    continue

                await self._set_visual_state(
                    microphone, VisualState.PROCESSING, "open_mic_utterance_accepted", logger
                )
                await self._stop_listening(microphone, listen_id, "utterance_accepted", logger)
                await self._play_cue(microphone, CueType.UTTERANCE_ACCEPTED, logger)

                logger.info(
                    "open-mic utterance accepted listen_id=%s utterance_id=%s wake_phrase=%r utterance_chars=%s",
                    listen_id,
                    event.utterance_id,
                    self._open_mic_wake_phrase,
                    len(accepted_text),
                )
                logger.debug("open_mic_utterance=%r", accepted_text)
                speaker_result = await self._recognize_speaker_from_audio_chunks(event, audio_chunks, logger)
                return CapturedUtterance(
                    captured=True,
                    text_fragments=(accepted_text,),
                    speaker_result=speaker_result,
                )
        except (Exception, asyncio.CancelledError):
            partial_task.cancel()
            with suppress(asyncio.CancelledError):
                await partial_task
            raise
        finally:
            await stt_session.close()

    async def _wait_for_speech_start(
        self,
        microphone: Microphone,
        logger: logging.Logger,
        listen_id: str,
        timeout_seconds: float | None,
        *,
        deadline: float | None = None,
    ) -> SpeechStarted | None:
        logger.debug(
            "waiting for speech start listen_id=%s timeout_seconds=%s",
            listen_id,
            timeout_seconds,
        )
        receive_task = None
        timeout_task = None
        try:
            if timeout_seconds is None:
                if deadline is not None:
                    raise AssertionError("speech-start deadline requires a timeout policy")
                event = await self._receive_event(microphone, logger)
            else:
                if deadline is None:
                    deadline = self._monotonic_time() + timeout_seconds
                    remaining_seconds = timeout_seconds
                else:
                    remaining_seconds = max(0.0, deadline - self._monotonic_time())

                async def receive_committed_event():
                    received = await self._receive_event(microphone, logger)
                    return self._monotonic_time(), received

                receive_task = asyncio.create_task(receive_committed_event())
                timeout_task = asyncio.create_task(asyncio.sleep(remaining_seconds))
                await asyncio.wait((receive_task, timeout_task), return_when=asyncio.FIRST_COMPLETED)
                received_at = None
                received_event = None
                if receive_task.done():
                    received_at, received_event = receive_task.result()
                if received_at is not None and received_at <= deadline:
                    logger.debug(
                        "speech start arbiter listen_id=%s received_at=%.9f deadline=%.9f winner=speech",
                        listen_id,
                        received_at,
                        deadline,
                    )
                    timeout_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await timeout_task
                    event = received_event
                else:
                    logger.debug(
                        "speech start arbiter listen_id=%s received_at=%s deadline=%.9f winner=timeout",
                        listen_id,
                        f"{received_at:.9f}" if received_at is not None else None,
                        deadline,
                    )
                    receive_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await receive_task
                    raise TimeoutError
        except TimeoutError:
            logger.debug("speech start timed out listen_id=%s timeout_seconds=%.2f", listen_id, timeout_seconds or 0.0)
            await self._stop_listening(microphone, listen_id, "speech_start_timeout", logger)
            return None
        finally:
            for task in (receive_task, timeout_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (receive_task, timeout_task):
                if task is not None:
                    with suppress(asyncio.CancelledError):
                        await task
        assert isinstance(event, SpeechStarted), (
            f"expected SpeechStarted for listen_id={listen_id}, received {type(event).__name__}"
        )
        assert event.listen_id == listen_id
        return event

    @staticmethod
    def _monotonic_time() -> float:
        return asyncio.get_running_loop().time()

    async def _capture_transcript_and_speaker(
        self,
        microphone: Microphone,
        logger: logging.Logger,
        first_event,
        audio_start: SpeechStarted | None,
        recognize_speaker: bool,
    ) -> CapturedUtterance:
        stt_session = await self._stt.create_session(microphone.context.name)
        text_task = asyncio.create_task(self._collect_transcript(stt_session, logger))
        speaker_stream = self._start_speaker_recognition(audio_start, recognize_speaker, logger)
        try:
            audio_done = False
            if isinstance(first_event, AudioChunk):
                await stt_session.send_audio(PcmAudioChunk(data=first_event.data))
                if speaker_stream is not None:
                    await speaker_stream.send_audio(first_event)
            elif isinstance(first_event, SpeechEnded):
                await stt_session.end_audio()
                if speaker_stream is not None:
                    await speaker_stream.end_audio()
                audio_done = True
            elif first_event is not None:
                logger.warning("ignored microphone event in audio stream event=%s", type(first_event).__name__)

            while not audio_done:
                next_event = await self._receive_event(microphone, logger)
                if isinstance(next_event, AudioChunk):
                    await stt_session.send_audio(PcmAudioChunk(data=next_event.data))
                    if speaker_stream is not None:
                        await speaker_stream.send_audio(next_event)
                    continue
                if isinstance(next_event, SpeechEnded):
                    await stt_session.end_audio()
                    if speaker_stream is not None:
                        await speaker_stream.end_audio()
                    audio_done = True
                    break
                if isinstance(next_event, SpeechStarted):
                    logger.warning("received nested audio start; ending current stream")
                    await stt_session.end_audio()
                    if speaker_stream is not None:
                        await speaker_stream.end_audio()
                    audio_done = True
                    break

            text_fragments = await text_task
            captured = any(fragment.strip() for fragment in text_fragments)
            speaker_result = await self._await_speaker_result(speaker_stream, logger)
            await stt_session.close()
            return CapturedUtterance(
                captured=captured,
                text_fragments=tuple(text_fragments),
                speaker_result=speaker_result,
            )
        except (Exception, asyncio.CancelledError):
            text_task.cancel()
            with suppress(asyncio.CancelledError):
                await text_task
            if speaker_stream is not None:
                speaker_stream.cancel()
            await stt_session.close()
            raise

    def _start_speaker_recognition(
        self,
        audio_start: SpeechStarted | None,
        recognize_speaker: bool,
        logger: logging.Logger,
    ) -> SpeakerRecognitionStream | None:
        if not recognize_speaker or not self._speaker_recognition.enabled:
            return None
        sample_rate = 16000
        sample_width = 2
        channels = 1
        if audio_start is not None:
            sample_rate = audio_start.rate
            sample_width = audio_start.width
            channels = audio_start.channels
        audio_format = SpeakerRecognitionAudioFormat(
            sample_rate=sample_rate,
            sample_width=sample_width,
            channels=channels,
        )
        logger.debug(
            "starting speaker recognition stream sample_rate=%s sample_width=%s channels=%s",
            audio_format.sample_rate,
            audio_format.sample_width,
            audio_format.channels,
        )
        return self._speaker_recognition.start_stream(audio_format)

    async def _await_speaker_result(
        self,
        speaker_stream: SpeakerRecognitionStream | None,
        logger: logging.Logger,
    ) -> SpeakerRecognitionResult | None:
        if speaker_stream is None:
            return None
        try:
            result = await asyncio.wait_for(
                speaker_stream.result(),
                timeout=self._speaker_recognition.timeout_seconds,
            )
        except TimeoutError:
            logger.warning(
                "speaker recognition did not finish within %.2fs; continuing without recognized user",
                self._speaker_recognition.timeout_seconds,
            )
            speaker_stream.cancel()
            return None
        except Exception as exc:
            logger.warning("speaker recognition failed; continuing without recognized user error=%s", exc)
            return None

        logger.info(
            "speaker recognition result user=%r confidence=%.3f score=%.3f threshold=%.3f profile=%r",
            result.recognized_user,
            result.confidence,
            result.score,
            result.threshold,
            result.profile,
        )
        return result

    async def _recognize_speaker_from_audio_chunks(
        self,
        audio_start: SpeechStarted,
        audio_chunks: list[AudioChunk],
        logger: logging.Logger,
    ) -> SpeakerRecognitionResult | None:
        speaker_stream = self._start_speaker_recognition(audio_start, True, logger)
        if speaker_stream is None:
            return None
        try:
            for chunk in audio_chunks:
                await speaker_stream.send_audio(chunk)
            await speaker_stream.end_audio()
            return await self._await_speaker_result(speaker_stream, logger)
        except Exception:
            speaker_stream.cancel()
            raise

    async def _speak_reply_text(
        self,
        microphone: Microphone,
        text: str,
        logger: logging.Logger,
        on_playback_commit: Callable[[], None] | None = None,
    ) -> None:
        if not text:
            logger.info("empty agent reply; returning to wake-word wait")
            return

        logger.info("agent reply batch ready chars=%s", len(text))
        logger.debug("reply_batch=%r", text)

        logger.info("starting TTS playback")
        await self._speak_tts_text(
            microphone=microphone,
            text=text,
            logger=logger,
            volume=None,
            on_playback_commit=on_playback_commit,
        )

    async def _speak_processing_update(
        self,
        microphone: Microphone,
        logger: logging.Logger,
    ) -> None:
        cue = random.choice(self._processing_update_spoken_cues)
        logger.info("starting processing cue TTS playback cue=%r", cue)
        committed = False

        def playback_committed() -> None:
            nonlocal committed
            committed = True

        await self._await_committed_operation(
            self._speak_tts_text(
                microphone=microphone,
                text=cue,
                logger=logger,
                volume=PROCESSING_UPDATE_VOLUME,
                on_playback_commit=playback_committed,
            ),
            is_committed=lambda: committed,
            logger=logger,
            label="processing update playback",
        )

    async def _speak_tts_text(
        self,
        microphone: Microphone,
        text: str,
        logger: logging.Logger,
        volume: float | None,
        on_playback_commit: Callable[[], None] | None = None,
    ) -> None:
        audio_start_count = 0
        audio_chunk_count = 0
        audio_byte_count = 0
        playback_id = new_id()
        async for audio_event in self._tts.synthesize(text):
            if isinstance(audio_event, SynthesizedAudioStart):
                audio_start_count += 1
                if volume is not None:
                    audio_event = replace(audio_event, volume=volume)
                logger.debug(
                    "TTS audio start count=%s rate=%s width=%s channels=%s",
                    audio_start_count,
                    audio_event.rate,
                    audio_event.width,
                    audio_event.channels,
                )
                await self._send_command(
                    microphone,
                    PlaybackBegin(
                        playback_id=playback_id,
                        rate=audio_event.rate,
                        width=audio_event.width,
                        channels=audio_event.channels,
                        volume=audio_event.volume,
                    ),
                    logger,
                    on_commit=on_playback_commit if audio_start_count == 1 else None,
                )
            elif isinstance(audio_event, SynthesizedAudioChunk):
                audio_chunk_count += 1
                audio_byte_count += len(audio_event.data)
                if audio_chunk_count == 1 or audio_chunk_count % 50 == 0:
                    logger.debug(
                        "TTS audio chunks=%s bytes=%s",
                        audio_chunk_count,
                        audio_byte_count,
                    )
                await self._send_command(microphone, PlaybackChunk(playback_id, audio_event.data), logger)
            elif isinstance(audio_event, SynthesizedAudioEnd):
                logger.debug(
                    "TTS audio end starts=%s chunks=%s bytes=%s",
                    audio_start_count,
                    audio_chunk_count,
                    audio_byte_count,
                )
                await self._send_command(microphone, PlaybackEnd(playback_id), logger)
                event = await self._receive_event(microphone, logger)
                assert event == PlaybackFinished(playback_id)
        logger.info(
            "TTS stream finished starts=%s chunks=%s bytes=%s",
            audio_start_count,
            audio_chunk_count,
            audio_byte_count,
        )

    async def _collect_transcript(
        self,
        stt_session: SttSession,
        logger: logging.Logger,
    ) -> tuple[str, ...]:
        text_fragments: list[str] = []
        while True:
            event = await stt_session.receive_text()
            if isinstance(event, TextFragment):
                if not text_fragments and not event.text.strip():
                    continue
                logger.info("transcription fragment chars=%s", len(event.text))
                logger.debug("transcript_fragment=%r", event.text)
                text_fragments.append(event.text)
                continue
            if isinstance(event, TextEnd):
                return tuple(text_fragments)
            raise ValueError(f"unsupported STT event: {type(event).__name__}")

    async def _collect_open_mic_partials(
        self,
        stt_session: StreamingSttSession,
        wake_candidate: asyncio.Event,
        microphone: Microphone,
        logger: logging.Logger,
    ) -> bool:
        partial_had_wake = False
        while True:
            event = await stt_session.receive_text()
            if isinstance(event, TextPartial):
                logger.debug(
                    "open-mic partial transcript chars=%s audio_start_seconds=%.2f audio_end_seconds=%.2f duration_seconds=%.2f",
                    len(event.text),
                    event.audio_start_seconds,
                    event.audio_end_seconds,
                    event.duration_seconds,
                )
                if _text_after_wake_phrase(event.text, self._open_mic_wake_phrase) is not None:
                    if not partial_had_wake:
                        logger.debug(
                            "open-mic wake phrase candidate detected audio_end_seconds=%.2f",
                            event.audio_end_seconds,
                        )
                        await self._set_visual_state(
                            microphone, VisualState.LISTENING, "open_mic_wake_candidate", logger
                        )
                    partial_had_wake = True
                    wake_candidate.set()
                continue
            if isinstance(event, TextEnd):
                return partial_had_wake
            raise ValueError(f"unsupported streaming STT event: {type(event).__name__}")

    def _follow_up_timeout_for(self, microphone: Microphone) -> float:
        return self._microphone_follow_up_timeouts.get(
            microphone.context.name,
            self._follow_up_timeout_seconds,
        )

    def _audio_start_timeout_for(self, microphone: Microphone) -> float:
        return self._microphone_audio_start_timeouts.get(
            microphone.context.name,
            DEFAULT_AUDIO_START_TIMEOUT_SECONDS,
        )

    def _audio_event_timeout_for(self, microphone: Microphone) -> float:
        return self._microphone_audio_event_timeouts.get(
            microphone.context.name,
            DEFAULT_AUDIO_EVENT_TIMEOUT_SECONDS,
        )

    def _new_conversation_listening_event(self, microphone: Microphone) -> StartListening:
        mode = (
            ListeningMode.OPEN_MIC
            if microphone.context.name in self._open_microphones
            else ListeningMode.WAKE_WORD
        )
        return StartListening(new_id(), mode)

    async def _send_command(
        self,
        microphone: Microphone,
        command: MicrophoneCommand,
        logger: logging.Logger,
        on_commit: Callable[[], None] | None = None,
    ) -> None:
        protocol = self._protocols[microphone.context.name]
        old_state = protocol.snapshot.state
        protocol.command(command)
        if on_commit is not None:
            on_commit()
        logger.debug(
            "microphone command=%s old_state=%s new_state=%s correlations=%s",
            type(command).__name__,
            old_state.value,
            protocol.snapshot.state.value,
            protocol.snapshot,
        )
        driver_send = asyncio.create_task(microphone.send_output_event(command))
        try:
            await asyncio.shield(driver_send)
        except asyncio.CancelledError:
            await asyncio.shield(driver_send)
            raise

    async def _set_visual_state(
        self,
        microphone: Microphone,
        state: VisualState,
        cause: str,
        logger: logging.Logger,
    ) -> None:
        old_state = self._visual_states[microphone.context.name]
        logger.debug(
            "microphone visual transition old=%s new=%s cause=%s correlations=%s",
            old_state.value if old_state is not None else "unknown",
            state.value,
            cause,
            self._protocols[microphone.context.name].snapshot,
        )
        await self._send_command(
            microphone,
            SetVisualState(state),
            logger,
            on_commit=lambda: self._visual_states.__setitem__(microphone.context.name, state),
        )

    async def _receive_event(self, microphone: Microphone, logger: logging.Logger) -> MicrophoneEvent:
        event = await microphone.wait_for_event()
        protocol = self._protocols[microphone.context.name]
        old_state = protocol.snapshot.state
        protocol.event(event)
        logger.debug(
            "microphone event=%s old_state=%s new_state=%s correlations=%s",
            type(event).__name__,
            old_state.value,
            protocol.snapshot.state.value,
            protocol.snapshot,
        )
        return event

    async def _await_listening_started(
        self,
        microphone: Microphone,
        command: StartListening,
        logger: logging.Logger,
    ) -> None:
        event = await asyncio.wait_for(
            self._receive_event(microphone, logger),
            timeout=self._audio_start_timeout_for(microphone),
        )
        assert event == ListeningStarted(command.listen_id, command.mode)

    async def _stop_listening(
        self,
        microphone: Microphone,
        listen_id: str,
        reason: str,
        logger: logging.Logger,
    ) -> None:
        committed = False

        def stop_committed() -> None:
            nonlocal committed
            committed = True

        async def stop_and_drain() -> None:
            await self._send_command(
                microphone,
                StopListening(listen_id, reason),
                logger,
                on_commit=stop_committed,
            )
            event = await self._receive_event(microphone, logger)
            assert event == ListeningStopped(listen_id, reason)

        await self._await_committed_operation(
            stop_and_drain(),
            is_committed=lambda: committed,
            logger=logger,
            label=f"stop listening {listen_id}",
        )

    async def _stop_listening_if_active(
        self,
        microphone: Microphone,
        listen_id: str,
        reason: str,
        logger: logging.Logger,
    ) -> None:
        snapshot = self._protocols[microphone.context.name].snapshot
        if snapshot.listen_id != listen_id:
            return
        if snapshot.state not in (
            DriverState.ARMING,
            DriverState.LISTENING,
            DriverState.CAPTURING,
        ):
            return
        await self._stop_listening(microphone, listen_id, reason, logger)

    async def _play_cue(
        self,
        microphone: Microphone,
        cue_type: CueType,
        logger: logging.Logger,
    ) -> None:
        cue_id = new_id()
        committed = False

        def cue_committed() -> None:
            nonlocal committed
            committed = True

        async def play_and_drain() -> None:
            await self._send_command(
                microphone,
                PlayCue(cue_id, cue_type),
                logger,
                on_commit=cue_committed,
            )
            event = await self._receive_event(microphone, logger)
            assert event == CueFinished(cue_id)

        await self._await_committed_operation(
            play_and_drain(),
            is_committed=lambda: committed,
            logger=logger,
            label=f"cue {cue_type.value}",
        )

    async def _present_follow_up_listening(
        self,
        microphone: Microphone,
        logger: logging.Logger,
    ) -> tuple[StartListening, float]:
        listen = StartListening(new_id(), ListeningMode.FOLLOW_UP)
        command_committed = False

        def command_handoff() -> None:
            nonlocal command_committed
            command_committed = True

        async def present() -> tuple[StartListening, float]:
            await self._send_command(
                microphone,
                listen,
                logger,
                on_commit=command_handoff,
            )
            await self._await_listening_started(microphone, listen, logger)
            return listen, self._monotonic_time()

        operation = asyncio.create_task(present())
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            if not command_committed:
                operation.cancel()
                with suppress(asyncio.CancelledError):
                    await operation
                raise
            committed_listen, _presented_at = await asyncio.shield(operation)
            await self._stop_listening(
                microphone,
                committed_listen.listen_id,
                "follow_up_presentation_cancelled",
                logger,
            )
            raise

    async def _await_committed_operation(
        self,
        operation: Awaitable[Any],
        *,
        is_committed: Callable[[], bool],
        logger: logging.Logger,
        label: str,
    ) -> Any:
        task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if is_committed():
                logger.debug("cancellation waits for committed %s", label)
                await asyncio.shield(task)
            else:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            raise

    async def _recover_microphone_boundary(
        self,
        microphone: Microphone,
        logger: logging.Logger,
        error: BaseException,
    ) -> None:
        snapshot = self._protocols[microphone.context.name].snapshot
        logger.warning(
            "recreating microphone protocol boundary after unavailability state=%s correlations=%s error=%s",
            snapshot.state.value,
            snapshot,
            error,
        )
        await microphone.close()
        self._protocols[microphone.context.name] = MicrophoneProtocolState()
        self._visual_states[microphone.context.name] = None

    @property
    def microphone_count(self) -> int:
        return len(self._microphones)


async def init_mics(
    mic_configs: tuple[MicrophoneConfig, ...],
    stt_config: SttConfig,
    tts_config: TtsConfig,
    conversation_config: ConversationConfig,
    speaker_recognition_config: SpeakerRecognitionConfig,
    agent: Agent,
    *,
    user_settings: dict[str, dict[str, Any]] | None = None,
    context_provider: ContextProvider | None = None,
    fatal_termination: FatalTerminationController | None = None,
    processing_update_spoken_cues: tuple[str, ...] = DEFAULT_PROCESSING_UPDATE_CUES,
    open_mic_wake_phrase: str = "Ryszardzie",
) -> MicrophoneManager | None:
    if not mic_configs:
        return None

    microphones = [create_microphone(mic_config) for mic_config in mic_configs]

    manager = MicrophoneManager(
        microphones=microphones,
        stt=FasterWhisperSpeechToText(stt_config),
        tts=PiperTextToSpeech(tts_config),
        agent=agent,
        follow_up_timeout_seconds=mic_configs[0].follow_up_timeout_seconds,
        microphone_follow_up_timeouts={
            mic_config.name: mic_config.follow_up_timeout_seconds
            for mic_config in mic_configs
        },
        microphone_audio_start_timeouts={
            mic_config.name: mic_config.audio_start_timeout_seconds
            for mic_config in mic_configs
        },
        microphone_audio_event_timeouts={
            mic_config.name: mic_config.audio_event_timeout_seconds
            for mic_config in mic_configs
        },
        open_microphones={
            mic_config.name
            for mic_config in mic_configs
            if mic_config.open_mic
        },
        user_settings=user_settings,
        context_provider=context_provider,
        bridge_settings=BridgeSettings(
            conversation_config.agent_cancellation_deadline_seconds,
            conversation_config.fatal_notification_seconds,
        ),
        fatal_termination=fatal_termination,
        microphone_assistant_text_buffers={
            mic_config.name: mic_config.assistant_text_buffer_characters
            for mic_config in mic_configs
        },
        speaker_recognition=SpeakerRecognitionClient(
            url=speaker_recognition_config.url,
            timeout_seconds=speaker_recognition_config.timeout_seconds,
            profiles=voice_profiles_from_users(user_settings or {}),
        ),
        processing_update_spoken_cues=processing_update_spoken_cues,
        open_mic_wake_phrase=open_mic_wake_phrase,
    )
    await manager.start()
    return manager


def _microphone_logger(microphone: Microphone) -> logging.Logger:
    return logging.getLogger(f"{__name__}.MicrophoneManager[{microphone.context.instance_id}]")


def _microphone_session_logger(microphone: Microphone, session_id: str) -> logging.Logger:
    return logging.getLogger(
        f"{__name__}.MicrophoneManager[{microphone.context.instance_id}].Session[{session_id}]"
    )


def _text_after_wake_phrase(text: str, wake_phrase: str) -> str | None:
    match = re.search(rf"(?iu)(?:^|\b){re.escape(wake_phrase)}\b[\s,.:;!?-]*(.*)", text)
    if match is None:
        return None
    return match.group(1).strip()


class _MicrophoneAvailabilityLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._unavailable = False

    def unavailable(self, error: BaseException) -> None:
        if self._unavailable:
            self._logger.debug("microphone still unavailable; retrying soon error=%s", error)
            return
        self._logger.warning("microphone unavailable; retrying soon error=%s", error)
        self._unavailable = True

    def available(self) -> None:
        if not self._unavailable:
            return
        self._logger.info("microphone available again")
        self._unavailable = False
