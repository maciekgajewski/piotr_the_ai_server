from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from typing import ClassVar

from ai_server.agent import Agent
from ai_server.config import MicrophoneConfig, SttConfig, TtsConfig
from ai_server.messages import UserMessage
from ai_server.microphones.agent_endpoint import MicrophoneAgentEndpoint
from ai_server.microphones.types import MicrophoneDriver, SpeechToText, TextToSpeech


class MicrophoneManager:
    _logger: ClassVar[logging.Logger] = logging.getLogger(f"{__name__}.MicrophoneManager")

    def __init__(
        self,
        drivers: list[MicrophoneDriver],
        stt: SpeechToText,
        tts: TextToSpeech,
        agent: Agent,
        capture_seconds: float,
    ) -> None:
        self._drivers = drivers
        self._stt = stt
        self._tts = tts
        self._agent = agent
        self._capture_seconds = capture_seconds
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await self._stt.start()
        for driver in self._drivers:
            self._logger.info("%s starting persistent microphone session", driver.context.log_prefix)
            self._tasks.append(asyncio.create_task(self._run_microphone(driver)))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

        for driver in self._drivers:
            await driver.close()
        await self._tts.close()
        await self._stt.close()

    async def _run_microphone(self, driver: MicrophoneDriver) -> None:
        endpoint = MicrophoneAgentEndpoint()
        session_id = f"mic-{driver.context.name}-{uuid.uuid4()}"
        agent_task = asyncio.create_task(self._agent.run(endpoint, session_id))
        try:
            while True:
                try:
                    await self._handle_utterance(driver, endpoint)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._logger.exception(
                        "%s utterance handling failed; returning to wake-word wait",
                        driver.context.log_prefix,
                    )
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        finally:
            endpoint.close()
            agent_task.cancel()
            with suppress(asyncio.CancelledError):
                await agent_task
            self._logger.info("%s microphone session ended", driver.context.log_prefix)

    async def _handle_utterance(
        self,
        driver: MicrophoneDriver,
        endpoint: MicrophoneAgentEndpoint,
    ) -> None:
        utterance = await driver.wait_for_utterance(self._capture_seconds)
        self._logger.info(
            "%s wake_word=%r captured bytes=%s",
            driver.context.log_prefix,
            utterance.wake_word,
            utterance.byte_count,
        )
        self._logger.debug("%s audio_chunks=%s", driver.context.log_prefix, len(utterance.audio_chunks))

        transcript = await self._stt.transcribe(utterance)
        self._logger.info("%s transcription complete chars=%s", driver.context.log_prefix, len(transcript))
        self._logger.debug("%s transcript=%r", driver.context.log_prefix, transcript)
        if not transcript:
            self._logger.info("%s empty transcript; returning to wake-word wait", driver.context.log_prefix)
            return

        reply = await endpoint.exchange(UserMessage(text=transcript))
        self._logger.info("%s agent reply ready chars=%s", driver.context.log_prefix, len(reply.text))
        self._logger.debug("%s reply=%r", driver.context.log_prefix, reply.text)

        self._logger.info("%s starting TTS playback", driver.context.log_prefix)
        await self._tts.speak(driver.playback_target, reply.text)
        self._logger.info("%s TTS playback complete", driver.context.log_prefix)

    @property
    def microphone_count(self) -> int:
        return len(self._drivers)


async def init_mics(
    mic_configs: tuple[MicrophoneConfig, ...],
    stt_config: SttConfig,
    tts_config: TtsConfig,
    agent: Agent,
) -> MicrophoneManager | None:
    if not mic_configs:
        return None

    from ai_server.microphones.box3_esphome import Box3EsphomeMicrophoneDriver
    from ai_server.microphones.stt import FasterWhisperSpeechToText
    from ai_server.microphones.tts import PiperTextToSpeech

    drivers: list[MicrophoneDriver] = []
    for mic_config in mic_configs:
        if mic_config.type == "box3_esphome":
            drivers.append(Box3EsphomeMicrophoneDriver.from_config(mic_config))
            continue
        raise ValueError(f"unsupported microphone type: {mic_config.type}")

    manager = MicrophoneManager(
        drivers=drivers,
        stt=FasterWhisperSpeechToText(stt_config),
        tts=PiperTextToSpeech(tts_config),
        agent=agent,
        capture_seconds=stt_config.capture_seconds,
    )
    await manager.start()
    return manager
