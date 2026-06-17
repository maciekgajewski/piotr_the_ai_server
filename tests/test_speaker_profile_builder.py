from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import wave

import pytest

from ai_server.speaker_recognition.profile_builder import ProfileArraySummary, build_profile, scan_samples


class FakeEmbedder:
    model_source = "fake-ecapa"

    def embed_wav(self, path: Path) -> list[float]:
        index = int(path.stem)
        return [float(index), 1.0, 0.0]


def fake_save_profile(path: Path, embeddings) -> ProfileArraySummary:
    path.write_text(json.dumps({"embeddings": list(embeddings)}), encoding="utf-8")
    return ProfileArraySummary(embedding_dimension=3, sample_embedding_count=len(embeddings))


def write_wav(path: Path, *, rate: int = 16000, channels: int = 1, sample_width: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(rate)
        writer.writeframes(b"\x01\x00" * 16000)


def test_scan_samples_accepts_capture_tool_wavs_and_rejects_wrong_rate(tmp_path: Path) -> None:
    write_wav(tmp_path / "0001.wav")
    write_wav(tmp_path / "0002.wav", rate=8000)

    accepted, rejected = scan_samples(tmp_path)

    assert [sample.path.name for sample in accepted] == ["0001.wav"]
    assert len(rejected) == 1
    assert rejected[0].path.name == "0002.wav"
    assert "expected 16 kHz" in rejected[0].reason


def test_build_profile_writes_profile_metadata_and_manifest(tmp_path: Path) -> None:
    input_dir = tmp_path / "samples"
    output_dir = tmp_path / "profile"
    write_wav(input_dir / "0001.wav")
    write_wav(input_dir / "0002.wav")
    write_wav(input_dir / "bad.wav", channels=2)

    result = build_profile(
        input_dir=input_dir,
        output_dir=output_dir,
        embedder=FakeEmbedder(),
        created_at=datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc),
        save_profile=fake_save_profile,
    )

    assert result.profile_path == output_dir.resolve() / "speaker_profile.npz"
    assert result.metadata_path.exists()
    assert result.manifest_path.exists()
    assert len(result.accepted_samples) == 2
    assert len(result.rejected_samples) == 1

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["model_source"] == "fake-ecapa"
    assert metadata["sample_count"] == 2
    assert metadata["sample_embedding_count"] == 2
    assert metadata["rejected_sample_count"] == 1
    assert metadata["embedding_dimension"] == 3
    assert metadata["profile_file"] == "speaker_profile.npz"

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert [sample["path"] for sample in manifest["accepted_samples"]] == ["0001.wav", "0002.wav"]
    assert manifest["rejected_samples"][0]["path"] == "bad.wav"


def test_build_profile_requires_overwrite_for_existing_profile(tmp_path: Path) -> None:
    input_dir = tmp_path / "samples"
    output_dir = tmp_path / "profile"
    write_wav(input_dir / "0001.wav")
    output_dir.mkdir()
    (output_dir / "speaker_profile.npz").write_text("old", encoding="utf-8")

    with pytest.raises(ValueError, match="use --overwrite"):
        build_profile(
            input_dir=input_dir,
            output_dir=output_dir,
            embedder=FakeEmbedder(),
            save_profile=fake_save_profile,
        )
