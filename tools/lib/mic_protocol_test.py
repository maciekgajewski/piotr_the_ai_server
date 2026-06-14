#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
import struct
import sys

from ai_server.config import Config, MicrophoneConfig, load_config_from_yaml
from ai_server.microphones.drivers import create_microphone
from ai_server.microphones.interfaces import Microphone
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioStart, ConversationTimeoutCue, MessageEndCue
from ai_server.microphones.messages import StartFollowUpListening, StartWakeWordListening


DEFAULT_AUDIO_START_TIMEOUT_SECONDS = 90.0
DEFAULT_STREAM_EVENT_TIMEOUT_SECONDS = 15.0
DEFAULT_PLAYBACK_RATE = 16000
DEFAULT_PLAYBACK_WIDTH = 2
DEFAULT_PLAYBACK_CHANNELS = 1
DEFAULT_REPLAY_VOLUME = 1.0
DEFAULT_NORMALIZE_REPLAY_PEAK = 0.85
PCM16_MAX_ABS = 32768
PCM16_MAX_POSITIVE = 32767
PCM16_MIN_NEGATIVE = -32768


@dataclass(frozen=True)
class RecordedUtterance:
    label: str
    start: AudioStart
    chunks: tuple[bytes, ...]
    rate: int
    width: int
    channels: int

    @property
    def byte_count(self) -> int:
        return sum(len(chunk) for chunk in self.chunks)

    @property
    def duration_seconds(self) -> float:
        bytes_per_second = self.rate * self.width * self.channels
        if bytes_per_second <= 0:
            return 0.0
        return self.byte_count / bytes_per_second


@dataclass(frozen=True)
class OperatorAnswer:
    question: str
    answer: bool


class Operator:
    def __init__(self) -> None:
        self.answers: list[OperatorAnswer] = []
        self._reader: asyncio.StreamReader | None = None

    async def _input(self, prompt: str) -> str:
        print(prompt, end="", flush=True)
        reader = await self._stdin_reader()
        line = await reader.readline()
        if line == b"":
            raise EOFError
        return line.decode(errors="replace").rstrip("\n")

    async def _stdin_reader(self) -> asyncio.StreamReader:
        if self._reader is not None:
            return self._reader
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        self._reader = reader
        return reader

    async def pause(self, message: str) -> None:
        print()
        print(message)
        await self._input("Press Enter to continue...")

    async def ask_yes_no(self, question: str) -> bool:
        while True:
            answer = await self._input(f"{question} [y/n] ")
            normalized = answer.strip().lower()
            if normalized in ("y", "yes", "t", "tak"):
                self.answers.append(OperatorAnswer(question=question, answer=True))
                return True
            if normalized in ("n", "no", "nie"):
                self.answers.append(OperatorAnswer(question=question, answer=False))
                return False
            print("Please answer y or n.")


async def wait_for_audio_start(
    microphone: Microphone,
    timeout_seconds: float,
) -> AudioStart | None:
    while True:
        try:
            event = await asyncio.wait_for(microphone.wait_for_event(), timeout=timeout_seconds)
        except TimeoutError:
            return None

        if isinstance(event, AudioStart):
            return event

        print(f"Ignored microphone event before audio start: {type(event).__name__}")


async def capture_utterance(
    microphone: Microphone,
    label: str,
    start_timeout_seconds: float,
    stream_event_timeout_seconds: float,
) -> RecordedUtterance | None:
    print(f"Waiting for audio start: {label}")
    start = await wait_for_audio_start(microphone, start_timeout_seconds)
    if start is None:
        print(f"No audio start received within {start_timeout_seconds:g}s.")
        return None

    rate = start.rate or DEFAULT_PLAYBACK_RATE
    width = start.width or DEFAULT_PLAYBACK_WIDTH
    channels = start.channels or DEFAULT_PLAYBACK_CHANNELS
    chunks: list[bytes] = []
    print(
        "Audio started "
        f"wake_word={start.wake_word!r} rate={rate} width={width} channels={channels}"
    )

    while True:
        try:
            event = await asyncio.wait_for(microphone.wait_for_event(), timeout=stream_event_timeout_seconds)
        except TimeoutError as exc:
            raise TimeoutError(
                f"audio stream did not end within {stream_event_timeout_seconds:g}s of the previous event"
            ) from exc

        if isinstance(event, AudioChunk):
            chunks.append(event.data)
            if len(chunks) == 1 or len(chunks) % 50 == 0:
                print(f"Captured chunks={len(chunks)} bytes={sum(len(chunk) for chunk in chunks)}")
            continue
        if isinstance(event, AudioEnd):
            utterance = RecordedUtterance(
                label=label,
                start=start,
                chunks=tuple(chunks),
                rate=rate,
                width=width,
                channels=channels,
            )
            print(
                "Audio ended "
                f"chunks={len(utterance.chunks)} bytes={utterance.byte_count} "
                f"duration={utterance.duration_seconds:.2f}s"
            )
            return utterance
        if isinstance(event, AudioStart):
            raise RuntimeError("received nested AudioStart before AudioEnd")

        print(f"Ignored microphone event while recording: {type(event).__name__}")


