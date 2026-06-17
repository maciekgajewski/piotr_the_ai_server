from __future__ import annotations

import array
import importlib
from pathlib import Path
import sys
from typing import Any
import wave


class SpeechBrainEcapaEmbedder:
    def __init__(
        self,
        *,
        model_source: str = "speechbrain/spkrec-ecapa-voxceleb",
        model_savedir: Path | None = None,
        device: str | None = None,
    ) -> None:
        self.model_source = model_source
        self._torch, encoder_classifier = require_speechbrain_dependencies()
        run_opts = {"device": device} if device else None
        kwargs: dict[str, Any] = {"source": model_source}
        if model_savedir is not None:
            kwargs["savedir"] = str(model_savedir)
        if run_opts is not None:
            kwargs["run_opts"] = run_opts
        self._classifier = encoder_classifier.from_hparams(**kwargs)

    def embed_wav(self, path: Path) -> Any:
        signal = load_pcm16_mono_wav(path, self._torch)
        with self._torch.no_grad():
            embedding = self._classifier.encode_batch(signal)
        return embedding.squeeze().detach().cpu().numpy()


def load_pcm16_mono_wav(path: Path, torch_module: Any) -> Any:
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())
    except (EOFError, OSError, wave.Error) as exc:
        raise ValueError(f"invalid WAV {path}: {exc}") from exc

    if channels != 1:
        raise ValueError(f"{path} must be mono for ECAPA embedding, got channels={channels}")
    if sample_width != 2:
        raise ValueError(f"{path} must be 16-bit PCM for ECAPA embedding, got sample_width={sample_width}")
    if sample_rate != 16000:
        raise ValueError(f"{path} must be 16 kHz for ECAPA embedding, got {sample_rate}")

    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    return torch_module.tensor(samples, dtype=torch_module.float32).unsqueeze(0) / 32768.0


def require_speechbrain_dependencies() -> tuple[Any, Any]:
    try:
        torch = importlib.import_module("torch")
        speaker_module = importlib.import_module("speechbrain.inference.speaker")
    except ImportError as exc:
        raise RuntimeError(
            "SpeechBrain ECAPA-TDNN dependencies are required. Run this inside the "
            "speaker-recognition container or install requirements-speaker-recognition.txt."
        ) from exc
    return torch, speaker_module.EncoderClassifier
