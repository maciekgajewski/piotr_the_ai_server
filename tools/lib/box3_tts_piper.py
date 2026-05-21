#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

from box3_common import DEFAULT_HOST
from box3_play_audio import run as play_audio


DEFAULT_VOICE = "pl_PL-bass-high"
VOICE_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
KNOWN_VOICES = {
    "pl_PL-bass-high": "pl/pl_PL/bass/high",
    "pl_PL-darkman-medium": "pl/pl_PL/darkman/medium",
    "pl_PL-gosia-medium": "pl/pl_PL/gosia/medium",
    "pl_PL-mc_speech-medium": "pl/pl_PL/mc_speech/medium",
    "pl_PL-mls_6892-low": "pl/pl_PL/mls_6892/low",
}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def timestamped_path(directory: Path, suffix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"box3-tts-{stamp}{suffix}"


def voice_cache_dir(cache_dir: Path, voice: str) -> Path:
    return cache_dir / "voices" / voice


def voice_urls(voice: str) -> tuple[str, str]:
    try:
        voice_dir = KNOWN_VOICES[voice]
    except KeyError as exc:
        raise ValueError(f"unknown voice {voice!r}; use --list-voices") from exc
    model_name = f"{voice}.onnx"
    config_name = f"{voice}.onnx.json"
    return (
        f"{VOICE_BASE_URL}/{voice_dir}/{model_name}",
        f"{VOICE_BASE_URL}/{voice_dir}/{config_name}",
    )


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_suffix(destination.suffix + ".tmp")
    log(f"downloading {url}")
    with urllib.request.urlopen(url) as response, tmp_path.open("wb") as output:
        shutil.copyfileobj(response, output)
    tmp_path.replace(destination)


def ensure_voice(cache_dir: Path, voice: str) -> Path:
    directory = voice_cache_dir(cache_dir, voice)
    model_path = directory / f"{voice}.onnx"
    config_path = directory / f"{voice}.onnx.json"
    model_url, config_url = voice_urls(voice)

    if not model_path.exists():
        download(model_url, model_path)
    if not config_path.exists():
        download(config_url, config_path)
    return model_path


def run_command(command: list[str], input_text: str | None = None) -> None:
    subprocess.run(command, input=input_text, text=True, check=True)


def synthesize_with_piper(text: str, model_path: Path, wav_path: Path) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "piper",
            "--model",
            str(model_path),
            "--output_file",
            str(wav_path),
        ],
        input_text=text,
    )


def convert_to_box_flac(wav_path: Path, flac_path: Path) -> None:
    flac_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav_path),
            "-ar",
            "48000",
            "-ac",
            "1",
            str(flac_path),
        ]
    )


async def run_tts(args: argparse.Namespace) -> int:
    if args.list_voices:
        for voice in sorted(KNOWN_VOICES):
            print(voice)
        return 0

    text = sys.stdin.read().strip()
    if not text:
        log("no text received on stdin")
        return 1

    model_path = ensure_voice(args.cache_dir, args.voice)
    output_dir = args.output_dir
    wav_path = timestamped_path(output_dir, ".wav")
    flac_path = timestamped_path(output_dir, ".flac")

    log(f"synthesizing voice={args.voice} chars={len(text)}")
    generated_started = time.monotonic()
    synthesize_with_piper(text, model_path, wav_path)
    convert_to_box_flac(wav_path, flac_path)
    generated_seconds = time.monotonic() - generated_started
    log(f"tts_generate_seconds={generated_seconds:.3f} audio={flac_path}")
    if not args.keep_wav:
        wav_path.unlink(missing_ok=True)

    if args.self_test:
        log(f"self-test audio={flac_path}")
        return 0

    playback_timing = await play_audio(args.host, flac_path, args.port, args.wait, args.volume)
    log(f"tts_send_seconds={playback_timing['send_seconds']:.3f}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Read text from stdin, synthesize it with Piper, and play it on the Box.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--voice", default=os.environ.get("BOX3_PIPER_VOICE", DEFAULT_VOICE))
    parser.add_argument("--cache-dir", type=Path, default=Path(os.environ.get("PIPER_HOME", ".piper-cache")))
    parser.add_argument("--output-dir", type=Path, default=Path("audio/tts"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--wait", type=float, default=8.0)
    parser.add_argument("--volume", type=float, default=1.0)
    parser.add_argument("--keep-wav", action="store_true")
    parser.add_argument("--self-test", action="store_true", help="Synthesize audio but do not send it to the Box.")
    parser.add_argument("--list-voices", action="store_true")
    args = parser.parse_args()

    if args.volume is not None and not 0.0 <= args.volume <= 1.0:
        raise SystemExit("--volume must be between 0.0 and 1.0")
    if args.wait <= 0:
        raise SystemExit("--wait must be positive")

    raise SystemExit(asyncio.run(run_tts(args)))


if __name__ == "__main__":
    main()