async def replay_utterance(
    microphone: Microphone,
    utterance: RecordedUtterance,
    volume: float | None,
    normalize_replay: bool,
    normalize_target_peak: float,
) -> None:
    if not utterance.chunks:
        print(f"Skipping replay for {utterance.label}: no audio chunks were captured.")
        return

    replay_chunks = utterance.chunks
    if normalize_replay:
        replay_chunks = normalize_pcm16_chunks(
            chunks=utterance.chunks,
            width=utterance.width,
            target_peak=normalize_target_peak,
        )

    print(
        "Replaying captured audio "
        f"label={utterance.label!r} bytes={utterance.byte_count} duration={utterance.duration_seconds:.2f}s"
    )
    await microphone.send_output_event(
        AudioStart(
            rate=utterance.rate,
            width=utterance.width,
            channels=utterance.channels,
            volume=volume,
        )
    )
    for chunk in replay_chunks:
        await microphone.send_output_event(AudioChunk(data=chunk))
    await microphone.send_output_event(AudioEnd())


def normalize_pcm16_chunks(chunks: tuple[bytes, ...], width: int, target_peak: float) -> tuple[bytes, ...]:
    if width != 2:
        print(f"Replay normalization skipped: unsupported sample width={width}.")
        return chunks

    peak = pcm16_peak(chunks)
    if peak == 0:
        print("Replay normalization skipped: captured audio is silent.")
        return chunks

    target = int(PCM16_MAX_POSITIVE * target_peak)
    gain = target / peak
    print(f"Replay normalization: peak={peak} target={target} gain={gain:.2f}x")
    return tuple(normalize_pcm16_chunk(chunk, gain) for chunk in chunks)


def pcm16_peak(chunks: tuple[bytes, ...]) -> int:
    peak = 0
    for chunk in chunks:
        for sample in _iter_pcm16_samples(chunk):
            peak = max(peak, abs(sample))
    return peak


def normalize_pcm16_chunk(chunk: bytes, gain: float) -> bytes:
    normalized = bytearray()
    sample_bytes = len(chunk) - (len(chunk) % 2)
    for sample in _iter_pcm16_samples(chunk[:sample_bytes]):
        scaled = round(sample * gain)
        clamped = max(PCM16_MIN_NEGATIVE, min(PCM16_MAX_POSITIVE, scaled))
        normalized.extend(struct.pack("<h", clamped))
    normalized.extend(chunk[sample_bytes:])
    return bytes(normalized)


def _iter_pcm16_samples(chunk: bytes):
    sample_bytes = len(chunk) - (len(chunk) % 2)
    return (sample[0] for sample in struct.iter_unpack("<h", chunk[:sample_bytes]))


async def capture_cue_and_replay_step(
    microphone: Microphone,
    operator: Operator,
    label: str,
    start_timeout_seconds: float,
    stream_event_timeout_seconds: float,
    volume: float | None,
    wake_word_expected: bool,
    start_cue_question: str | None = None,
    normalize_replay: bool = True,
    normalize_target_peak: float = DEFAULT_NORMALIZE_REPLAY_PEAK,
) -> RecordedUtterance | None:
    utterance = await capture_utterance(
        microphone=microphone,
        label=label,
        start_timeout_seconds=start_timeout_seconds,
        stream_event_timeout_seconds=stream_event_timeout_seconds,
    )
    if utterance is None:
        return None

    await microphone.send_output_event(MessageEndCue())

    if wake_word_expected:
        await operator.ask_yes_no("Was the wake word detected?")
        await operator.ask_yes_no("Was the wake-word chime audible?")

    if start_cue_question is not None:
        await operator.ask_yes_no(start_cue_question)

    await operator.ask_yes_no("Was the message-end chime audible?")
    await replay_utterance(microphone, utterance, volume, normalize_replay, normalize_target_peak)
    await operator.ask_yes_no("Was the replayed phrase audible from beginning to end?")
    await operator.ask_yes_no("Was the replay free of front/back clipping?")
    return utterance


