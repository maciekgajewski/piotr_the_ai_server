#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.request
import wave

from box3_common import DEFAULT_HOST, local_ip_for, make_client, media_player_key
from box3_play_audio import FIRMWARE_VOLUME_MAX, FIRMWARE_VOLUME_MIN
from box3_play_audio import run as play_audio
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice


DEFAULT_VOICE = "pl_PL-bass-high"
VOICE_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
KNOWN_VOICES = {
    "pl_PL-bass-high": "pl/pl_PL/bass/high",
    "pl_PL-darkman-medium": "pl/pl_PL/darkman/medium",
    "pl_PL-gosia-medium": "pl/pl_PL/gosia/medium",
    "pl_PL-mc_speech-medium": "pl/pl_PL/mc_speech/medium",
    "pl_PL-mls_6892-low": "pl/pl_PL/mls_6892/low",
}
STREAM_PATH = "/tts.wav"
STREAM_CHUNK_BYTES = 8192
DEFAULT_WYOMING_HOST = os.environ.get("BOX3_TTS_SERVER_HOST", "127.0.0.1")
DEFAULT_WYOMING_PORT = int(os.environ.get("BOX3_TTS_SERVER_PORT", "10200"))
WAV_HEADER_BYTES = 44


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


def wav_header(rate: int, width: int, channels: int) -> bytes:
    # Use an intentionally large data size for live HTTP streaming.
    data_size = 0x7FFFFFF0
    byte_rate = rate * channels * width
    block_align = channels * width
    riff_size = data_size + WAV_HEADER_BYTES - 8
    return (
        b"RIFF"
        + riff_size.to_bytes(4, "little")
        + b"WAVE"
        + b"fmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + channels.to_bytes(2, "little")
        + rate.to_bytes(4, "little")
        + byte_rate.to_bytes(4, "little")
        + block_align.to_bytes(2, "little")
        + (width * 8).to_bytes(2, "little")
        + b"data"
        + data_size.to_bytes(4, "little")
    )


async def synthesize_with_wyoming(
    text: str,
    voice: str,
    server_host: str,
    server_port: int,
    wav_path: Path,
) -> None:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    client = AsyncTcpClient(server_host, server_port)
    await client.connect()
    writer: wave.Wave_write | None = None
    try:
        await client.write_event(Synthesize(text=text, voice=SynthesizeVoice(name=voice)).event())
        while True:
            event = await client.read_event()
            if event is None:
                raise RuntimeError("Wyoming TTS server closed connection before audio stop")

            if AudioStart.is_type(event.type):
                audio_start = AudioStart.from_event(event)
                writer = wave.open(str(wav_path), "wb")
                writer.setnchannels(audio_start.channels)
                writer.setsampwidth(audio_start.width)
                writer.setframerate(audio_start.rate)
            elif AudioChunk.is_type(event.type):
                if writer is None:
                    raise RuntimeError("received Wyoming audio chunk before audio start")
                writer.writeframes(AudioChunk.from_event(event).audio)
            elif AudioStop.is_type(event.type):
                break
    finally:
        if writer is not None:
            writer.close()
        await client.disconnect()


def piper_stdout_command(model_path: Path) -> list[str]:
    return [
        "piper",
        "--model",
        str(model_path),
        "--output_file",
        "-",
    ]


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


class StreamingTTSHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, text: str, model_path: Path, stats: dict[str, Any], **kwargs: Any) -> None:
        self.text = text
        self.model_path = model_path
        self.stats = stats
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != STREAM_PATH:
            self.send_error(404)
            return

        stream_started = time.monotonic()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Connection", "close")
        self.end_headers()

        process = subprocess.Popen(
            piper_stdout_command(self.model_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(self.text.encode("utf-8"))
        process.stdin.close()

        byte_count = 0
        first_byte_at: float | None = None
        try:
            while True:
                chunk = process.stdout.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                if first_byte_at is None:
                    first_byte_at = time.monotonic()
                    self.stats["first_audio_seconds"] = first_byte_at - stream_started
                byte_count += len(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        finally:
            return_code = process.wait()
            stderr = process.stderr.read().decode("utf-8", errors="replace").strip()

        self.stats["stream_seconds"] = time.monotonic() - stream_started
        self.stats["stream_bytes"] = byte_count
        self.stats["return_code"] = return_code
        if stderr:
            self.stats["stderr"] = stderr


class WyomingStreamingTTSHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args: Any,
        text: str,
        voice: str,
        server_host: str,
        server_port: int,
        stats: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self.text = text
        self.voice = voice
        self.server_host = server_host
        self.server_port = server_port
        self.stats = stats
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != STREAM_PATH:
            self.send_error(404)
            return

        asyncio.run(self.stream_wyoming())

    async def stream_wyoming(self) -> None:
        stream_started = time.monotonic()
        self.stats["http_request_seen"] = True
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Connection", "close")
        self.end_headers()

        client = AsyncTcpClient(self.server_host, self.server_port)
        await client.connect()
        byte_count = 0
        first_chunk_at: float | None = None
        first_byte_sent_at: float | None = None

        try:
            await client.write_event(Synthesize(text=self.text, voice=SynthesizeVoice(name=self.voice)).event())
            while True:
                event = await client.read_event()
                if event is None:
                    raise RuntimeError("Wyoming TTS server closed connection before audio stop")

                if AudioStart.is_type(event.type):
                    audio_start = AudioStart.from_event(event)
                    header = wav_header(audio_start.rate, audio_start.width, audio_start.channels)
                    self.wfile.write(header)
                    self.wfile.flush()
                    byte_count += len(header)
                    first_byte_sent_at = time.monotonic()
                    self.stats["first_byte_sent_seconds"] = first_byte_sent_at - stream_started
                elif AudioChunk.is_type(event.type):
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic()
                        self.stats["first_audio_seconds"] = first_chunk_at - stream_started
                    chunk = AudioChunk.from_event(event).audio
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    byte_count += len(chunk)
                    if first_byte_sent_at is None:
                        first_byte_sent_at = time.monotonic()
                        self.stats["first_byte_sent_seconds"] = first_byte_sent_at - stream_started
                elif AudioStop.is_type(event.type):
                    break
        finally:
            await client.disconnect()

        self.stats["stream_seconds"] = time.monotonic() - stream_started
        self.stats["stream_bytes"] = byte_count


async def stream_to_box(
    host: str,
    text: str,
    model_path: Path,
    voice: str,
    engine: str,
    server_host: str,
    server_port: int,
    port: int,
    wait: float,
    volume: float | None,
) -> dict[str, float]:
    local_ip = local_ip_for(host)
    stats: dict[str, Any] = {}
    if engine == "wyoming":
        handler = partial(
            WyomingStreamingTTSHandler,
            text=text,
            voice=voice,
            server_host=server_host,
            server_port=server_port,
            stats=stats,
        )
    else:
        handler = partial(StreamingTTSHandler, text=text, model_path=model_path, stats=stats)
    server = ThreadingHTTPServer((local_ip, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://{local_ip}:{server.server_port}{STREAM_PATH}"
    client = make_client("piotr-box3-tts-stream", host)
    await client.connect(login=True)
    try:
        key = await media_player_key(client)
        send_started = time.monotonic()
        if volume is not None:
            effective_volume = FIRMWARE_VOLUME_MIN + (FIRMWARE_VOLUME_MAX - FIRMWARE_VOLUME_MIN) * volume
            print(f"setting volume={volume:.2f} effective_firmware_volume={effective_volume:.2f}")
            client.media_player_command(key, volume=volume)
            await asyncio.sleep(0.1)
        print(f"playing {url}")
        client.media_player_command(key, media_url=url, announcement=True)
        send_seconds = time.monotonic() - send_started
        await asyncio.sleep(wait)
    finally:
        await client.disconnect()
        server.shutdown()
        server.server_close()

    if engine == "cli" and stats.get("return_code") not in (None, 0):
        raise RuntimeError(f"piper exited with {stats['return_code']}: {stats.get('stderr', '')}")

    if not stats.get("http_request_seen"):
        log("warning: Box did not request the TTS stream before wait timeout")

    return {
        "send_seconds": send_seconds,
        "first_audio_seconds": float(stats.get("first_audio_seconds", 0.0)),
        "first_byte_sent_seconds": float(stats.get("first_byte_sent_seconds", 0.0)),
        "stream_seconds": float(stats.get("stream_seconds", 0.0)),
        "stream_bytes": float(stats.get("stream_bytes", 0.0)),
    }


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

    if args.stream:
        log(f"streaming engine={args.engine} voice={args.voice} chars={len(text)}")
        playback_timing = await stream_to_box(
            args.host,
            text,
            model_path,
            args.voice,
            args.engine,
            args.server_host,
            args.server_port,
            args.port,
            args.wait,
            args.volume,
        )
        log(f"tts_send_seconds={playback_timing['send_seconds']:.3f}")
        log(f"tts_first_audio_seconds={playback_timing['first_audio_seconds']:.3f}")
        log(f"tts_first_byte_sent_seconds={playback_timing['first_byte_sent_seconds']:.3f}")
        log(f"tts_stream_seconds={playback_timing['stream_seconds']:.3f}")
        log(f"tts_stream_bytes={playback_timing['stream_bytes']:.0f}")
        return 0

    output_dir = args.output_dir
    wav_path = timestamped_path(output_dir, ".wav")
    flac_path = timestamped_path(output_dir, ".flac")

    log(f"synthesizing voice={args.voice} chars={len(text)}")
    generated_started = time.monotonic()
    if args.engine == "wyoming":
        await synthesize_with_wyoming(text, args.voice, args.server_host, args.server_port, wav_path)
    else:
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
    parser.add_argument("--engine", choices=("wyoming", "cli"), default="wyoming")
    parser.add_argument("--server-host", default=DEFAULT_WYOMING_HOST)
    parser.add_argument("--server-port", type=int, default=DEFAULT_WYOMING_PORT)
    parser.add_argument("--cache-dir", type=Path, default=Path(os.environ.get("PIPER_HOME", ".piper-cache")))
    parser.add_argument("--output-dir", type=Path, default=Path("audio/tts"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--wait", type=float, default=8.0)
    parser.add_argument("--volume", type=float, default=1.0)
    parser.add_argument("--keep-wav", action="store_true")
    parser.add_argument("--stream", action="store_true", help="Experimental: stream Piper WAV output over HTTP instead of pre-rendering FLAC.")
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
