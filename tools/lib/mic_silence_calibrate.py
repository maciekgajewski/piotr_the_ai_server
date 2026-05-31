#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import math
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aioesphomeapi

from ai_server.config import DEFAULT_SPEECH_PEAK_THRESHOLD, MicrophoneConfig, load_config_from_yaml


API_PORT = 6053
DEFAULT_CAPTURE_SECONDS = 5.0
DEFAULT_DISCARD_SECONDS = 0.5
DEFAULT_START_TIMEOUT_SECONDS = 30.0
DEFAULT_STOP_TIMEOUT_SECONDS = 3.0
DEFAULT_RECOMMENDATION_MARGIN = 1.5
DEFAULT_ROUND_TO = 50
START_FOLLOW_UP_LISTENING_SERVICE = "start_follow_up_listening"
PCM16_MAX_POSITIVE = 32767


@dataclass(frozen=True)
class ChunkPeak:
    seconds_since_start: float
    peak: int
    byte_count: int


@dataclass(frozen=True)
class PeakSummary:
    chunk_count: int
    byte_count: int
    p50: int
    p90: int
    p95: int
    p99: int
    max_peak: int
    recommended_threshold: int


def select_microphone_config(microphones: tuple[MicrophoneConfig, ...], name: str | None) -> MicrophoneConfig:
    if name is None:
        if len(microphones) == 1:
            return microphones[0]
        raise ValueError("--microphone is required when more than one microphone is configured")

    for microphone in microphones:
        if microphone.name == name:
            return microphone
    raise ValueError(f"unknown microphone: {name}")


def summarize_peaks(
    peaks: list[ChunkPeak],
    discard_seconds: float,
    min_threshold: int,
    margin: float,
    round_to: int,
) -> PeakSummary:
    analyzed = [peak for peak in peaks if peak.seconds_since_start >= discard_seconds]
    if not analyzed:
        raise ValueError("no audio chunks were captured after the discarded startup window")

    values = sorted(peak.peak for peak in analyzed)
    p99 = percentile(values, 99)
    recommended = round_up(max(min_threshold, math.ceil(p99 * margin)), round_to)
    recommended = min(recommended, PCM16_MAX_POSITIVE)
    return PeakSummary(
        chunk_count=len(analyzed),
        byte_count=sum(peak.byte_count for peak in analyzed),
        p50=percentile(values, 50),
        p90=percentile(values, 90),
        p95=percentile(values, 95),
        p99=p99,
        max_peak=max(values),
        recommended_threshold=recommended,
    )


def percentile(sorted_values: list[int], percentile_value: int) -> int:
    if not sorted_values:
        raise ValueError("cannot calculate percentile of an empty list")
    index = math.ceil((percentile_value / 100) * len(sorted_values)) - 1
    return sorted_values[max(0, min(len(sorted_values) - 1, index))]


def round_up(value: int, step: int) -> int:
    if step <= 1:
        return value
    return int(math.ceil(value / step) * step)


def pcm16_peak(data: bytes) -> int:
    peak = 0
    sample_bytes = len(data) - (len(data) % 2)
    for sample in struct.iter_unpack("<h", data[:sample_bytes]):
        peak = max(peak, abs(sample[0]))
    return peak


async def execute_service(client: Any, service_name: str) -> None:
    _, services = await client.list_entities_services()
    for service in services:
        if service.name == service_name:
            await client.execute_service(service, {})
            return
    raise ValueError(f"ESPHome API service not found: {service_name}")