async def run_timeout_step(
    microphone: Microphone,
    operator: Operator,
    timeout_seconds: float,
    stream_event_timeout_seconds: float,
) -> bool:
    await operator.pause(
        "Timeout step: after the next follow-up chime, stay silent. "
        "The tool will wait for the configured follow-up timeout."
    )
    await microphone.send_output_event(StartFollowUpListening())
    start = await wait_for_audio_start(microphone, timeout_seconds)
    if start is not None:
        print(
            "The microphone opened an audio stream during the silence timeout step "
            f"wake_word={start.wake_word!r}. Waiting briefly to see whether it ends on its own."
        )
        chunks = 0
        bytes_seen = 0
        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    event = await asyncio.wait_for(microphone.wait_for_event(), timeout=stream_event_timeout_seconds)
                    if isinstance(event, AudioChunk):
                        chunks += 1
                        bytes_seen += len(event.data)
                        continue
                    if isinstance(event, AudioEnd):
                        print(
                            "Silence stream ended before the tool timeout "
                            f"chunks={chunks} bytes={bytes_seen}."
                        )
                        break
                    if isinstance(event, AudioStart):
                        print("Received nested AudioStart during timeout step.")
                        return False
        except TimeoutError:
            print(
                "No AudioEnd arrived during the timeout step "
                f"chunks={chunks} bytes={bytes_seen}. Sending the timeout cue anyway."
            )

    await microphone.send_output_event(ConversationTimeoutCue())
    await operator.ask_yes_no("Was the timeout-step follow-up chime audible?")
    await operator.ask_yes_no("Was the timeout chime audible?")
    return True


def select_microphone_config(config: Config, name: str | None) -> MicrophoneConfig:
    if name is None:
        if len(config.microphones) == 1:
            return config.microphones[0]
        names = ", ".join(microphone.name for microphone in config.microphones) or "(none configured)"
        raise ValueError(f"use --mic to select one microphone; configured microphones: {names}")

    for microphone in config.microphones:
        if microphone.name == name:
            return microphone
    names = ", ".join(microphone.name for microphone in config.microphones) or "(none configured)"
    raise ValueError(f"unknown microphone {name!r}; configured microphones: {names}")


def print_microphones(config: Config) -> None:
    if not config.microphones:
        print("No microphones configured.")
        return
    for microphone in config.microphones:
        area = microphone.area or "unknown area"
        print(
            f"{microphone.name}\ttype={microphone.type}\tarea={area}"
            f"\tinitial_silence={microphone.initial_silence_seconds:g}s"
            f"\tend_silence={microphone.end_silence_seconds:g}s"
            f"\tfollow_up_timeout={microphone.follow_up_timeout_seconds:g}s"
        )


