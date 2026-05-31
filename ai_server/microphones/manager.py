from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress

from ai_server.agent import Agent
from ai_server.config import ConversationConfig, MicrophoneConfig, SttConfig, TtsConfig
from ai_server.messages import ConversationEnded, MessageBegin, MessageEnd, MessageFragment, NewConversation, TextMessage
from ai_server.messages import WaitForNewConversation, WaitForNewMessage
from ai_server.microphones.agent_endpoint import MicrophoneAgentEndpoint
from ai_server.microphones.drivers import create_microphone
from ai_server.microphones.interfaces import Microphone, MicrophoneUnavailable, SpeechToText, SttSession, TextToSpeech
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioStart, ConversationTimeoutCue, MessageEndCue
from ai_server.microphones.messages import StartFollowUpListening, StartWakeWordListening, TextEnd, TextFragment
from ai_server.microphones.stt import WyomingFasterWhisperSpeechToText
from ai_server.microphones.tts import PiperTextToSpeech
from ai_server.sessions import Session


class MicrophoneManager:
    def __init__(
        self,
        microphones: list[Microphone],
        stt: SpeechToText,
        tts: TextToSpeech,
        agent: Agent,
        follow_up_timeout_seconds: float,
        microphone_follow_up_timeouts: dict[str, float] | None = None,
    ) -> None:
        self._microphones = microphones
        self._stt = stt
        self._tts = tts
        self._agent = agent
        self._follow_up_timeout_seconds = follow_up_timeout_seconds
        self._microphone_follow_up_timeouts = dict(microphone_follow_up_timeouts or {})
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
        session = Session(session_id=session_id, endpoint=endpoint, attributes=attributes)
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
                        if not captured:
                            logger.info("wake-word stream had no transcript; ending conversation")
                            await endpoint.send_to_session(ConversationEnded())
                        pending_event = None
                        continue
                    if isinstance(event, WaitForNewMessage):
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
                        if not captured:
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
    ) -> bool:
        event = await self._wait_for_audio_start(microphone, logger, timeout_seconds)
        if event is None:
            return False

        if starts_new_conversation:
            await endpoint.send_to_session(NewConversation(attributes={}))

        logger.info("wake_word=%r audio stream started", event.wake_word)
        captured = await self._send_transcript_message(microphone, endpoint, logger)
        if not captured:
            logger.info("audio stream ended without transcript")
            return False

        await microphone.send_output_event(MessageEndCue())
        return True

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
            return await self._send_audio_stream_to_stt(microphone, endpoint, logger, first_event=event)

        logger.info("wake_word=%r audio stream started", event.wake_word)
        return await self._send_audio_stream_to_stt(microphone, endpoint, logger, first_event=None)

    async def _send_audio_stream_to_stt(
        self,
        microphone: Microphone,
        endpoint: MicrophoneAgentEndpoint,
        logger: logging.Logger,
        first_event,
    ) -> bool:
        stt_session = await self._stt.create_session(microphone.context.name)
        text_task = asyncio.create_task(self._forward_text_to_agent(endpoint, stt_session, logger))
        try:
            audio_done = False
            if isinstance(first_event, AudioChunk):
                await stt_session.send_audio(first_event)
            elif isinstance(first_event, AudioEnd):
                await stt_session.end_audio()
                audio_done = True
            elif first_event is not None:
                logger.warning("ignored microphone event in audio stream event=%s", type(first_event).__name__)

            while not audio_done:
                next_event = await microphone.wait_for_event()
                if isinstance(next_event, AudioChunk):
                    await stt_session.send_audio(next_event)
                    continue
                if isinstance(next_event, AudioEnd):
                    await stt_session.end_audio()
                    audio_done = True
                    break
                if isinstance(next_event, AudioStart):
                    logger.warning("received nested audio start; ending current stream")
                    await stt_session.end_audio()
                    audio_done = True
                    break

            captured = await text_task
            await stt_session.close()
            return captured
        except Exception:
            text_task.cancel()
            with suppress(asyncio.CancelledError):
                await text_task
            await stt_session.close()
            raise

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

    async def _forward_text_to_agent(
        self,
        endpoint: MicrophoneAgentEndpoint,
        stt_session: SttSession,
        logger: logging.Logger,
    ) -> bool:
        message_started = False
        captured = False
        while True:
            event = await stt_session.receive_text()
            if isinstance(event, TextFragment):
                if not captured and not event.text.strip():
                    continue
                captured = True
                if not message_started:
                    await endpoint.send_to_session(MessageBegin())
                    message_started = True
                logger.info("transcription fragment chars=%s", len(event.text))
                logger.debug("transcript_fragment=%r", event.text)
                await endpoint.send_to_session(MessageFragment(text=event.text))
                continue
            if isinstance(event, TextEnd):
                if message_started:
                    await endpoint.send_to_session(MessageEnd())
                return captured
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
    agent: Agent,
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
