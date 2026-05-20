#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
import time
import wave

import aioesphomeapi

from box3_common import DEFAULT_HOST, make_client


def configure_cuda_library_path() -> None:
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
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    paths = [str(path) for path in cuda_lib_dirs if path.exists()]
    if not paths:
        return
    wanted = ":".join(paths)
    if existing.startswith(wanted):
        return

    os.environ["LD_LIBRARY_PATH"] = ":".join([*paths, existing] if existing else paths)
    if os.environ.get("BOX3_STT_CUDA_LIB_PATH_READY") != "1":
        os.environ["BOX3_STT_CUDA_LIB_PATH_READY"] = "1"
        os.execvpe(sys.executable, [sys.executable, *sys.argv], os.environ)


configure_cuda_library_path()

from faster_whisper import WhisperModel


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
DEFAULT_MODEL = "base"
DEFAULT_LANGUAGE = "pl"
MODEL_PRESETS = (
    ("tiny", "Fastest, lowest accuracy."),
    ("base", "Default first-pass balance for short commands."),
    ("small", "Better accuracy, slower."),
    ("medium", "Higher accuracy, much slower and heavier."),
    ("large-v3", "Best quality preset, heaviest."),
)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def print_model_presets() -> None:
    for name, description in MODEL_PRESETS:
        print(f"{name}\t{description}")


def timestamped_wav(directory: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"box3-stt-{stamp}.wav"


def write_wav(path: Path, chunks: Iterable[bytes]) -> int:
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


def write_silence_wav(path: Path, seconds: float = 1.0) -> None:
    frames = int(SAMPLE_RATE * seconds)
    write_wav(path, [b"\x00" * frames * CHANNELS * SAMPLE_WIDTH_BYTES])


def default_compute_type(device: str) -> str:
    return "float16" if device == "cuda" else "int8"


def load_model(model_name: str, device: str, compute_type: str | None) -> tuple[WhisperModel, str, str]:
    if device == "auto":
        try:
            selected_compute_type = compute_type or default_compute_type("cuda")
            return WhisperModel(model_name, device="cuda", compute_type=selected_compute_type), "cuda", selected_compute_type
        except Exception as exc:  # CUDA visibility can differ between sandbox and escalated runs.
            log(f"CUDA unavailable, falling back to CPU: {exc}")
            selected_compute_type = compute_type or default_compute_type("cpu")
            return WhisperModel(model_name, device="cpu", compute_type=selected_compute_type), "cpu", selected_compute_type

    selected_compute_type = compute_type or default_compute_type(device)
    return WhisperModel(model_name, device=device, compute_type=selected_compute_type), device, selected_compute_type


def transcribe_file(
    whisper: WhisperModel,
    audio_path: Path,
    language: str | None,
    beam_size: int,
) -> str:
    segments, _info = whisper.transcribe(str(audio_path), language=language, beam_size=beam_size)
    return " ".join(segment.text.strip() for segment in segments).strip()


def run_self_test(whisper: WhisperModel, language: str | None, beam_size: int) -> None:
    with tempfile.TemporaryDirectory(prefix="box3-stt-self-test-") as tmpdir:
        audio_path = Path(tmpdir) / "silence.wav"
        write_silence_wav(audio_path)
        _text = transcribe_file(whisper, audio_path, language, beam_size)


async def run_stt(args: argparse.Namespace) -> int:
    started = time.monotonic()
    whisper, selected_device, selected_compute_type = load_model(args.model, args.device, args.compute_type)
    log(
        f"loaded whisper model={args.model} device={selected_device} "
        f"compute_type={selected_compute_type} in {time.monotonic() - started:.1f}s"
    )

    if args.self_test:
        run_self_test(whisper, args.language, args.beam_size)
        log("self-test transcription completed")
        return 0

    client = make_client("piotr-box3-stt-whisper", args.host)
    stream_started = asyncio.Event()
    stream_done = asyncio.Event()
    chunks: list[bytes] = []
    wake_word: str | None = None

    async def handle_start(
        _conversation_id: str,
        _flags: int,
        _audio_settings: aioesphomeapi.VoiceAssistantAudioSettings,
        wake_word_phrase: str | None,
    ) -> int:
        nonlocal wake_word
        chunks.clear()
        wake_word = wake_word_phrase
        stream_done.clear()
        stream_started.set()
        return 0

    async def handle_audio(data: bytes, data2: bytes | None = None) -> None:
        if data:
            chunks.append(data)
        if data2:
            chunks.append(data2)

    async def handle_stop(_aborted: bool) -> None:
        stream_done.set()

    await client.connect(login=True)
    try:
        unsubscribe = client.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_audio=handle_audio,
            handle_stop=handle_stop,
        )
        try:
            while True:
                log("waiting for wake word")
                try:
                    await asyncio.wait_for(stream_started.wait(), timeout=args.wait_timeout)
                except TimeoutError:
                    log(f"no wake-word event received within {args.wait_timeout:g}s")
                    if args.continuous:
                        continue
                    return 1

                log(f"wake word detected: {wake_word!r}; capturing {args.seconds:g}s")
                await asyncio.sleep(args.seconds)
                client.send_voice_assistant_event(
                    aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END,
                    None,
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(stream_done.wait(), timeout=5)
                client.send_voice_assistant_event(
                    aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END,
                    None,
                )
                await asyncio.sleep(0.2)

                audio_chunks = chunks[:]
                utterance_wake_word = wake_word
                stream_started.clear()
                stream_done.clear()
                chunks.clear()
                wake_word = None

                if not audio_chunks:
                    log("wake-word run ended without audio")
                    if not args.continuous:
                        return 1
                    continue

                if args.keep_audio is None:
                    with tempfile.TemporaryDirectory(prefix="box3-stt-") as tmpdir:
                        audio_path = Path(tmpdir) / "utterance.wav"
                        byte_count = write_wav(audio_path, audio_chunks)
                        log(f"captured bytes={byte_count} wake_word={utterance_wake_word!r}")
                        text = transcribe_file(whisper, audio_path, args.language, args.beam_size)
                else:
                    audio_path = timestamped_wav(args.keep_audio)
                    byte_count = write_wav(audio_path, audio_chunks)
                    log(f"captured bytes={byte_count} wake_word={utterance_wake_word!r} audio={audio_path}")
                    text = transcribe_file(whisper, audio_path, args.language, args.beam_size)

                print(text, flush=True)
                if not args.continuous:
                    return 0
        finally:
            unsubscribe()
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Wake-word-triggered local Whisper STT for ESP32-S3-BOX-3.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--wait-timeout", type=float, default=60.0)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--model", default=os.environ.get("BOX3_WHISPER_MODEL", DEFAULT_MODEL))
    parser.add_argument("--list-models", action="store_true", help="Print common faster-whisper model presets and exit.")
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--compute-type", default=None)
    parser.add_argument("--language", default=os.environ.get("BOX3_WHISPER_LANGUAGE", DEFAULT_LANGUAGE))
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--keep-audio", type=Path, default=None)
    parser.add_argument("--self-test", action="store_true", help="Load the model and exit without connecting to the Box.")
    args = parser.parse_args()

    if args.list_models:
        print_model_presets()
        return

    if args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    if args.wait_timeout <= 0:
        raise SystemExit("--wait-timeout must be positive")
    if args.beam_size <= 0:
        raise SystemExit("--beam-size must be positive")

    raise SystemExit(asyncio.run(run_stt(args)))


if __name__ == "__main__":
    main()
