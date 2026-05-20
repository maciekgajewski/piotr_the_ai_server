#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import array
from datetime import datetime, timezone
from pathlib import Path
import wave

import aioesphomeapi

from box3_common import DEFAULT_HOST, make_client


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
DEFAULT_NORMALIZE_PEAK = 0.89


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


def timestamped_output(directory: Path = Path("audio/captures")) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"box3-{stamp}.wav"


def capture_output(output: Path | None, continuous: bool) -> Path:
    if output is None:
        return timestamped_output()
    if continuous and (output.is_dir() or output.suffix == ""):
        return timestamped_output(output)
    return output


async def run(
    host: str,
    output: Path | None,
    seconds: float,
    wait_timeout: float,
    normalize_peak: float | None,
    continuous: bool,
) -> None:
    client = make_client("piotr-box3-capture", host)
    stream_done = asyncio.Event()
    stream_started = asyncio.Event()
    writer: wave.Wave_write | None = None
    current_output: Path | None = None
    byte_count = 0

    def close_capture(aborted: bool | None = None) -> None:
        nonlocal writer
        if writer is not None:
            writer.close()
            writer = None
        suffix = "" if aborted is None else f" aborted={aborted}"
        print(f"capture stopped{suffix} bytes={byte_count} output={current_output}")
        stream_done.set()

    async def handle_start(
        conversation_id: str,
        flags: int,
        audio_settings: aioesphomeapi.VoiceAssistantAudioSettings,
        wake_word_phrase: str | None,
    ) -> int:
        nonlocal byte_count, current_output, writer
        if writer is not None:
            close_capture(aborted=True)
        stream_done.clear()
        stream_started.clear()
        byte_count = 0
        current_output = capture_output(output, continuous)
        current_output.parent.mkdir(parents=True, exist_ok=True)
        writer = wave.open(str(current_output), "wb")
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE)
        print(
            "capture started "
            f"conversation_id={conversation_id} wake_word={wake_word_phrase!r} "
            f"flags={flags} audio_settings={audio_settings}"
        )
        stream_started.set()
        return 0

    async def handle_audio(data: bytes) -> None:
        nonlocal byte_count
        if writer is not None:
            writer.writeframes(data)
            byte_count += len(data)

    async def handle_stop(aborted: bool) -> None:
        close_capture(aborted)

    await client.connect(login=True)
    try:
        unsubscribe = client.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_audio=handle_audio,
            handle_stop=handle_stop,
        )
        try:
            while True:
                print("waiting for wake word; keep this process running for wake-word detection")
                try:
                    await asyncio.wait_for(stream_started.wait(), timeout=wait_timeout)
                except TimeoutError:
                    print(f"no wake-word event received within {wait_timeout:g}s")
                    if continuous:
                        continue
                    return

                await asyncio.sleep(seconds)
                client.send_voice_assistant_event(
                    aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END,
                    None,
                )
                try:
                    await asyncio.wait_for(stream_done.wait(), timeout=5)
                except TimeoutError:
                    close_capture()
                client.send_voice_assistant_event(
                    aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END,
                    None,
                )
                await asyncio.sleep(0.2)

                if byte_count > 0 and normalize_peak is not None and current_output is not None:
                    peak, gain = normalize_wav(current_output, normalize_peak)
                    print(
                        f"normalized peak={peak:.3f} gain={gain:.2f} "
                        f"target_peak={normalize_peak:.3f} output={current_output}"
                    )

                stream_started.clear()
                stream_done.clear()
                if not continuous:
                    break
        finally:
            unsubscribe()
    finally:
        if writer is not None:
            close_capture()
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture wake-word-triggered Box microphone audio to WAV.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--wait-timeout", type=float, default=60.0)
    parser.add_argument("--continuous", action="store_true", help="Stay connected and capture every wake-word run.")
    parser.add_argument(
        "--normalize-peak",
        type=float,
        default=DEFAULT_NORMALIZE_PEAK,
        help="Normalize captured 16-bit PCM WAV peak to this 0.0-1.0 level. Use 0 to disable.",
    )
    args = parser.parse_args()
    if not 0.0 <= args.normalize_peak <= 1.0:
        raise SystemExit("--normalize-peak must be between 0.0 and 1.0")
    normalize_peak = args.normalize_peak or None
    asyncio.run(run(args.host, args.output, args.seconds, args.wait_timeout, normalize_peak, args.continuous))


if __name__ == "__main__":
    main()
