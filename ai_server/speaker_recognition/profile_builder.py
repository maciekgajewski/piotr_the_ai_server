from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
from typing import Any, Protocol
import wave

from ai_server.speaker_recognition.speechbrain_ecapa import SpeechBrainEcapaEmbedder


PROFILE_FORMAT_VERSION = 1
PROFILE_FILENAME = "speaker_profile.npz"
METADATA_FILENAME = "metadata.json"
MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True)
class WavSample:
    path: Path
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width: int
    sha256: str


@dataclass(frozen=True)
class RejectedSample:
    path: Path
    reason: str


@dataclass(frozen=True)
class ProfileArraySummary:
    embedding_dimension: int
    sample_embedding_count: int


@dataclass(frozen=True)
class BuildResult:
    profile_path: Path
    metadata_path: Path
    manifest_path: Path
    accepted_samples: tuple[WavSample, ...]
    rejected_samples: tuple[RejectedSample, ...]
    embedding_dimension: int


class SpeakerEmbedder(Protocol):
    model_source: str

    def embed_wav(self, path: Path) -> Any:
        raise NotImplementedError


def build_profile(
    input_dir: Path,
    output_dir: Path,
    embedder: SpeakerEmbedder,
    *,
    overwrite: bool = False,
    created_at: datetime | None = None,
    save_profile: Callable[[Path, Sequence[Any]], ProfileArraySummary] | None = None,
) -> BuildResult:
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if not input_dir.is_dir():
        raise ValueError(f"input directory does not exist: {input_dir}")

    profile_path = output_dir / PROFILE_FILENAME
    metadata_path = output_dir / METADATA_FILENAME
    manifest_path = output_dir / MANIFEST_FILENAME
    if not overwrite:
        existing = [path for path in (profile_path, metadata_path, manifest_path) if path.exists()]
        if existing:
            joined = ", ".join(str(path) for path in existing)
            raise ValueError(f"profile output already exists; use --overwrite to replace: {joined}")

    accepted, rejected = scan_samples(input_dir)
    if not accepted:
        raise ValueError(f"no usable 16 kHz mono 16-bit WAV samples found in {input_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    embeddings = [embedder.embed_wav(sample.path) for sample in accepted]
    save_profile = save_profile or save_profile_npz
    array_summary = save_profile(profile_path, embeddings)
    created = created_at or datetime.now(timezone.utc)

    metadata = {
        "profile_format_version": PROFILE_FORMAT_VERSION,
        "model_source": embedder.model_source,
        "created_at": created.isoformat(),
        "sample_count": len(accepted),
        "sample_embedding_count": array_summary.sample_embedding_count,
        "rejected_sample_count": len(rejected),
        "total_sample_seconds": round(sum(sample.duration_seconds for sample in accepted), 3),
        "embedding_dimension": array_summary.embedding_dimension,
        "profile_file": PROFILE_FILENAME,
        "manifest_file": MANIFEST_FILENAME,
    }
    manifest = {
        "profile_format_version": PROFILE_FORMAT_VERSION,
        "input_dir": str(input_dir),
        "accepted_samples": [sample_to_json(sample, input_dir) for sample in accepted],
        "rejected_samples": [rejected_sample_to_json(sample, input_dir) for sample in rejected],
    }
    write_json(metadata_path, metadata)
    write_json(manifest_path, manifest)

    return BuildResult(
        profile_path=profile_path,
        metadata_path=metadata_path,
        manifest_path=manifest_path,
        accepted_samples=tuple(accepted),
        rejected_samples=tuple(rejected),
        embedding_dimension=array_summary.embedding_dimension,
    )


def scan_samples(input_dir: Path) -> tuple[tuple[WavSample, ...], tuple[RejectedSample, ...]]:
    accepted: list[WavSample] = []
    rejected: list[RejectedSample] = []
    for path in sorted(input_dir.glob("*.wav")):
        try:
            accepted.append(validate_wav_sample(path))
        except ValueError as exc:
            rejected.append(RejectedSample(path=path, reason=str(exc)))
    return tuple(accepted), tuple(rejected)


def validate_wav_sample(path: Path) -> WavSample:
    try:
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frames = reader.getnframes()
    except (EOFError, OSError, wave.Error) as exc:
        raise ValueError(f"invalid WAV: {exc}") from exc

    if channels != 1:
        raise ValueError(f"expected mono WAV, got channels={channels}")
    if sample_width != 2:
        raise ValueError(f"expected 16-bit PCM WAV, got sample_width={sample_width}")
    if sample_rate != 16000:
        raise ValueError(f"expected 16 kHz WAV, got sample_rate={sample_rate}")
    if frames <= 0:
        raise ValueError("empty WAV")

    return WavSample(
        path=path.resolve(),
        duration_seconds=frames / sample_rate,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        sha256=sha256_file(path),
    )


def save_profile_npz(path: Path, embeddings: Sequence[Any]) -> ProfileArraySummary:
    np = require_numpy()
    sample_embeddings = np.vstack([np.asarray(embedding, dtype=np.float32).reshape(1, -1) for embedding in embeddings])
    normalized_samples = normalize_rows(sample_embeddings, np)
    profile_embedding = normalized_samples.mean(axis=0)
    profile_embedding = profile_embedding / max(float(np.linalg.norm(profile_embedding)), 1.0e-12)
    np.savez(
        path,
        profile_format_version=np.asarray([PROFILE_FORMAT_VERSION], dtype=np.int32),
        profile_embedding=profile_embedding.astype(np.float32),
        sample_embeddings=normalized_samples.astype(np.float32),
    )
    return ProfileArraySummary(
        embedding_dimension=int(profile_embedding.shape[0]),
        sample_embedding_count=int(sample_embeddings.shape[0]),
    )


def normalize_rows(values: Any, np: Any) -> Any:
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    norms = np.maximum(norms, 1.0e-12)
    return values / norms


def require_numpy() -> Any:
    try:
        return importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError(
            "NumPy is required for speaker profile output. Run this inside the "
            "speaker-recognition container or install requirements-speaker-recognition.txt."
        ) from exc


def sample_to_json(sample: WavSample, input_dir: Path) -> dict[str, Any]:
    return {
        "path": display_path(sample.path, input_dir),
        "duration_seconds": round(sample.duration_seconds, 3),
        "sample_rate": sample.sample_rate,
        "channels": sample.channels,
        "sample_width": sample.sample_width,
        "sha256": sample.sha256,
    }


def rejected_sample_to_json(sample: RejectedSample, input_dir: Path) -> dict[str, Any]:
    return {
        "path": display_path(sample.path, input_dir),
        "reason": sample.reason,
    }


def display_path(path: Path, base_dir: Path) -> str:
    try:
        return str(path.resolve().relative_to(base_dir.resolve()))
    except ValueError:
        return str(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a SpeechBrain ECAPA-TDNN speaker profile from WAV samples.")
    parser.add_argument("input_dir", type=Path, help="Directory containing captured 16 kHz mono WAV samples.")
    parser.add_argument("output_dir", type=Path, help="Directory where the speaker profile files will be written.")
    parser.add_argument(
        "--model-source",
        default="speechbrain/spkrec-ecapa-voxceleb",
        help="SpeechBrain pretrained model source.",
    )
    parser.add_argument("--model-savedir", type=Path, default=None, help="Directory for SpeechBrain model files.")
    parser.add_argument("--device", default="cpu", help="Torch device, for example cpu or cuda:0.")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing profile files in the output directory.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    embedder = SpeechBrainEcapaEmbedder(
        model_source=args.model_source,
        model_savedir=args.model_savedir,
        device=args.device,
    )
    result = build_profile(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        embedder=embedder,
        overwrite=args.overwrite,
    )
    print(f"profile={result.profile_path}")
    print(f"metadata={result.metadata_path}")
    print(f"manifest={result.manifest_path}")
    print(
        "accepted_samples="
        f"{len(result.accepted_samples)} rejected_samples={len(result.rejected_samples)} "
        f"embedding_dimension={result.embedding_dimension}"
    )


if __name__ == "__main__":
    main()
