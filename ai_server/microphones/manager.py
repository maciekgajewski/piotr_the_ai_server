from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from ai_server.agent import Agent
from ai_server.config import ConversationConfig, MicrophoneConfig, SpeakerRecognitionConfig, SttConfig, TtsConfig
from ai_server.messages import ConversationEnded, MessageBegin, MessageEnd, MessageFragment, NewConversation, RequestFollowUp
from ai_server.messages import TextMessage, WaitForNewConversation, WaitForNewMessage
from ai_server.microphones.agent_endpoint import MicrophoneAgentEndpoint
from ai_server.microphones.drivers import create_microphone
from ai_server.microphones.interfaces import Microphone, MicrophoneUnavailable, SpeechToText, SttSession, TextToSpeech
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioStart, ConversationTimeoutCue, MessageEndCue
from ai_server.microphones.messages import StartFollowUpListening, StartWakeWordListening, TextEnd, TextFragment
from ai_server.microphones.stt import WyomingFasterWhisperSpeechToText
from ai_server.microphones.tts import PiperTextToSpeech
from ai_server.sessions import Session
from ai_server.speaker_recognition.client import SpeakerRecognitionAudioFormat, SpeakerRecognitionClient
from ai_server.speaker_recognition.client import SpeakerRecognitionResult, SpeakerRecognitionStream
from ai_server.speaker_recognition.client import voice_profiles_from_users
from ai_server.user_settings import UserSettingsProvider


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
        microphone_follow_up_timeouts: dict[str, float] | None = None,
        default_user: str | None = None,
        user_settings: dict[str, dict[str, Any]] | None = None,
        user_settings_provider: UserSettingsProvider | None = None,
        speaker_recognition: SpeakerRecognitionClient | None = None,
    ) -> None:
        self._microphones = microphones
        self._stt = stt
        self._tts = tts
        self._agent = agent
        self._follow_up_timeout_seconds = follow_up_timeout_seconds
        self._microphone_follow_up_timeouts = dict(microphone_follow_up_timeouts or {})
        self._default_user = default_user
        self._user_settings = dict(user_settings or {})
        self._user_settings_provider = user_settings_provider
        self._speaker_recognition = speaker_recognition or SpeakerRecognitionClient(
            url=None,
            timeout_seconds=1.0,
            profiles={},
        )
        self._tasks: list[asyncio.Task[None]] = []

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
        for task in self._tasks:
            task.cancel()
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
        endpoint = MicrophoneAgentEndpoint()
        session_id = f"mic-{microphone.context.name}-{uuid.uuid4()}"
        attributes = {}
        if microphone.context.area:
            attributes["area"] = microphone.context.area
        session = Session(
            session_id=session_id,
            endpoint=endpoint,
            attributes=attributes,
            default_user=self._default_user,
            user_settings=self._user_settings,
            user_settings_provider=self._user_settings_provider,
        )
        session_task = asyncio.create_task(session.run(self._agent))
        availability_logger = _MicrophoneAvailabilityLogger(logger)
        pending_event = None
        pending_reply: TextMessage | None = None
        try:
            while True:
                event = None
                try:
                    if pending_reply is not None:
                        await self._speak_reply(microphone, pending_reply, logger)
                        availability_logger.available()
                        pending_reply = None
                        pending_event = None
                        continue

                    event = pending_event
                    if event is None:
                        event = await endpoint.receive_from_session()
                    if isinstance(event, WaitForNewConversation):
                        logger.debug("opening microphone for wake-word listening")
                        await microphone.send_output_event(StartWakeWordListening())
                        availability_logger.available()
                        captured = await self._capture_utterance(
                            microphone=microphone,
                            endpoint=endpoint,
                            logger=logger,
                            starts_new_conversation=True,
                            timeout_seconds=None,
                        )
                        if not captured.captured:
                            logger.info("wake-word stream had no transcript; ending conversation")
                            await endpoint.send_to_session(ConversationEnded())
                        pending_event = None
                        continue
                    if isinstance(event, (RequestFollowUp, WaitForNewMessage)):
                        follow_up_timeout = self._follow_up_timeout_for(microphone)
                        logger.debug("opening microphone for follow-up timeout_seconds=%s", follow_up_timeout)
                        await microphone.send_output_event(StartFollowUpListening())
                        availability_logger.available()
                        captured = await self._capture_utterance(
                            microphone=microphone,
                            endpoint=endpoint,
                            logger=logger,
                            starts_new_conversation=False,
                            timeout_seconds=follow_up_timeout,
                        )
                        if not captured.captured:
                            logger.info("follow-up timed out; ending conversation")
                            await microphone.send_output_event(ConversationTimeoutCue())
                            await endpoint.send_to_session(ConversationEnded())
                        pending_event = None
                        continue
                    if isinstance(event, MessageBegin):
                        reply = await self._receive_agent_reply(endpoint, first_event=event)
                        pending_reply = reply
                        await self._speak_reply(microphone, reply, logger)
                        availability_logger.available()
                        pending_reply = None
                        pending_event = None
                        continue

                    raise ValueError(f"unsupported session event: {type(event).__name__}")
                except asyncio.CancelledError:
                    raise
                except MicrophoneUnavailable as error:
                    if pending_reply is None:
                        pending_event = event
                    availability_logger.unavailable(error)
                    await asyncio.sleep(0.5)
                except Exception:
                    logger.exception("microphone conversation handling failed; returning to wake-word wait")
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        finally:
            endpoint.close()
            session_task.cancel()
            with suppress(asyncio.CancelledError):
                await session_task
            logger.info("microphone session ended")

    async def _capture_utterance(
        self,
        microphone: Microphone,
        endpoint: MicrophoneAgentEndpoint,
        logger: logging.Logger,
        starts_new_conversation: bool,
        timeout_seconds: float | None,
    ) -> CapturedUtterance:
        event = await self._wait_for_audio_start(microphone, logger, timeout_seconds)
        if event is None:
            return CapturedUtterance(captured=False, text_fragments=())

        logger.info("wake_word=%r audio stream started", event.wake_word)
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

        if starts_new_conversation:
            attributes = {}
            if captured.speaker_result is not None and captured.speaker_result.recognized_user:
                attributes["user"] = captured.speaker_result.recognized_user
            await endpoint.send_to_session(NewConversation(attributes=attributes))
        await self._send_captured_text(endpoint, captured.text_fragments)

        await microphone.send_output_event(MessageEndCue())
        return captured

    async def _wait_for_audio_start(
        self,
        microphone: Microphone,
        logger: logging.Logger,
        timeout_seconds: float | None,
    ) -> AudioStart | None:
        while True:
            try:
                if timeout_seconds is None:
                    event = await microphone.wait_for_event()
                else:
                    event = await asyncio.wait_for(microphone.wait_for_event(), timeout=timeout_seconds)
            except TimeoutError:
                return None

            if isinstance(event, AudioStart):
                return event
            logger.warning("ignored microphone event before audio start event=%s", type(event).__name__)

    async def _send_transcript_message(
        self,
        microphone: Microphone,
        endpoint: MicrophoneAgentEndpoint,
        logger: logging.Logger,
    ) -> bool:
        event = await microphone.wait_for_event()
        if not isinstance(event, AudioStart):
            captured = await self._capture_transcript_and_speaker(
                microphone,
                logger,
                first_event=event,
                audio_start=None,
                recognize_speaker=False,
            )
            await self._send_captured_text(endpoint, captured.text_fragments)
            return captured.captured

        logger.info("wake_word=%r audio stream started", event.wake_word)
        captured = await self._capture_transcript_and_speaker(
            microphone,
            logger,
            first_event=None,
            audio_start=event,
            recognize_speaker=False,
        )
        await self._send_captured_text(endpoint, captured.text_fragments)
        return captured.captured

    async def _capture_transcript_and_speaker(
        self,
        microphone: Microphone,
        logger: logging.Logger,
        first_event,
        audio_start: AudioStart | None,
        recognize_speaker: bool,
    ) -> CapturedUtterance:
        stt_session = await self._stt.create_session(microphone.context.name)
        text_task = asyncio.create_task(self._collect_transcript(stt_session, logger))
        speaker_stream = self._start_speaker_recognition(audio_start, recognize_speaker, logger)
        try:
            audio_done = False
            if isinstance(first_event, AudioChunk):
                await stt_session.send_audio(first_event)
                if speaker_stream is not None:
                    await speaker_stream.send_audio(first_event)
            elif isinstance(first_event, AudioEnd):
                await stt_session.end_audio()
                if speaker_stream is not None:
                    await speaker_stream.end_audio()
                audio_done = True
            elif first_event is not None:
                logger.warning("ignored microphone event in audio stream event=%s", type(first_event).__name__)

            while not audio_done:
                next_event = await microphone.wait_for_event()
                if isinstance(next_event, AudioChunk):
                    await stt_session.send_audio(next_event)
                    if speaker_stream is not None:
                        await speaker_stream.send_audio(next_event)
                    continue
                if isinstance(next_event, AudioEnd):
                    await stt_session.end_audio()
                    if speaker_stream is not None:
                        await speaker_stream.end_audio()
                    audio_done = True
                    break
                if isinstance(next_event, AudioStart):
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
        except Exception:
            text_task.cancel()
            with suppress(asyncio.CancelledError):
                await text_task
            if speaker_stream is not None:
                speaker_stream.cancel()
            await stt_session.close()
            raise

    def _start_speaker_recognition(
        self,
        audio_start: AudioStart | None,
        recognize_speaker: bool,
        logger: logging.Logger,
    ) -> SpeakerRecognitionStream | None:
        if not recognize_speaker or not self._speaker_recognition.enabled:
            return None
        sample_rate = 16000
        sample_width = 2
        channels = 1
        if audio_start is not None:
            sample_rate = audio_start.rate or sample_rate
            sample_width = audio_start.width or sample_width
            channels = audio_start.channels or channels
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

    async def _send_captured_text(
        self,
        endpoint: MicrophoneAgentEndpoint,
        text_fragments: tuple[str, ...],
    ) -> None:
        if not text_fragments:
            return
        await endpoint.send_to_session(MessageBegin())
        for fragment in text_fragments:
            await endpoint.send_to_session(MessageFragment(text=fragment))
        await endpoint.send_to_session(MessageEnd())

    async def _speak_reply(
        self,
        microphone: Microphone,
        reply: TextMessage,
        logger: logging.Logger,
    ) -> None:
        if not reply.text:
            logger.info("empty agent reply; returning to wake-word wait")
            return

        logger.info("agent reply ready chars=%s", len(reply.text))
        logger.debug("reply=%r", reply.text)

        logger.info("starting TTS playback")
        audio_start_count = 0
        audio_chunk_count = 0
        audio_byte_count = 0
        async for audio_event in self._tts.synthesize(reply.text):
            if isinstance(audio_event, AudioStart):
                audio_start_count += 1
                logger.debug(
                    "TTS audio start count=%s rate=%s width=%s channels=%s",
                    audio_start_count,
                    audio_event.rate,
                    audio_event.width,
                    audio_event.channels,
                )
            elif isinstance(audio_event, AudioChunk):
                audio_chunk_count += 1
                audio_byte_count += len(audio_event.data)
                if audio_chunk_count == 1 or audio_chunk_count % 50 == 0:
                    logger.debug(
                        "TTS audio chunks=%s bytes=%s",
                        audio_chunk_count,
                        audio_byte_count,
                    )
            elif isinstance(audio_event, AudioEnd):
                logger.debug(
                    "TTS audio end starts=%s chunks=%s bytes=%s",
                    audio_start_count,
                    audio_chunk_count,
                    audio_byte_count,
                )
            await microphone.send_output_event(audio_event)
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

    async def _receive_agent_reply(
        self,
        endpoint: MicrophoneAgentEndpoint,
        first_event: MessageBegin,
    ) -> TextMessage:
        text_parts: list[str] = []
        event = first_event
        while True:
            if isinstance(event, MessageBegin):
                text_parts.clear()
            elif isinstance(event, MessageFragment):
                text_parts.append(event.text)
            elif isinstance(event, MessageEnd):
                return TextMessage(text="".join(text_parts))
            else:
                raise ValueError(f"unsupported agent reply event: {type(event).__name__}")

            event = await endpoint.receive_from_session()

    def _follow_up_timeout_for(self, microphone: Microphone) -> float:
        return self._microphone_follow_up_timeouts.get(
            microphone.context.name,
            self._follow_up_timeout_seconds,
        )

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
    default_user: str | None = None,
    user_settings: dict[str, dict[str, Any]] | None = None,
    user_settings_provider: UserSettingsProvider | None = None,
) -> MicrophoneManager | None:
    if not mic_configs:
        return None

    microphones = [create_microphone(mic_config) for mic_config in mic_configs]

    manager = MicrophoneManager(
        microphones=microphones,
        stt=WyomingFasterWhisperSpeechToText(stt_config),
        tts=PiperTextToSpeech(tts_config),
        agent=agent,
        follow_up_timeout_seconds=conversation_config.follow_up_timeout_seconds,
        microphone_follow_up_timeouts={
            mic_config.name: mic_config.follow_up_timeout_seconds
            for mic_config in mic_configs
        },
        default_user=default_user,
        user_settings=user_settings,
        user_settings_provider=user_settings_provider,
        speaker_recognition=SpeakerRecognitionClient(
            url=speaker_recognition_config.url,
            timeout_seconds=speaker_recognition_config.timeout_seconds,
            profiles=voice_profiles_from_users(user_settings or {}),
        ),
    )
    await manager.start()
    return manager


def _microphone_logger(microphone: Microphone) -> logging.Logger:
    return logging.getLogger(f"{__name__}.MicrophoneManager[{microphone.context.instance_id}]")


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
