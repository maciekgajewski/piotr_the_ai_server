#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import array
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
import sys
import wave

import aioesphomeapi

from box3_common import DEFAULT_HOST, make_client


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
DEFAULT_OUTPUT_DIR = Path("audio/training-samples/ryszardzie/positive")
DEFAULT_SECONDS = 1.5
DEFAULT_COUNT = 20
DEFAULT_READY_DELAY = 0.4
DEFAULT_NORMALIZE_PEAK = 0.89
HA_WAKE_WORD_MODE = "In Home Assistant"
ON_DEVICE_WAKE_WORD_MODE = "On device"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


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


def normalize_wav(path: Path, target_peak: float) -> tuple[float, float]:
    with wave.open(str(path), "rb") as reader:
        params = reader.getparams()
        frames = reader.readframes(reader.getnframes())

    if params.sampwidth != SAMPLE_WIDTH_BYTES:
        raise ValueError(f"normalization only supports 16-bit PCM WAV, got sampwidth={params.sampwidth}")
    if not frames:
        return 0.0, 1.0

    samples = array.array("h")
    samples.frombytes(frames)
    if samples.itemsize != SAMPLE_WIDTH_BYTES:
        raise RuntimeError("platform does not expose 16-bit signed short samples")

    peak = max(abs(sample) for sample in samples)
    if peak == 0:
        return 0.0, 1.0

    current_peak = peak / 32767.0
    gain = min(target_peak / current_peak, 32767.0 / peak)
    if gain <= 1.0:
        return current_peak, 1.0

    for index, sample in enumerate(samples):
        samples[index] = max(-32768, min(32767, round(sample * gain)))

    with wave.open(str(path), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(samples.tobytes())

    return current_peak, gain


def next_sample_index(directory: Path) -> int:
    highest = 0
    if not directory.exists():
        return 1
    for path in directory.glob("*.wav"):
        if path.stem.isdecimal():
            highest = max(highest, int(path.stem))
    return highest + 1


def sample_path(directory: Path, index: int) -> Path:
    return directory / f"{index:04d}.wav"


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
    if len(candidates) > 1:
        log(f"found multiple wake-word select entities; using key={candidates[0].key}")
    selected = candidates[0]
    return int(selected.key), int(getattr(selected, "device_id", 0) or 0)


async def prompt_for_sample(index: int, count: int, path: Path) -> None:
    prompt = (
        f"sample {index}/{count}: ready -> press Enter, then say \"Ryszardzie\" "
        f"(saving {path})"
    )
    await asyncio.to_thread(input, prompt)


async def run(args: argparse.Namespace) -> int:
    client = make_client("piotr-box3-record-wakeword-samples", args.host)
    stream_started = asyncio.Event()
    stream_stopped = asyncio.Event()
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
        stream_stopped.clear()
        stream_started.set()
        return 0

    async def handle_audio(*data_chunks: bytes) -> None:
        if recording:
            chunks.extend(chunk for chunk in data_chunks if chunk)

    async def handle_stop(aborted: bool) -> None:
        log(f"audio stream stopped aborted={aborted}")
        stream_stopped.set()

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

        log(f"switching wake-word engine to {HA_WAKE_WORD_MODE!r} for raw capture")
        client.select_command(select_key, HA_WAKE_WORD_MODE, device_id=device_id)
        restore_on_device = True

        try:
            await asyncio.wait_for(stream_started.wait(), timeout=args.stream_timeout)
        except TimeoutError:
            raise RuntimeError(
                f"Box did not start streaming audio within {args.stream_timeout:g}s"
            ) from None

        args.output_dir.mkdir(parents=True, exist_ok=True)
        start_index = next_sample_index(args.output_dir)
        saved = 0

        for offset in range(args.count):
            sample_number = start_index + offset
            output = sample_path(args.output_dir, sample_number)
            await prompt_for_sample(offset + 1, args.count, output)
            await asyncio.sleep(args.ready_delay)

            chunks.clear()
            recording = True
            try:
                await asyncio.sleep(args.seconds)
            finally:
                recording = False

            byte_count = write_wav(output, chunks)
            duration = byte_count / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES)
            normalize_message = ""
            if byte_count > 0 and args.normalize_peak is not None:
                peak, gain = normalize_wav(output, args.normalize_peak)
                normalize_message = (
                    f" normalized_peak={peak:.3f} normalized_gain={gain:.2f} "
                    f"target_peak={args.normalize_peak:.3f}"
                )
            print(
                f"saved sample={offset + 1}/{args.count} "
                f"bytes={byte_count} duration={duration:.3f}s{normalize_message} output={output}",
                flush=True,
            )
            saved += 1

        print(f"saved {saved} samples in {args.output_dir}", flush=True)
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
    parser = argparse.ArgumentParser(
        description="Record positive wake-word training samples through the ESP32-S3-BOX-3 microphone."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument(
        "--ready-delay",
        type=float,
        default=DEFAULT_READY_DELAY,
        help="Seconds to wait after Enter before recording starts, to avoid keyboard noise.",
    )
    parser.add_argument(
        "--normalize-peak",
        type=float,
        default=DEFAULT_NORMALIZE_PEAK,
        help="Normalize recorded 16-bit PCM WAV peak to this 0.0-1.0 level. Use 0 to disable.",
    )
    parser.add_argument("--stream-timeout", type=float, default=15.0)
    args = parser.parse_args()

    if args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    if args.count <= 0:
        raise SystemExit("--count must be positive")
    if args.ready_delay < 0:
        raise SystemExit("--ready-delay must be non-negative")
    if not 0.0 <= args.normalize_peak <= 1.0:
        raise SystemExit("--normalize-peak must be between 0.0 and 1.0")
    if args.stream_timeout <= 0:
        raise SystemExit("--stream-timeout must be positive")
    args.normalize_peak = args.normalize_peak or None

    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
