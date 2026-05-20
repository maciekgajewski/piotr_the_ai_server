#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import wave

import aioesphomeapi

from box3_common import DEFAULT_HOST, make_client


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2


async def run(host: str, output: Path, seconds: float, wait_timeout: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    client = make_client("piotr-box3-capture", host)
    done = asyncio.Event()
    started = asyncio.Event()
    writer: wave.Wave_write | None = None
    byte_count = 0

    async def handle_start(
        conversation_id: str,
        flags: int,
        audio_settings: aioesphomeapi.VoiceAssistantAudioSettings,
        wake_word_phrase: str | None,
    ) -> int:
        nonlocal writer
        writer = wave.open(str(output), "wb")
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE)
        print(
            "capture started "
            f"conversation_id={conversation_id} wake_word={wake_word_phrase!r} "
            f"flags={flags} audio_settings={audio_settings}"
        )
        started.set()
        return 0

    async def handle_audio(data: bytes) -> None:
        nonlocal byte_count
        if writer is not None:
            writer.writeframes(data)
            byte_count += len(data)

    async def handle_stop(aborted: bool) -> None:
        nonlocal writer
        if writer is not None:
            writer.close()
            writer = None
        print(f"capture stopped aborted={aborted} bytes={byte_count} output={output}")
        done.set()

    await client.connect(login=True)
    try:
        unsubscribe = client.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_audio=handle_audio,
            handle_stop=handle_stop,
        )
        try:
            print("waiting for wake word; say one of the active wake words near the Box")
            try:
                await asyncio.wait_for(started.wait(), timeout=wait_timeout)
            except TimeoutError:
                print(f"no wake-word event received within {wait_timeout:g}s")
                return
            await asyncio.sleep(seconds)
            client.send_voice_assistant_event(
                aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END,
                None,
            )
            await asyncio.sleep(0.2)
            client.send_voice_assistant_event(
                aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END,
                None,
            )
            await asyncio.wait_for(done.wait(), timeout=5)
        finally:
            unsubscribe()
    finally:
        if writer is not None:
            writer.close()
        await client.disconnect()


def default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("audio/captures") / f"box3-{stamp}.wav"


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture wake-word-triggered Box microphone audio to WAV.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--output", type=Path, default=default_output())
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--wait-timeout", type=float, default=60.0)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.output, args.seconds, args.wait_timeout))


if __name__ == "__main__":
    main()
