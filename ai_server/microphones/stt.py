from __future__ import annotations

import asyncio
import logging
import socket
import time

from ai_server.config import SttConfig
from ai_server.microphones.messages import AudioChunk, TextEnd, TextEvent, TextFragment

try:
    from wyoming.asr import Transcribe, Transcript, TranscriptChunk, TranscriptStop
    from wyoming.audio import AudioChunk as WyomingAudioChunk
    from wyoming.audio import AudioStart as WyomingAudioStart
    from wyoming.audio import AudioStop as WyomingAudioStop
    from wyoming.client import AsyncTcpClient
except ModuleNotFoundError:
    Transcribe = None
    Transcript = None
    TranscriptChunk = None
    TranscriptStop = None
    WyomingAudioChunk = None
    WyomingAudioStart = None
    WyomingAudioStop = None
    AsyncTcpClient = None


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
WYOMING_WHISPER_HOST = "127.0.0.1"
WYOMING_WHISPER_PORT = 10300
WYOMING_WHISPER_START_TIMEOUT_SECONDS = 30.0


class WyomingFasterWhisperSpeechToText:
    def __init__(
        self,
        config: SttConfig,
        server_host: str = WYOMING_WHISPER_HOST,
        server_port: int = WYOMING_WHISPER_PORT,
    ) -> None:
        self._config = config
        self._server_host = server_host
        self._server_port = server_port
        self._started = False
        self._logger = logging.getLogger(
            f"{__name__}.WyomingFasterWhisperSpeechToText[{config.model}:{config.language}]"
        )

    async def start(self) -> None:
        _require_wyoming()
        self._logger.info(
            "connecting to required server model=%s language=%s host=%s port=%s",
            self._config.model,
            self._config.language,
            self._server_host,
            self._server_port,
        )
        await asyncio.to_thread(
            _wait_for_tcp_port,
            self._server_host,
            self._server_port,
            WYOMING_WHISPER_START_TIMEOUT_SECONDS,
        )
        self._started = True
        self._logger.info(
            "server ready host=%s port=%s",
            self._server_host,
            self._server_port,
        )

    async def create_session(self, session_id: str) -> "WyomingFasterWhisperSttSession":
        if not self._started:
            await self.start()

        session = WyomingFasterWhisperSttSession(
            session_id=session_id,
            language=self._config.language,
            server_host=self._server_host,
            server_port=self._server_port,
        )
        await session.start()
        return session

    async def close(self) -> None:
        self._started = False


class WyomingFasterWhisperSttSession:
    def __init__(
        self,
        session_id: str,
        language: str,
        server_host: str,
        server_port: int,
    ) -> None:
        self._session_id = session_id
        self._language = language
        self._server_host = server_host
        self._server_port = server_port
        self._client = AsyncTcpClient(server_host, server_port)
        self._pending_end = False
        self._done = False
        self._logger = logging.getLogger(f"{__name__}.WyomingFasterWhisperSttSession[{session_id}]")

    async def start(self) -> None:
        await self._client.connect()
        await self._client.write_event(Transcribe(language=self._language).event())
        await self._client.write_event(
            WyomingAudioStart(rate=SAMPLE_RATE, width=SAMPLE_WIDTH_BYTES, channels=CHANNELS).event()
        )

    async def send_audio(self, chunk: AudioChunk) -> None:
        if self._done:
            return
        await self._client.write_event(
            WyomingAudioChunk(
                rate=SAMPLE_RATE,
                width=SAMPLE_WIDTH_BYTES,
                channels=CHANNELS,
                audio=chunk.data,
            ).event()
        )

    async def end_audio(self) -> None:
        if self._done:
            return
        await self._client.write_event(WyomingAudioStop().event())

    async def receive_text(self) -> TextEvent:
        if self._pending_end:
            self._pending_end = False
            self._done = True
            return TextEnd()

        while True:
            event = await self._client.read_event()
            if event is None:
                self._done = True
                return TextEnd()

            if TranscriptChunk.is_type(event.type):
                transcript = TranscriptChunk.from_event(event)
                if transcript.text:
                    return TextFragment(text=transcript.text)
                continue

            if Transcript.is_type(event.type):
                transcript = Transcript.from_event(event)
                self._pending_end = True
                if transcript.text:
                    return TextFragment(text=transcript.text.strip())
                continue

            if TranscriptStop.is_type(event.type):
                self._done = True
                return TextEnd()

    async def close(self) -> None:
        self._done = True
        await self._client.disconnect()


def _require_wyoming() -> None:
    if AsyncTcpClient is None:
        raise RuntimeError("wyoming package is required for Wyoming Faster Whisper STT")


def _can_connect(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_tcp_port(host: str, port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _can_connect(host, port):
            return
        time.sleep(0.1)

    raise TimeoutError(f"required Wyoming Faster Whisper server was not reachable on {host}:{port} within {timeout:.1f}s")
