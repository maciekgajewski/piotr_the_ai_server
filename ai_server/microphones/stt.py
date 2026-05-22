from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from pathlib import Path
import sys
import tempfile
import wave

from ai_server.config import SttConfig
from ai_server.microphones.types import MicrophoneUtterance


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


class FasterWhisperSpeechToText:
    def __init__(self, config: SttConfig) -> None:
        self._config = config
        self._whisper = None
        self._selected_device: str | None = None
        self._selected_compute_type: str | None = None

    async def start(self) -> None:
        self._configure_cuda_library_path()
        self._whisper, self._selected_device, self._selected_compute_type = await asyncio.to_thread(
            self._load_model
        )

    async def transcribe(self, utterance: MicrophoneUtterance) -> str:
        if self._whisper is None:
            raise RuntimeError("STT model is not loaded")

        with tempfile.TemporaryDirectory(prefix="ai-server-stt-") as tmpdir:
            audio_path = Path(tmpdir) / "utterance.wav"
            _write_wav(audio_path, utterance.audio_chunks)
            return await asyncio.to_thread(self._transcribe_file, audio_path)

    async def close(self) -> None:
        self._whisper = None

    def _load_model(self):
        from faster_whisper import WhisperModel

        if self._config.device == "auto":
            try:
                compute_type = _default_compute_type("cuda")
                return (
                    WhisperModel(self._config.model, device="cuda", compute_type=compute_type),
                    "cuda",
                    compute_type,
                )
            except Exception:
                compute_type = _default_compute_type("cpu")
                return (
                    WhisperModel(self._config.model, device="cpu", compute_type=compute_type),
                    "cpu",
                    compute_type,
                )

        compute_type = _default_compute_type(self._config.device)
        return (
            WhisperModel(self._config.model, device=self._config.device, compute_type=compute_type),
            self._config.device,
            compute_type,
        )

    def _transcribe_file(self, audio_path: Path) -> str:
        segments, _info = self._whisper.transcribe(
            str(audio_path),
            language=self._config.language,
            beam_size=self._config.beam_size,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _configure_cuda_library_path(self) -> None:
        site_packages = (
            Path(sys.prefix)
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        cuda_lib_dirs = [
            site_packages / "nvidia" / "cublas" / "lib",
            site_packages / "nvidia" / "cudnn" / "lib",
            site_packages / "nvidia" / "cuda_nvrtc" / "lib",
        ]
        paths = [str(path) for path in cuda_lib_dirs if path.exists()]
        if not paths:
            return

        existing = os.environ.get("LD_LIBRARY_PATH", "")
        wanted = ":".join(paths)
        if existing.startswith(wanted):
            return
        os.environ["LD_LIBRARY_PATH"] = ":".join([*paths, existing] if existing else paths)


def _default_compute_type(device: str) -> str:
    return "float16" if device == "cuda" else "int8"


def _write_wav(path: Path, chunks: Iterable[bytes]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    byte_count = 0
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE)
        for chunk in chunks:
            writer.writeframes(chunk)
            byte_count += len(chunk)
    return byte_count
