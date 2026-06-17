from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from ai_server.speaker_recognition.profile_builder import PROFILE_FILENAME
from ai_server.speaker_recognition.profile_builder import require_numpy


@dataclass(frozen=True)
class SpeakerProfile:
    name: str
    path: Path
    embedding: Any
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RecognitionResult:
    recognized_user: str | None
    score: float
    confidence: float
    threshold: float
    margin_to_second_best: float | None
    profile_path: Path | None


def load_profiles(profiles_dir: Path) -> tuple[SpeakerProfile, ...]:
    profiles = []
    for profile_path in sorted(profiles_dir.glob(f"*/{PROFILE_FILENAME}")):
        profiles.append(load_profile(profile_path))
    return tuple(profiles)


def load_named_profiles(profile_paths: dict[str, str]) -> tuple[SpeakerProfile, ...]:
    profiles = []
    for user, path in sorted(profile_paths.items()):
        profile = load_profile(Path(path))
        profiles.append(
            SpeakerProfile(
                name=user,
                path=profile.path,
                embedding=profile.embedding,
                metadata=profile.metadata,
            )
        )
    return tuple(profiles)


def load_profile(profile_path: Path) -> SpeakerProfile:
    np = require_numpy()
    data = np.load(profile_path)
    embedding = data["profile_embedding"].astype(np.float32)
    norm = max(float(np.linalg.norm(embedding)), 1.0e-12)
    embedding = embedding / norm
    metadata_path = profile_path.with_name("metadata.json")
    metadata: dict[str, Any] = {}
    if metadata_path.exists():
        with metadata_path.open("r", encoding="utf-8") as stream:
            metadata = json.load(stream)
    return SpeakerProfile(
        name=profile_path.parent.name,
        path=profile_path,
        embedding=embedding,
        metadata=metadata,
    )


def recognize_speaker(
    utterance_embedding: Any,
    profiles: tuple[SpeakerProfile, ...],
    *,
    threshold: float,
) -> RecognitionResult:
    np = require_numpy()
    if not profiles:
        return RecognitionResult(
            recognized_user=None,
            score=0.0,
            confidence=0.0,
            threshold=threshold,
            margin_to_second_best=None,
            profile_path=None,
        )

    embedding = np.asarray(utterance_embedding, dtype=np.float32).reshape(-1)
    embedding = embedding / max(float(np.linalg.norm(embedding)), 1.0e-12)
    scored = sorted(
        ((float(np.dot(embedding, profile.embedding)), profile) for profile in profiles),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_profile = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else None
    margin = None if second_score is None else best_score - second_score
    recognized = best_score >= threshold
    return RecognitionResult(
        recognized_user=best_profile.name if recognized else None,
        score=best_score,
        confidence=score_to_confidence(best_score, threshold),
        threshold=threshold,
        margin_to_second_best=margin,
        profile_path=best_profile.path,
    )


def score_to_confidence(score: float, threshold: float) -> float:
    if score <= threshold:
        return max(0.0, min(0.5, score / max(threshold, 1.0e-12) * 0.5))
    return max(0.5, min(1.0, 0.5 + ((score - threshold) / max(1.0 - threshold, 1.0e-12)) * 0.5))
