from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress

from ai_server.agent import Agent
from ai_server.config import MicrophoneConfig, SttConfig, TtsConfig
from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, UserMessage
from ai_server.microphones.agent_endpoint import MicrophoneAgentEndpoint
from ai_server.microphones.drivers import create_microphone
from ai_server.microphones.interfaces import Microphone, SpeechToText, SttSession, TextToSpeech
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioStart, TextEnd, TextFragment
from ai_server.microphones.stt import WyomingFasterWhisperSpeechToText
from ai_server.microphones.tts import PiperTextToSpeech


class MicrophoneManager:
    def __init__(
        self,
        microphones: list[Microphone],
        stt: SpeechToText,
        tts: TextToSpeech,
        agent: Agent,
    ) -> None:
        self._microphones = microphones
        self._stt = stt
        self._tts = tts
        self._agent = agent
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
        agent_task = asyncio.create_task(self._agent.run(endpoint, session_id))
        try:
            while True:
                try:
                    await self._handle_utterance(microphone, endpoint, logger)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("utterance handling failed; returning to wake-word wait")
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        finally:
            endpoint.close()
            agent_task.cancel()
            with suppress(asyncio.CancelledError):
                await agent_task
            logger.info("microphone session ended")

    async def _handle_utterance(
        self,
        microphone: Microphone,
        endpoint: MicrophoneAgentEndpoint,
        logger: logging.Logger,
    ) -> None:
        event = await microphone.wait_for_event()
        if not isinstance(event, AudioStart):
            logger.warning("ignored microphone event before audio start event=%s", type(event).__name__)
            return

        logger.info("wake_word=%r audio stream started", event.wake_word)
        stt_session = await self._stt.create_session(microphone.context.name)
        text_task = asyncio.create_task(self._forward_text_to_agent(endpoint, stt_session, logger))
        await endpoint.send_to_agent(MessageBegin())
        try:
            while True:
                event = await microphone.wait_for_event()
                if isinstance(event, AudioChunk):
                    await stt_session.send_audio(event)
                    continue
                if isinstance(event, AudioEnd):
                    await stt_session.end_audio()
                    break
                if isinstance(event, AudioStart):
                    logger.warning("received nested audio start; ending current stream")
                    await stt_session.end_audio()
                    break

            await text_task
            await stt_session.close()
            await endpoint.send_to_agent(MessageEnd())
        except Exception:
            text_task.cancel()
            with suppress(asyncio.CancelledError):
                await text_task
            await stt_session.close()
            raise

        reply = await self._receive_agent_reply(endpoint)
        if not reply.text:
            logger.info("empty agent reply; returning to wake-word wait")
            return

        logger.info("agent reply ready chars=%s", len(reply.text))
        logger.debug("reply=%r", reply.text)

        logger.info("starting TTS playback")
        async for audio_event in self._tts.synthesize(reply.text):
            await microphone.send_audio_event(audio_event)
        logger.info("TTS stream finished")

    async def _forward_text_to_agent(
        self,
        endpoint: MicrophoneAgentEndpoint,
        stt_session: SttSession,
        logger: logging.Logger,
    ) -> None:
        while True:
            event = await stt_session.receive_text()
            if isinstance(event, TextFragment):
                logger.info("transcription fragment chars=%s", len(event.text))
                logger.debug("transcript_fragment=%r", event.text)
                await endpoint.send_to_agent(MessageFragment(text=event.text))
                continue
            if isinstance(event, TextEnd):
                return
            raise ValueError(f"unsupported STT event: {type(event).__name__}")

    async def _receive_agent_reply(self, endpoint: MicrophoneAgentEndpoint) -> UserMessage:
        text_parts: list[str] = []
        while True:
            event = await endpoint.receive_reply()
            if isinstance(event, MessageBegin):
                text_parts.clear()
                continue
            if isinstance(event, MessageFragment):
                text_parts.append(event.text)
                continue
            if isinstance(event, MessageEnd):
                return UserMessage(text="".join(text_parts))
            raise ValueError(f"unsupported agent reply event: {type(event).__name__}")

    @property
    def microphone_count(self) -> int:
        return len(self._microphones)


async def init_mics(
    mic_configs: tuple[MicrophoneConfig, ...],
    stt_config: SttConfig,
    tts_config: TtsConfig,
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
    )
    await manager.start()
    return manager


def _microphone_logger(microphone: Microphone) -> logging.Logger:
    return logging.getLogger(f"{__name__}.MicrophoneManager[{microphone.context.instance_id}]")
