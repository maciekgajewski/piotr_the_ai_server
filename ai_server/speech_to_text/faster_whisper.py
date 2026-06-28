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
from ai_server.speech_to_text.messages import TextEnd, TextEvent, TextFragment, TextPartial
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
            "loading STT model model=%s language=%s device=%s compute_type=%s local_files_only=%s beam_size=%s",
            self._config.model,
            self._config.language,
            self._config.device,
            self._config.compute_type,
            self._config.local_files_only,
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

    async def create_streaming_session(self, session_id: str) -> "FasterWhisperStreamingSttSession":
        if self._transcriber is None:
            await self.start()
        assert self._transcriber is not None
        return FasterWhisperStreamingSttSession(
            session_id=session_id,
            transcriber=self._transcriber,
            language=self._config.language,
            final_beam_size=self._config.beam_size,
            partial_beam_size=self._config.partial_beam_size,
            partial_interval_seconds=self._config.partial_interval_seconds,
            partial_window_seconds=self._config.partial_window_seconds,
            partial_max_backlog_seconds=self._config.partial_max_backlog_seconds,
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


class FasterWhisperStreamingSttSession:
    def __init__(
        self,
        session_id: str,
        transcriber: Transcriber,
        language: str,
        final_beam_size: int,
        partial_beam_size: int,
        partial_interval_seconds: float,
        partial_window_seconds: float,
        partial_max_backlog_seconds: float,
        audio_format: PcmAudioFormat,
    ) -> None:
        self._session_id = session_id
        self._transcriber = transcriber
        self._language = language
        self._final_beam_size = final_beam_size
        self._partial_beam_size = partial_beam_size
        self._partial_interval_seconds = partial_interval_seconds
        self._partial_window_seconds = partial_window_seconds
        self._partial_max_backlog_seconds = partial_max_backlog_seconds
        self._audio_format = audio_format
        self._audio = bytearray()
        self._audio_ended = False
        self._closed = False
        self._pending_end = False
        self._last_partial_text = ""
        self._last_partial_audio_end_seconds = 0.0
        self._events: asyncio.Queue[TextPartial | TextEnd] = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._partial_worker())
        self._transcript_preprocessor = TranscriptPreprocessor(session_id)
        self._logger = logging.getLogger(f"{__name__}.FasterWhisperStreamingSttSession[{session_id}]")

    async def send_audio(self, chunk: PcmAudioChunk) -> None:
        assert not self._audio_ended, "cannot send streaming STT audio after end_audio"
        if self._closed:
            return
        self._audio.extend(chunk.data)

    async def end_audio(self) -> None:
        if self._audio_ended:
            return
        self._audio_ended = True

    async def receive_text(self) -> TextPartial | TextEnd:
        if self._pending_end:
            self._pending_end = False
            return TextEnd()
        if self._closed:
            return TextEnd()
        return await self._events.get()

    async def transcribe_final(self) -> str:
        self._audio_ended = True
        if not self._worker_task.done():
            await self._worker_task
        audio = bytes(self._audio)
        return await self._transcribe(
            audio=audio,
            beam_size=self._final_beam_size,
            audio_start_seconds=0.0,
            audio_end_seconds=audio_seconds(len(audio), self._audio_format),
            kind="final",
        )

    async def close(self) -> None:
        self._closed = True
        self._audio_ended = True
        if not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def _partial_worker(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._partial_interval_seconds)
                if self._closed:
                    return
                audio_length = len(self._audio)
                if audio_length <= 0:
                    if self._audio_ended:
                        break
                    continue

                audio_end_seconds = audio_seconds(audio_length, self._audio_format)
                if self._audio_ended and audio_end_seconds <= self._last_partial_audio_end_seconds:
                    break
                window_bytes = int(self._partial_window_seconds * self._audio_format.byte_rate)
                audio_start_byte = max(0, audio_length - window_bytes)
                audio_start_seconds = audio_seconds(audio_start_byte, self._audio_format)
                audio = bytes(self._audio[audio_start_byte:])
                snapshot_end_seconds = audio_end_seconds
                self._last_partial_audio_end_seconds = snapshot_end_seconds
                text = await self._transcribe(
                    audio=audio,
                    beam_size=self._partial_beam_size,
                    audio_start_seconds=audio_start_seconds,
                    audio_end_seconds=audio_end_seconds,
                    kind="partial",
                )
                backlog_seconds = audio_seconds(len(self._audio), self._audio_format) - snapshot_end_seconds
                if backlog_seconds > self._partial_max_backlog_seconds:
                    self._logger.warning(
                        "streaming STT partial backlog_seconds=%.2f max_backlog_seconds=%.2f; dropping stale partial",
                        backlog_seconds,
                        self._partial_max_backlog_seconds,
                    )
                    continue
                if text and text != self._last_partial_text:
                    self._last_partial_text = text
                    await self._events.put(
                        TextPartial(
                            text=text,
                            audio_start_seconds=audio_start_seconds,
                            audio_end_seconds=audio_end_seconds,
                            duration_seconds=audio_end_seconds - audio_start_seconds,
                        )
                    )
                if self._audio_ended:
                    break
        finally:
            await self._events.put(TextEnd())

    async def _transcribe(
        self,
        audio: bytes,
        beam_size: int,
        audio_start_seconds: float,
        audio_end_seconds: float,
        kind: str,
    ) -> str:
        started_at = time.monotonic()
        seconds = audio_seconds(len(audio), self._audio_format)
        self._logger.debug(
            "starting streaming STT %s transcription audio_start_seconds=%.2f audio_end_seconds=%.2f audio_seconds=%.2f bytes=%s",
            kind,
            audio_start_seconds,
            audio_end_seconds,
            seconds,
            len(audio),
        )
        text = await asyncio.to_thread(
            _transcribe_pcm,
            self._transcriber,
            audio,
            self._audio_format,
            self._language,
            beam_size,
        )
        processed_text = self._transcript_preprocessor.preprocess(text.strip())
        self._logger.debug(
            "streaming STT %s transcription finished audio_start_seconds=%.2f audio_end_seconds=%.2f audio_seconds=%.2f chars=%s duration_seconds=%.2f",
            kind,
            audio_start_seconds,
            audio_end_seconds,
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
        local_files_only=config.local_files_only,
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
