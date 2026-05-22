#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import wave

import aioesphomeapi

from box3_common import DEFAULT_HOST, make_client


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
DEFAULT_OUTPUT_DIR = Path("audio/wakeword-tests/ryszardzie")
DEFAULT_SECONDS = 1.5
DEFAULT_READY_DELAY = 0.4
DEFAULT_MODEL = Path("wakeword/ryszardzie/model/ryszardzie.tflite")
HA_WAKE_WORD_MODE = "In Home Assistant"
ON_DEVICE_WAKE_WORD_MODE = "On device"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def timestamped_wav(directory: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"wakeword-test-{stamp}.wav"


def write_wav(path: Path, chunks: Iterable[bytes]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    byte_count = 0
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE)
        for chunk in chunks:
            if chunk:
                writer.writeframes(chunk)
                byte_count += len(chunk)
    return byte_count


async def find_wake_word_select(client: aioesphomeapi.APIClient) -> tuple[int, int]:
    entities, _services = await client.list_entities_services()
    candidates = []
    for entity in entities:
        if type(entity).__name__ != "SelectInfo":
            continue
        name = (getattr(entity, "name", "") or "").lower()
        object_id = (getattr(entity, "object_id", "") or "").lower()
        if "wake word engine location" in name or "wake_word_engine_location" in object_id:
            candidates.append(entity)

    if not candidates:
        raise RuntimeError("Could not find the Box 'Wake word engine location' select entity")
    selected = candidates[0]
    return int(selected.key), int(getattr(selected, "device_id", 0) or 0)


async def prompt_for_test(index: int, output: Path) -> None:
    prompt = f"test {index}: ready -> press Enter, then speak near the Box (saving {output})"
    await asyncio.to_thread(input, prompt)


def predict_file(audio_path: Path, model_path: Path, cutoff: float) -> None:
    container_model_path = str(model_path) if model_path.is_absolute() else f"/app/{model_path}"
    command = [
        "tools/box3-wakeword-predict-file.sh",
        str(audio_path),
        "--model",
        container_model_path,
        "--cutoff",
        str(cutoff),
    ]
    subprocess.run(command, check=True)


async def run(args: argparse.Namespace) -> int:
    client = make_client("piotr-box3-wakeword-test", args.host)
    stream_started = asyncio.Event()
    recording = False
    chunks: list[bytes] = []

    async def handle_start(
        conversation_id: str,
        flags: int,
        audio_settings: aioesphomeapi.VoiceAssistantAudioSettings,
        wake_word_phrase: str | None,
    ) -> int:
        log(
            "audio stream started "
            f"conversation_id={conversation_id} wake_word={wake_word_phrase!r} "
            f"flags={flags} audio_settings={audio_settings}"
        )
        stream_started.set()
        return 0

    async def handle_audio(*data_chunks: bytes) -> None:
        if recording:
            chunks.extend(chunk for chunk in data_chunks if chunk)

    async def handle_stop(aborted: bool) -> None:
        log(f"audio stream stopped aborted={aborted}")

    await client.connect(login=True)
    unsubscribe = None
    restore_on_device = False
    try:
        select_key, device_id = await find_wake_word_select(client)
        unsubscribe = client.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_audio=handle_audio,
            handle_stop=handle_stop,
        )

        log(f"switching wake-word engine to {HA_WAKE_WORD_MODE!r} for raw test capture")
        client.select_command(select_key, HA_WAKE_WORD_MODE, device_id=device_id)
        restore_on_device = True

        try:
            await asyncio.wait_for(stream_started.wait(), timeout=args.stream_timeout)
        except TimeoutError:
            raise RuntimeError(
                f"Box did not start streaming audio within {args.stream_timeout:g}s"
            ) from None

        for index in range(1, args.count + 1):
            output = timestamped_wav(args.output_dir)
            await prompt_for_test(index, output)
            await asyncio.sleep(args.ready_delay)

            chunks.clear()
            recording = True
            try:
                await asyncio.sleep(args.seconds)
            finally:
                recording = False

            byte_count = write_wav(output, chunks)
            duration = byte_count / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES)
            print(f"captured bytes={byte_count} duration={duration:.3f}s output={output}", flush=True)
            predict_file(output, args.model, args.cutoff)

        return 0
    finally:
        if restore_on_device:
            with suppress_cleanup_errors():
                log(f"restoring wake-word engine to {ON_DEVICE_WAKE_WORD_MODE!r}")
                client.select_command(select_key, ON_DEVICE_WAKE_WORD_MODE, device_id=device_id)
        if unsubscribe is not None:
            unsubscribe()
        await client.disconnect()


@contextmanager
def suppress_cleanup_errors() -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        log(f"ignored cleanup error: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture Box microphone audio and test it against a local wake-word model.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument("--ready-delay", type=float, default=DEFAULT_READY_DELAY)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--cutoff", type=float, default=0.01)
    parser.add_argument("--stream-timeout", type=float, default=15.0)
    args = parser.parse_args()

    if args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    if args.ready_delay < 0:
        raise SystemExit("--ready-delay must be non-negative")
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    if not 0.0 <= args.cutoff <= 1.0:
        raise SystemExit("--cutoff must be between 0.0 and 1.0")
    if args.stream_timeout <= 0:
        raise SystemExit("--stream-timeout must be positive")

    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
