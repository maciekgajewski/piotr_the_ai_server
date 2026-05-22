#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import wave

from microwakeword.inference import Model
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


DEFAULT_MODEL = Path("/app/wakeword/ryszardzie/model/ryszardzie.tflite")
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


def bool_text(value: bool) -> str:
    return str(value).lower()


def read_wav_16k_mono(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if channels != CHANNELS or sample_width != SAMPLE_WIDTH_BYTES or sample_rate != SAMPLE_RATE:
        raise ValueError(
            f"{path} must be 16 kHz mono 16-bit PCM WAV; "
            f"got channels={channels} sample_width={sample_width} sample_rate={sample_rate}"
        )

    return np.frombuffer(frames, dtype="<i2")


def predict_file(
    model: Model,
    model_path: Path,
    audio_path: Path,
    step_ms: int,
    sliding_window_size: int,
    cutoff: float,
) -> bool:
    audio = read_wav_16k_mono(audio_path)
    predictions = np.array(model.predict_clip(audio, step_ms=step_ms), dtype=np.float32)

    if predictions.size == 0:
        raise ValueError(f"{audio_path}: no predictions produced; audio may be too short for this model")

    if sliding_window_size > 1 and predictions.size >= sliding_window_size:
        averaged = sliding_window_view(predictions, sliding_window_size).mean(axis=-1)
    else:
        averaged = predictions

    max_probability = float(predictions.max())
    max_average_probability = float(averaged.max())
    detected = max_average_probability >= cutoff

    print(
        "prediction "
        f"detected={bool_text(detected)} "
        f"max_probability={max_probability:.6f} "
        f"max_average_probability={max_average_probability:.6f} "
        f"cutoff={cutoff:.6f} "
        f"frames={predictions.size} "
        f"model={model_path} "
        f"file={audio_path}",
        flush=True,
    )
    return detected


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a local microWakeWord TFLite model on WAV files.")
    parser.add_argument("files", type=Path, nargs="+")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--step-ms", type=int, default=10)
    parser.add_argument("--stride", type=int, default=3)
    parser.add_argument("--sliding-window-size", type=int, default=5)
    parser.add_argument("--cutoff", type=float, default=0.01)
    args = parser.parse_args()

    model = Model(str(args.model), stride=args.stride)
    detected_count = 0

    for audio_path in args.files:
        if predict_file(model, args.model, audio_path, args.step_ms, args.sliding_window_size, args.cutoff):
            detected_count += 1

    if len(args.files) > 1:
        print(
            "summary "
            f"detected={detected_count} "
            f"total={len(args.files)} "
            f"not_detected={len(args.files) - detected_count} "
            f"cutoff={args.cutoff:.6f}",
            flush=True,
        )


if __name__ == "__main__":
    main()
