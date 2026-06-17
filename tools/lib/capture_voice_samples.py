#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import array
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
import sys
import wave

import yaml

from ai_server.config import MicrophoneConfig, load_config_from_yaml
from ai_server.microphones.drivers import create_microphone
from ai_server.microphones.interfaces import Microphone
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioStart, StartFollowUpListening


DEFAULT_RATE = 16000
DEFAULT_WIDTH = 2
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_SECONDS = 5.0
DEFAULT_SECTION_SECONDS = 0.1
CONTINUOUS_CAPTURE_TIMEOUT_SECONDS = 24 * 60 * 60
CONTINUOUS_CAPTURE_DRIVER_THRESHOLD = 1
SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "orchestrator_and_dsa_tests" / "scenarios"
PCM16_WIDTH = 2


@dataclass(frozen=True)
class AudioFormat:
    rate: int = DEFAULT_RATE
    width: int = DEFAULT_WIDTH
    channels: int = DEFAULT_CHANNELS

    @property
    def bytes_per_second(self) -> int:
        return self.rate * self.width * self.channels

    @property
    def section_bytes(self) -> int:
        size = int(round(self.bytes_per_second * DEFAULT_SECTION_SECONDS))
        frame_size = self.width * self.channels
        return max(frame_size, size - (size % frame_size))

    @property
    def sample_bytes(self) -> int:
        return int(round(self.bytes_per_second * DEFAULT_SAMPLE_SECONDS))


@dataclass(frozen=True)
class OutputScan:
    existing_seconds: float
    next_index: int
    file_count: int


