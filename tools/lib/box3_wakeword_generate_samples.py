#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

from box3_tts_piper import DEFAULT_VOICE, KNOWN_VOICES, ensure_voice


DEFAULT_PHRASES = (
    "Ryszardzie",
    "Ryszardzie.",
    "Ryszardzie!",
)
DEFAULT_VOICES = tuple(sorted(KNOWN_VOICES))
DEFAULT_TEMPO = (0.88, 0.94, 1.0, 1.06, 1.12)
DEFAULT_PITCH = (0.94, 1.0, 1.06)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(command: list[str], input_text: str | None = None) -> None:
    subprocess.run(command, input=input_text, text=True, check=True)


def synthesize_base(model_path: Path, text: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "piper",
            "--model",
            str(model_path),
            "--output_file",
            str(output),
        ],
        input_text=text,
    )


def transform_sample(source: Path, output: Path, tempo: float, pitch: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    # asetrate changes pitch and tempo together; a following atempo restores tempo.
    # The final format matches microWakeWord's 16 kHz mono input expectation.
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-af",
            f"asetrate=16000*{pitch:.5f},aresample=16000,atempo={tempo / pitch:.5f}",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(output),
        ]
    )


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_float_csv(value: str) -> list[float]:
    return [float(part) for part in parse_csv(value)]


def run_generation(args: argparse.Namespace) -> int:
    cache_dir = args.cache_dir
    output_dir = args.output_dir
    base_dir = output_dir / "_base"
    sample_count = 0

    for voice in args.voices:
        if voice not in KNOWN_VOICES:
            raise SystemExit(f"unknown Piper voice {voice!r}; choices: {', '.join(sorted(KNOWN_VOICES))}")

        model_path = ensure_voice(cache_dir, voice)
        for phrase_index, phrase in enumerate(args.phrases):
            base_path = base_dir / f"{voice}-{phrase_index:02d}.wav"
            log(f"synthesizing base voice={voice} phrase={phrase!r}")
            synthesize_base(model_path, phrase, base_path)

            for tempo in args.tempos:
                for pitch in args.pitches:
                    output = output_dir / f"ryszardzie-{sample_count:05d}-{voice}-t{tempo:.2f}-p{pitch:.2f}.wav"
                    transform_sample(base_path, output, tempo, pitch)
                    sample_count += 1

    log(f"generated_samples={sample_count} output_dir={output_dir}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate positive Ryszardzie wake-word samples with Polish Piper voices.")
    parser.add_argument("--cache-dir", type=Path, default=Path("/app/.piper-cache"))
    parser.add_argument("--output-dir", type=Path, default=Path("wakeword/ryszardzie/samples/positive"))
    parser.add_argument("--voices", type=parse_csv, default=list(DEFAULT_VOICES), help="Comma-separated Piper voices.")
    parser.add_argument("--phrases", type=parse_csv, default=list(DEFAULT_PHRASES), help="Comma-separated text prompts.")
    parser.add_argument("--tempos", type=parse_float_csv, default=list(DEFAULT_TEMPO), help="Comma-separated tempo factors.")
    parser.add_argument("--pitches", type=parse_float_csv, default=list(DEFAULT_PITCH), help="Comma-separated pitch factors.")
    args = parser.parse_args()

    if DEFAULT_VOICE not in KNOWN_VOICES:
        raise SystemExit("default voice registry is inconsistent")

    raise SystemExit(run_generation(args))


if __name__ == "__main__":
    main()