async def run(
    config_path: Path,
    microphone_name: str | None,
    capture_seconds: float,
    discard_seconds: float,
    start_timeout_seconds: float,
    min_threshold: int,
    margin: float,
    round_to: int,
    use_follow_up_service: bool,
) -> PeakSummary:
    config = load_config_from_yaml(config_path)
    microphone = select_microphone_config(config.microphones, microphone_name)
    if microphone.type != "box3_esphome":
        raise ValueError(f"unsupported microphone type for calibration: {microphone.type}")

    expected_name = microphone.options.get("expected_name")
    if expected_name is not None and not isinstance(expected_name, str):
        raise ValueError(f"microphone {microphone.name} expected_name must be a string when provided")

    stream_started = asyncio.Event()
    stream_stopped = asyncio.Event()
    peaks: list[ChunkPeak] = []
    stream_started_at: float | None = None

    async def handle_start(
        conversation_id: str,
        flags: int,
        audio_settings: Any,
        wake_word_phrase: str | None,
    ) -> int:
        nonlocal stream_started_at
        stream_started_at = time.monotonic()
        peaks.clear()
        stream_stopped.clear()
        print(
            "audio started "
            f"conversation_id={conversation_id} wake_word={wake_word_phrase!r} "
            f"flags={flags} audio_settings={audio_settings}"
        )
        stream_started.set()
        return 0

    async def handle_audio(data: bytes, data2: bytes | None = None) -> None:
        if stream_started_at is None:
            return
        now = time.monotonic()
        for chunk in (data, data2):
            if not chunk:
                continue
            peaks.append(
                ChunkPeak(
                    seconds_since_start=now - stream_started_at,
                    peak=pcm16_peak(chunk),
                    byte_count=len(chunk),
                )
            )

    async def handle_stop(aborted: bool) -> None:
        print(f"audio stopped aborted={aborted}")
        stream_stopped.set()

    client = aioesphomeapi.APIClient(
        microphone.options["address"],
        API_PORT,
        password=None,
        client_info="piotr-mic-silence-calibrate",
        noise_psk=microphone.options["api_key"],
        expected_name=expected_name,
    )
    await client.connect(login=True)
    try:
        unsubscribe = client.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_audio=handle_audio,
            handle_stop=handle_stop,
        )
        try:
            if use_follow_up_service:
                print(f"starting follow-up listening through ESPHome service on microphone={microphone.name}")
                await execute_service(client, START_FOLLOW_UP_LISTENING_SERVICE)
            else:
                print(f"waiting for wake word or button on microphone={microphone.name}")

            await asyncio.wait_for(stream_started.wait(), timeout=start_timeout_seconds)
            print(
                f"capturing room tone seconds={capture_seconds:g} "
                f"discard_startup_seconds={discard_seconds:g}; keep the room quiet"
            )
            await asyncio.sleep(capture_seconds)
            client.send_voice_assistant_event(
                aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END,
                None,
            )
            client.send_voice_assistant_event(
                aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END,
                None,
            )
            try:
                await asyncio.wait_for(stream_stopped.wait(), timeout=DEFAULT_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                print(f"audio stop was not observed within {DEFAULT_STOP_TIMEOUT_SECONDS:g}s; continuing")
        finally:
            unsubscribe()
    finally:
        await client.disconnect()

    return summarize_peaks(
        peaks=peaks,
        discard_seconds=discard_seconds,
        min_threshold=min_threshold,
        margin=margin,
        round_to=round_to,
    )


def print_summary(microphone_name: str | None, current_threshold: int, summary: PeakSummary) -> None:
    print()
    print(f"analyzed_chunks={summary.chunk_count} analyzed_bytes={summary.byte_count}")
    print(
        "chunk_peak "
        f"p50={summary.p50} p90={summary.p90} p95={summary.p95} "
        f"p99={summary.p99} max={summary.max_peak}"
    )
    print(f"current_speech_peak_threshold={current_threshold}")
    print(f"recommended_speech_peak_threshold={summary.recommended_threshold}")
    if microphone_name is not None:
        print()
        print("config snippet:")
        print(f"  - name: {microphone_name}")
        print(f"    speech_peak_threshold: {summary.recommended_threshold}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate room-noise peak threshold for ESPHome microphones.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--microphone", help="Microphone name from config. Required when multiple are configured.")
    parser.add_argument("--seconds", type=float, default=DEFAULT_CAPTURE_SECONDS)
    parser.add_argument("--discard-seconds", type=float, default=DEFAULT_DISCARD_SECONDS)
    parser.add_argument("--start-timeout", type=float, default=DEFAULT_START_TIMEOUT_SECONDS)
    parser.add_argument("--min-threshold", type=int, default=DEFAULT_SPEECH_PEAK_THRESHOLD)
    parser.add_argument("--margin", type=float, default=DEFAULT_RECOMMENDATION_MARGIN)
    parser.add_argument("--round-to", type=int, default=DEFAULT_ROUND_TO)
    parser.add_argument(
        "--wake-word",
        action="store_true",
        help="Wait for a wake word/button instead of starting the firmware follow-up-listening service.",
    )
    args = parser.parse_args()

    if args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    if args.discard_seconds < 0 or args.discard_seconds >= args.seconds:
        raise SystemExit("--discard-seconds must be >= 0 and less than --seconds")
    if not 1 <= args.min_threshold <= PCM16_MAX_POSITIVE:
        raise SystemExit(f"--min-threshold must be between 1 and {PCM16_MAX_POSITIVE}")
    if args.margin < 1:
        raise SystemExit("--margin must be at least 1")
    if args.round_to <= 0:
        raise SystemExit("--round-to must be positive")

    config = load_config_from_yaml(args.config)
    microphone = select_microphone_config(config.microphones, args.microphone)
    summary = asyncio.run(
        run(
            config_path=args.config,
            microphone_name=args.microphone,
            capture_seconds=args.seconds,
            discard_seconds=args.discard_seconds,
            start_timeout_seconds=args.start_timeout,
            min_threshold=args.min_threshold,
            margin=args.margin,
            round_to=args.round_to,
            use_follow_up_service=not args.wake_word,
        )
    )
    print_summary(microphone.name, microphone.speech_peak_threshold, summary)


if __name__ == "__main__":
    main()