class VoiceSampleAssembler:
    def __init__(
        self,
        output_dir: Path,
        start_index: int,
        existing_seconds: float,
        threshold: int,
        audio_format: AudioFormat,
    ) -> None:
        self._output_dir = output_dir
        self._next_index = start_index
        self._existing_seconds = existing_seconds
        self._threshold = threshold
        self._audio_format = audio_format
        self._section_buffer = bytearray()
        self._usable_buffer = bytearray()
        self._new_usable_bytes = 0
        self._saved_files = 0
        self._last_printed_whole_second = int(existing_seconds)

    @property
    def audio_format(self) -> AudioFormat:
        return self._audio_format

    @property
    def total_usable_seconds(self) -> float:
        return self._existing_seconds + (self._new_usable_bytes / self._audio_format.bytes_per_second)

    @property
    def pending_seconds(self) -> float:
        return len(self._usable_buffer) / self._audio_format.bytes_per_second

    @property
    def saved_files(self) -> int:
        return self._saved_files

    def add_chunk(self, chunk: bytes) -> list[Path]:
        if not chunk:
            return []

        self._section_buffer.extend(chunk)
        saved_paths: list[Path] = []
        section_bytes = self._audio_format.section_bytes
        while len(self._section_buffer) >= section_bytes:
            section = bytes(self._section_buffer[:section_bytes])
            del self._section_buffer[:section_bytes]
            if self._is_voice_section(section):
                self._usable_buffer.extend(section)
                self._new_usable_bytes += len(section)
                saved_paths.extend(self._write_ready_samples())
        return saved_paths

    def update_audio_format(self, audio_format: AudioFormat) -> None:
        if audio_format == self._audio_format:
            return
        if self._section_buffer or self._usable_buffer or self._new_usable_bytes:
            raise RuntimeError(
                f"microphone audio format changed during capture: {self._audio_format} -> {audio_format}"
            )
        self._audio_format = audio_format

    def flush(self) -> list[Path]:
        saved_paths: list[Path] = []
        if self._section_buffer:
            section = bytes(self._section_buffer)
            self._section_buffer.clear()
            if self._is_voice_section(section):
                self._usable_buffer.extend(section)
                self._new_usable_bytes += len(section)
                saved_paths.extend(self._write_ready_samples())
        return saved_paths

    def should_print_progress(self) -> bool:
        whole_second = int(self.total_usable_seconds)
        if whole_second <= self._last_printed_whole_second:
            return False
        self._last_printed_whole_second = whole_second
        return True

    def _is_voice_section(self, section: bytes) -> bool:
        if self._audio_format.width != PCM16_WIDTH:
            return any(section)
        return pcm16_peak(section) >= self._threshold

    def _write_ready_samples(self) -> list[Path]:
        saved_paths: list[Path] = []
        sample_bytes = self._audio_format.sample_bytes
        while len(self._usable_buffer) >= sample_bytes:
            payload = bytes(self._usable_buffer[:sample_bytes])
            del self._usable_buffer[:sample_bytes]
            path = next_sample_path(self._output_dir, self._next_index)
            self._write_wav(path, payload)
            self._next_index = next_free_sample_index(self._output_dir, self._next_index + 1)
            self._saved_files += 1
            saved_paths.append(path)
        return saved_paths

    def _write_wav(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as writer:
            writer.setnchannels(self._audio_format.channels)
            writer.setsampwidth(self._audio_format.width)
            writer.setframerate(self._audio_format.rate)
            writer.writeframes(payload)


def select_microphone_config(microphones: tuple[MicrophoneConfig, ...], name: str) -> MicrophoneConfig:
    for microphone in microphones:
        if microphone.name == name:
            return microphone
    raise ValueError(f"unknown microphone: {name}")


def continuous_capture_config(config: MicrophoneConfig) -> MicrophoneConfig:
    return replace(
        config,
        initial_silence_seconds=CONTINUOUS_CAPTURE_TIMEOUT_SECONDS,
        end_silence_seconds=CONTINUOUS_CAPTURE_TIMEOUT_SECONDS,
        speech_peak_threshold=CONTINUOUS_CAPTURE_DRIVER_THRESHOLD,
        post_speech_ignore_seconds=0.0,
    )


def scan_output_dir(output_dir: Path) -> OutputScan:
    existing_seconds = 0.0
    file_count = 0
    used_indexes: set[int] = set()
    if not output_dir.exists():
        return OutputScan(existing_seconds=0.0, next_index=1, file_count=0)

    for path in sorted(output_dir.glob("*.wav")):
        if path.stem.isdecimal():
            used_indexes.add(int(path.stem))
        try:
            existing_seconds += wav_duration_seconds(path)
            file_count += 1
        except (EOFError, wave.Error, OSError) as exc:
            print(f"ignored invalid wav path={path} error={exc}", file=sys.stderr, flush=True)

    return OutputScan(
        existing_seconds=existing_seconds,
        next_index=first_missing_positive(used_indexes),
        file_count=file_count,
    )


def wav_duration_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as reader:
        rate = reader.getframerate()
        if rate <= 0:
            return 0.0
        return reader.getnframes() / rate


def first_missing_positive(values: set[int]) -> int:
    candidate = 1
    while candidate in values:
        candidate += 1
    return candidate


def next_free_sample_index(output_dir: Path, start: int) -> int:
    index = max(1, start)
    while next_sample_path(output_dir, index).exists():
        index += 1
    return index


def next_sample_path(output_dir: Path, index: int) -> Path:
    return output_dir / f"{index:04d}.wav"


def audio_format_from_start(event: AudioStart) -> AudioFormat:
    return AudioFormat(
        rate=event.rate or DEFAULT_RATE,
        width=event.width or DEFAULT_WIDTH,
        channels=event.channels or DEFAULT_CHANNELS,
    )


def pcm16_peak(data: bytes) -> int:
    if len(data) < PCM16_WIDTH:
        return 0
    samples = array.array("h")
    samples.frombytes(data[: len(data) - (len(data) % PCM16_WIDTH)])
    if not samples:
        return 0
    return max(abs(sample) for sample in samples)


def load_example_phrases(scenarios_dir: Path = SCENARIOS_DIR) -> list[str]:
    phrases: list[str] = []
    for path in sorted(scenarios_dir.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as stream:
            scenario = yaml.safe_load(stream) or {}
        if not isinstance(scenario, dict):
            continue
        cases = scenario.get("cases", [])
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, dict):
                continue
            messages = case.get("messages", [])
            if isinstance(messages, list):
                phrases.extend(_strings(messages))
            command = ((case.get("task") or {}).get("command") or {})
            if isinstance(command, dict):
                query = command.get("query")
                if isinstance(query, str) and query.strip():
                    phrases.append(query.strip())
    return phrases


def _strings(values: Iterable[object]) -> list[str]:
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def print_example_phrases(phrases: list[str]) -> None:
    print("Example phrases from behavior tests:", flush=True)
    if not phrases:
        print("  (none found)", flush=True)
        return
    for index, phrase in enumerate(phrases, start=1):
        print(f"  {index}. {phrase}", flush=True)
    print(flush=True)


def print_progress(assembler: VoiceSampleAssembler) -> None:
    print(
        "usable_sound_seconds="
        f"{assembler.total_usable_seconds:.1f} pending_sample_seconds={assembler.pending_seconds:.1f}",
        flush=True,
    )


async def capture_samples(
    microphone: Microphone,
    assembler: VoiceSampleAssembler,
) -> None:
    print("Starting microphone follow-up listening. Press Ctrl-C to stop.", flush=True)
    await microphone.send_output_event(StartFollowUpListening())
    while True:
        event = await microphone.wait_for_event()
        if isinstance(event, AudioStart):
            assembler.update_audio_format(audio_format_from_start(event))
            print(
                "audio started "
                f"wake_word={event.wake_word!r} format={assembler.audio_format}",
                flush=True,
            )
            continue
        if isinstance(event, AudioChunk):
            saved_paths = assembler.add_chunk(event.data)
            for path in saved_paths:
                print(f"saved learning sample path={path}", flush=True)
            if saved_paths or assembler.should_print_progress():
                print_progress(assembler)
            continue
        if isinstance(event, AudioEnd):
            saved_paths = assembler.flush()
            for path in saved_paths:
                print(f"saved learning sample path={path}", flush=True)
            if saved_paths or assembler.should_print_progress():
                print_progress(assembler)
            print("audio ended unexpectedly; stopping capture", flush=True)
            return


async def run(args: argparse.Namespace) -> int:
    config = load_config_from_yaml(args.config)
    microphone_config = select_microphone_config(config.microphones, args.mic)
    scan = scan_output_dir(args.out)
    phrases = load_example_phrases()

    print_example_phrases(phrases)
    print(
        "voice sample output "
        f"dir={args.out} existing_files={scan.file_count} "
        f"usable_sound_seconds={scan.existing_seconds:.1f} next_file={next_sample_path(args.out, scan.next_index)}",
        flush=True,
    )
    print(
        "microphone "
        f"name={microphone_config.name} type={microphone_config.type} "
        f"speech_peak_threshold={microphone_config.speech_peak_threshold}",
        flush=True,
    )

    args.out.mkdir(parents=True, exist_ok=True)
    microphone = create_microphone(continuous_capture_config(microphone_config))
    assembler = VoiceSampleAssembler(
        output_dir=args.out,
        start_index=scan.next_index,
        existing_seconds=scan.existing_seconds,
        threshold=microphone_config.speech_peak_threshold,
        audio_format=AudioFormat(),
    )
    print_progress(assembler)
    try:
        await capture_samples(microphone, assembler)
    finally:
        await microphone.close()
        print(
            "capture stopped "
            f"usable_sound_seconds={assembler.total_usable_seconds:.1f} "
            f"pending_sample_seconds={assembler.pending_seconds:.1f} "
            f"saved_files={assembler.saved_files}",
            flush=True,
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture per-user voice learning samples from a configured microphone."
    )
    parser.add_argument("--config", type=Path, required=True, help="AI server YAML config path.")
    parser.add_argument("--mic", required=True, help="Microphone name from the server config.")
    parser.add_argument("--out", type=Path, required=True, help="Directory for 5-second WAV learning samples.")
    return parser.parse_args()


def main() -> None:
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("interrupted by Ctrl-C", flush=True)
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