async def run(args: argparse.Namespace) -> int:
    config = load_config_from_yaml(args.config)
    if args.list_mics:
        print_microphones(config)
        return 0

    microphone_config = select_microphone_config(config, args.mic)
    microphone = create_microphone(microphone_config)
    operator = Operator()
    follow_up_timeout = (
        args.follow_up_timeout
        if args.follow_up_timeout is not None
        else microphone_config.follow_up_timeout_seconds
    )

    print(f"Mic protocol test microphone={microphone.context.instance_id}")
    print("This test uses the real microphone driver and asks you to confirm audible behavior.")

    try:
        await operator.pause(
            "Initial wake-word step: the microphone should be waiting for a wake word. "
            "After pressing Enter, say the wake word and a short phrase."
        )
        await microphone.send_output_event(StartWakeWordListening())
        first = await capture_cue_and_replay_step(
            microphone=microphone,
            operator=operator,
            label="wake-word utterance",
            start_timeout_seconds=args.start_timeout,
            stream_event_timeout_seconds=args.stream_event_timeout,
            volume=args.volume,
            wake_word_expected=True,
            normalize_replay=not args.no_normalize_replay,
            normalize_target_peak=args.normalize_replay_peak,
        )
        if first is None:
            return 1

        await operator.pause(
            "Follow-up step: after the next chime, speak a short phrase without saying the wake word."
        )
        await microphone.send_output_event(StartFollowUpListening())
        follow_up = await capture_cue_and_replay_step(
            microphone=microphone,
            operator=operator,
            label="follow-up utterance",
            start_timeout_seconds=follow_up_timeout,
            stream_event_timeout_seconds=args.stream_event_timeout,
            volume=args.volume,
            wake_word_expected=False,
            start_cue_question="Was the follow-up chime audible?",
            normalize_replay=not args.no_normalize_replay,
            normalize_target_peak=args.normalize_replay_peak,
        )
        await operator.ask_yes_no("Did follow-up mode capture speech without a wake word?")
        if follow_up is None:
            return 1

        timeout_ok = await run_timeout_step(
            microphone=microphone,
            operator=operator,
            timeout_seconds=follow_up_timeout,
            stream_event_timeout_seconds=args.stream_event_timeout,
        )
        if not timeout_ok:
            return 1

        if not args.skip_final_wake_check:
            await operator.pause(
                "Final wake-word check: the tool will now explicitly return the mic to wake-word mode. "
                "After pressing Enter, say the wake word and one short phrase."
            )
            await microphone.send_output_event(StartWakeWordListening())
            final = await capture_cue_and_replay_step(
                microphone=microphone,
                operator=operator,
                label="final wake-word utterance",
                start_timeout_seconds=args.start_timeout,
                stream_event_timeout_seconds=args.stream_event_timeout,
                volume=args.volume,
                wake_word_expected=True,
                normalize_replay=not args.no_normalize_replay,
                normalize_target_peak=args.normalize_replay_peak,
            )
            if final is None:
                return 1

        print()
        print("Operator answers:")
        for answer in operator.answers:
            print(f"- {'yes' if answer.answer else 'no'}: {answer.question}")

        if args.fail_on_no and any(not answer.answer for answer in operator.answers):
            return 1
        return 0
    finally:
        await microphone.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_config = Path(os.environ.get("AI_SERVER_CONFIG", "ai_server/test-config.yaml"))
    parser = argparse.ArgumentParser(description="Interactively test a configured microphone protocol driver.")
    parser.add_argument("--config", type=Path, default=default_config, help="AI server YAML config path.")
    parser.add_argument("--mic", default=None, help="Configured microphone name. Required when multiple mics exist.")
    parser.add_argument("--list-mics", action="store_true", help="List configured microphones and exit.")
    parser.add_argument(
        "--start-timeout",
        type=float,
        default=DEFAULT_AUDIO_START_TIMEOUT_SECONDS,
        help="Seconds to wait for wake-word audio start.",
    )
    parser.add_argument(
        "--stream-event-timeout",
        type=float,
        default=DEFAULT_STREAM_EVENT_TIMEOUT_SECONDS,
        help="Seconds to wait between audio stream events before failing.",
    )
    parser.add_argument(
        "--follow-up-timeout",
        type=float,
        default=None,
        help="Override configured follow-up timeout seconds.",
    )
    parser.add_argument(
        "--volume",
        type=float,
        default=DEFAULT_REPLAY_VOLUME,
        help="Replay playback volume from 0.0 to 1.0.",
    )
    parser.add_argument(
        "--no-normalize-replay",
        action="store_true",
        help="Replay captured microphone audio without PCM peak normalization.",
    )
    parser.add_argument(
        "--normalize-replay-peak",
        type=float,
        default=DEFAULT_NORMALIZE_REPLAY_PEAK,
        help="Target replay normalization peak from 0.0 to 1.0.",
    )
    parser.add_argument("--skip-final-wake-check", action="store_true")
    parser.add_argument("--fail-on-no", action="store_true", help="Exit with status 1 if any operator answer is no.")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    args = parser.parse_args(argv)

    if args.start_timeout <= 0:
        raise SystemExit("--start-timeout must be positive")
    if args.stream_event_timeout <= 0:
        raise SystemExit("--stream-event-timeout must be positive")
    if args.follow_up_timeout is not None and args.follow_up_timeout <= 0:
        raise SystemExit("--follow-up-timeout must be positive")
    if args.volume is not None and not 0.0 <= args.volume <= 1.0:
        raise SystemExit("--volume must be between 0.0 and 1.0")
    if not 0.0 < args.normalize_replay_peak <= 1.0:
        raise SystemExit("--normalize-replay-peak must be between 0.0 and 1.0")
    return args


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGINT, _exit_on_sigint)
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aioesphomeapi.connection").setLevel(logging.WARNING)
    logging.getLogger("aioesphomeapi._frame_helper").setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except asyncio.CancelledError:
        print()
        print("Interrupted.")
        return 130
    except KeyboardInterrupt:
        print()
        print("Interrupted.")
        return 130


def _exit_on_sigint(_signum, _frame) -> None:
    os.write(sys.stdout.fileno(), b"\nInterrupted.\n")
    os._exit(130)


if __name__ == "__main__":
    sys.exit(main())
