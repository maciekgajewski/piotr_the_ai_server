from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from pathlib import Path
import tempfile
import time
from typing import Protocol
import wave

from ai_server.config import SttConfig
from ai_server.speech_to_text.messages import TextEnd, TextEvent, TextFragment
from ai_server.speech_to_text.transcript_preprocessor import TranscriptPreprocessor
from ai_server.speech_to_text.types import DEFAULT_STT_AUDIO_FORMAT, PcmAudioChunk, PcmAudioFormat, audio_seconds

try:
    from faster_whisper import WhisperModel
except ModuleNotFoundError:
    WhisperModel = None


class Transcriber(Protocol):
    def transcribe(self, wav_path: Path, language: str | None, beam_size: int) -> str:
        raise NotImplementedError


TranscriberFactory = Callable[[SttConfig], Transcriber]


class FasterWhisperSpeechToText:
    def __init__(
        self,
        config: SttConfig,
        transcriber_factory: TranscriberFactory | None = None,
        audio_format: PcmAudioFormat = DEFAULT_STT_AUDIO_FORMAT,
    ) -> None:
        self._config = config
        self._transcriber_factory = transcriber_factory or _create_faster_whisper_transcriber
        self._audio_format = audio_format
        self._transcriber: Transcriber | None = None
        self._logger = logging.getLogger(
            f"{__name__}.FasterWhisperSpeechToText[{config.model}:{config.language}]"
        )

    async def start(self) -> None:
        if self._transcriber is not None:
            return

        started_at = time.monotonic()
        self._logger.info(
            "loading STT model model=%s language=%s device=%s compute_type=%s beam_size=%s",
            self._config.model,
            self._config.language,
            self._config.device,
            self._config.compute_type,
            self._config.beam_size,
        )
        self._transcriber = await asyncio.to_thread(self._transcriber_factory, self._config)
        self._logger.info(
            "STT model loaded model=%s language=%s device=%s compute_type=%s duration_seconds=%.2f",
            self._config.model,
            self._config.language,
            self._config.device,
            self._config.compute_type,
            time.monotonic() - started_at,
        )

    async def create_session(self, session_id: str) -> "FasterWhisperSttSession":
        if self._transcriber is None:
            await self.start()
        assert self._transcriber is not None
        return FasterWhisperSttSession(
            session_id=session_id,
            transcriber=self._transcriber,
            language=self._config.language,
            beam_size=self._config.beam_size,
            audio_format=self._audio_format,
        )

    async def close(self) -> None:
        self._transcriber = None


class FasterWhisperSttSession:
    def __init__(
        self,
        session_id: str,
        transcriber: Transcriber,
        language: str,
        beam_size: int,
        audio_format: PcmAudioFormat,
    ) -> None:
        self._session_id = session_id
        self._transcriber = transcriber
        self._language = language
        self._beam_size = beam_size
        self._audio_format = audio_format
        self._audio = bytearray()
        self._audio_ended = False
        self._audio_ended_event = asyncio.Event()
        self._closed = False
        self._pending_end = False
        self._transcription_task: asyncio.Task[str] | None = None
        self._transcript_preprocessor = TranscriptPreprocessor(session_id)
        self._logger = logging.getLogger(f"{__name__}.FasterWhisperSttSession[{session_id}]")

    async def send_audio(self, chunk: PcmAudioChunk) -> None:
        assert not self._audio_ended, "cannot send STT audio after end_audio"
        if self._closed:
            return
        self._audio.extend(chunk.data)

    async def end_audio(self) -> None:
        if self._audio_ended:
            return
        self._audio_ended = True
        audio = bytes(self._audio)
        self._audio.clear()
        self._transcription_task = asyncio.create_task(self._transcribe(audio))
        self._audio_ended_event.set()

    async def receive_text(self) -> TextEvent:
        if self._pending_end:
            self._pending_end = False
            return TextEnd()

        if self._closed:
            return TextEnd()

        if not self._audio_ended:
            await self._audio_ended_event.wait()

        assert self._transcription_task is not None
        text = await self._transcription_task
        self._pending_end = True
        if text:
            return TextFragment(text=text)
        return TextEnd()

    async def close(self) -> None:
        self._closed = True
        if self._transcription_task is not None and not self._transcription_task.done():
            self._transcription_task.cancel()
            try:
                await self._transcription_task
            except asyncio.CancelledError:
                pass

    async def _transcribe(self, audio: bytes) -> str:
        started_at = time.monotonic()
        seconds = audio_seconds(len(audio), self._audio_format)
        self._logger.info(
            "starting STT transcription audio_seconds=%.2f bytes=%s",
            seconds,
            len(audio),
        )
        text = await asyncio.to_thread(
            _transcribe_pcm,
            self._transcriber,
            audio,
            self._audio_format,
            self._language,
            self._beam_size,
        )
        processed_text = self._transcript_preprocessor.preprocess(text.strip())
        self._logger.info(
            "STT transcription finished audio_seconds=%.2f chars=%s duration_seconds=%.2f",
            seconds,
            len(processed_text),
            time.monotonic() - started_at,
        )
        return processed_text


class FasterWhisperTranscriber:
    def __init__(self, model) -> None:
        self._model = model

    def transcribe(self, wav_path: Path, language: str | None, beam_size: int) -> str:
        segments, _info = self._model.transcribe(
            str(wav_path),
            language=language,
            beam_size=beam_size,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()


def _create_faster_whisper_transcriber(config: SttConfig) -> FasterWhisperTranscriber:
    if WhisperModel is None:
        raise RuntimeError("faster-whisper package is required for in-process STT")
    model = WhisperModel(
        config.model,
        device=config.device,
        compute_type=config.compute_type,
    )
    return FasterWhisperTranscriber(model)


def _transcribe_pcm(
    transcriber: Transcriber,
    audio: bytes,
    audio_format: PcmAudioFormat,
    language: str,
    beam_size: int,
) -> str:
    with tempfile.TemporaryDirectory(prefix="ai-server-stt-") as tmpdir:
        wav_path = Path(tmpdir) / "speech.wav"
        with wave.open(str(wav_path), "wb") as writer:
            writer.setframerate(audio_format.rate)
            writer.setsampwidth(audio_format.width)
            writer.setnchannels(audio_format.channels)
            writer.writeframes(audio)
        return transcriber.transcribe(wav_path, language, beam_size)
