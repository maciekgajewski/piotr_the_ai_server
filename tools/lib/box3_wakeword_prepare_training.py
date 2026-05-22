#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
import shutil
import wave
from pathlib import Path

from mmap_ninja.ragged import RaggedMmap
import numpy as np
import yaml

from microwakeword.audio.spectrograms import SpectrogramGeneration


DEFAULT_TRAINING_STEPS = 1500


class LocalWavClips:
    def __init__(self, samples_dir: Path, split_count: float = 0.1, random_seed: int = 10) -> None:
        paths = sorted(samples_dir.glob("*.wav"))
        if not paths:
            raise SystemExit(f"no WAV samples found in {samples_dir}")

        rng = random.Random(random_seed)
        rng.shuffle(paths)
        holdout_count = max(1, round(len(paths) * split_count))
        test_count = min(holdout_count, len(paths) - 2)
        validation_count = min(holdout_count, len(paths) - test_count - 1)

        self.split_paths = {
            "test": paths[:test_count],
            "validation": paths[test_count : test_count + validation_count],
            "train": paths[test_count + validation_count :],
        }

    def audio_generator(self, split: str | None = None, repeat: int = 1):
        if split is None:
            paths = [path for split_paths in self.split_paths.values() for path in split_paths]
        else:
            paths = self.split_paths[split]

        for _ in range(repeat):
            for path in paths:
                yield read_wav_16k_mono(path)


def read_wav_16k_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if channels != 1 or sample_width != 2 or sample_rate != 16000:
        raise ValueError(
            f"{path} must be 16 kHz mono 16-bit PCM WAV; "
            f"got channels={channels} sample_width={sample_width} sample_rate={sample_rate}"
        )

    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


def create_positive_features(samples_dir: Path, output_dir: Path) -> None:
    clips = LocalWavClips(samples_dir=samples_dir, split_count=0.1, random_seed=10)

    output_dir.mkdir(parents=True, exist_ok=True)
    split_settings = {
        "training": ("train", 2, 10),
        "validation": ("validation", 1, 10),
        "testing": ("test", 1, 1),
    }

    for split, (clip_split, repetition, slide_frames) in split_settings.items():
        split_dir = output_dir / split
        mmap_dir = split_dir / "wakeword_mmap"
        if (mmap_dir / "data.ninja").exists():
            continue
        if mmap_dir.exists():
            shutil.rmtree(mmap_dir)

        split_dir.mkdir(parents=True, exist_ok=True)
        spectrograms = SpectrogramGeneration(
            clips=clips,
            augmenter=None,
            slide_frames=slide_frames,
            step_ms=10,
        )
        RaggedMmap.from_generator(
            out_dir=str(mmap_dir),
            sample_generator=spectrograms.spectrogram_generator(split=clip_split, repeat=repetition),
            batch_size=100,
            verbose=True,
        )


def training_config(root: Path, features_dir: Path, train_dir: Path, training_steps: int) -> dict:
    return {
        "window_step_ms": 10,
        "train_dir": str(train_dir),
        "features": [
            {
                "features_dir": str(features_dir),
                "sampling_weight": 2.0,
                "penalty_weight": 1.0,
                "truth": True,
                "truncation_strategy": "truncate_start",
                "type": "mmap",
            },
            {
                "features_dir": str(root / "negative_datasets" / "speech"),
                "sampling_weight": 10.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(root / "negative_datasets" / "dinner_party"),
                "sampling_weight": 10.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(root / "negative_datasets" / "no_speech"),
                "sampling_weight": 5.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "random",
                "type": "mmap",
            },
            {
                "features_dir": str(root / "negative_datasets" / "dinner_party_eval"),
                "sampling_weight": 0.0,
                "penalty_weight": 1.0,
                "truth": False,
                "truncation_strategy": "split",
                "type": "mmap",
            },
        ],
        "training_steps": [training_steps],
        "positive_class_weight": [1],
        "negative_class_weight": [20],
        "learning_rates": [0.001],
        "batch_size": 128,
        "time_mask_max_size": [0],
        "time_mask_count": [0],
        "freq_mask_max_size": [0],
        "freq_mask_count": [0],
        "eval_step_interval": 500,
        "clip_duration_ms": 1500,
        "target_minimization": 0.9,
        "minimization_metric": None,
        "maximization_metric": "average_viable_recall",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare microWakeWord training inputs for Ryszardzie.")
    parser.add_argument("--root", type=Path, default=Path("/app/wakeword/ryszardzie"))
    parser.add_argument(
        "--positive-samples-dir",
        type=Path,
        default=Path("/app/audio/training-samples/ryszardzie/positive"),
    )
    parser.add_argument("--features-dir", type=Path, default=None)
    parser.add_argument("--train-dir", type=Path, default=None)
    parser.add_argument("--config-path", type=Path, default=None)
    parser.add_argument("--training-steps", type=int, default=DEFAULT_TRAINING_STEPS)
    parser.add_argument("--force-features", action="store_true")
    args = parser.parse_args()

    samples_dir = args.positive_samples_dir
    features_dir = args.features_dir or args.root / "generated_features_recorded"
    train_dir = args.train_dir or args.root / "trained_models" / "wakeword_recorded"
    config_path = args.config_path or args.root / "training_parameters_recorded.yaml"
    if not samples_dir.exists():
        raise SystemExit(f"missing positive samples directory: {samples_dir}")

    if args.force_features and features_dir.exists():
        shutil.rmtree(features_dir)
    create_positive_features(samples_dir, features_dir)

    config_path.write_text(
        yaml.safe_dump(
            training_config(args.root, features_dir, train_dir, args.training_steps),
            sort_keys=False,
        )
    )
    print(f"wrote {config_path}")
    print(f"positive samples: {samples_dir}")
    print(f"features: {features_dir}")
    print(f"train dir: {train_dir}")


if __name__ == "__main__":
    main()
