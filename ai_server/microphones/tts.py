from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import socket
import threading
import time
from typing import Any

from ai_server.config import TtsConfig
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioEvent, AudioStart
from ai_server.microphones.types import PlaybackTarget

try:
    import aioesphomeapi
    import aioesphomeapi.host_resolver
except ModuleNotFoundError:
    aioesphomeapi = None

try:
    from wyoming.audio import AudioChunk as WyomingAudioChunk
    from wyoming.audio import AudioStart as WyomingAudioStart
    from wyoming.audio import AudioStop as WyomingAudioStop
    from wyoming.client import AsyncTcpClient
    from wyoming.tts import Synthesize, SynthesizeVoice
except ModuleNotFoundError:
    WyomingAudioChunk = None
    WyomingAudioStart = None
    WyomingAudioStop = None
    AsyncTcpClient = None
    Synthesize = None
    SynthesizeVoice = None


API_PORT = 6053
STREAM_PATH = "/tts.wav"
WAV_HEADER_BYTES = 44
STREAM_REQUEST_TIMEOUT_SECONDS = 5.0
STREAM_DONE_TIMEOUT_SECONDS = 30.0
WYOMING_PIPER_HOST = "127.0.0.1"
WYOMING_PIPER_PORT = 10200
WYOMING_PIPER_START_TIMEOUT_SECONDS = 30.0
WYOMING_PIPER_VALIDATION_TEXT = "Dzień dobry."


class PiperTextToSpeech:
    def __init__(
        self,
        config: TtsConfig,
        server_host: str = WYOMING_PIPER_HOST,
        server_port: int = WYOMING_PIPER_PORT,
    ) -> None:
        self._config = config
        self._server_host = server_host
        self._server_port = server_port
        self._started = False
        self._logger = logging.getLogger(f"{__name__}.PiperTextToSpeech[{config.voice}]")

    async def start(self) -> None:
        if self._started:
            return

        self._logger.info(
            "connecting to required Wyoming Piper server voice=%s host=%s port=%s",
            self._config.voice,
            self._server_host,
            self._server_port,
        )
        await asyncio.to_thread(
            _wait_for_tcp_port,
            self._server_host,
            self._server_port,
            WYOMING_PIPER_START_TIMEOUT_SECONDS,
        )
        await _validate_wyoming_server(self._server_host, self._server_port, self._config.voice)
        self._started = True
        self._logger.info(
            "Wyoming Piper server ready voice=%s host=%s port=%s",
            self._config.voice,
            self._server_host,
            self._server_port,
        )

    async def speak(self, target: PlaybackTarget, text: str) -> None:
        if not self._started:
            await self.start()
        await _stream_to_box(
            target=target,
            text=text,
            voice=self._config.voice,
            server_host=self._server_host,
            server_port=self._server_port,
            volume=self._config.volume,
        )

    async def synthesize(self, text: str) -> AsyncIterator[AudioEvent]:
        if not self._started:
            await self.start()
        _require_wyoming()

        client = AsyncTcpClient(self._server_host, self._server_port)
        await client.connect()
        try:
            await client.write_event(Synthesize(text=text, voice=SynthesizeVoice(name=self._config.voice)).event())
            while True:
                event = await client.read_event()
                if event is None:
                    raise RuntimeError("Wyoming TTS server closed connection before audio stop")

                if WyomingAudioStart.is_type(event.type):
                    audio_start = WyomingAudioStart.from_event(event)
                    self._logger.debug("received Wyoming audio start event=%s", event)
                    yield AudioStart(
                        rate=audio_start.rate,
                        width=audio_start.width,
                        channels=audio_start.channels,
                        volume=self._config.volume,
                    )
                    continue

                if WyomingAudioChunk.is_type(event.type):
                    self._logger.debug("received Wyoming audio chunk event=%s", event.data)
                    yield AudioChunk(data=WyomingAudioChunk.from_event(event).audio)
                    continue

                if WyomingAudioStop.is_type(event.type):
                    self._logger.debug("received Wyoming audio stop event=%s", event)
                    yield AudioEnd()
                    return
        finally:
            await client.disconnect()

    async def close(self) -> None:
        self._started = False


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

        self.stats["http_request_seen"] = True
        self.stats["http_request_at"] = time.monotonic()
        self.stats["http_request_event"].set()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            asyncio.run(self._stream_wyoming())
        except Exception as exc:
            self.stats["error"] = exc
            raise
        finally:
            self.stats["stream_done_at"] = time.monotonic()
            self.stats["stream_done_event"].set()

    async def _stream_wyoming(self) -> None:
        _require_wyoming()

        client = AsyncTcpClient(self.server_host, self.server_port)
        await client.connect()
        byte_count = 0
        try:
            await client.write_event(Synthesize(text=self.text, voice=SynthesizeVoice(name=self.voice)).event())
            while True:
                event = await client.read_event()
                if event is None:
                    raise RuntimeError("Wyoming TTS server closed connection before audio stop")

                if WyomingAudioStart.is_type(event.type):
                    audio_start = WyomingAudioStart.from_event(event)
                    header = wav_header(audio_start.rate, audio_start.width, audio_start.channels)
                    self.wfile.write(header)
                    self.wfile.flush()
                    byte_count += len(header)
                    self.stats["first_byte_at"] = time.monotonic()
                elif WyomingAudioChunk.is_type(event.type):
                    chunk = WyomingAudioChunk.from_event(event).audio
                    if "first_audio_at" not in self.stats:
                        self.stats["first_audio_at"] = time.monotonic()
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    byte_count += len(chunk)
                    if "first_byte_at" not in self.stats:
                        self.stats["first_byte_at"] = time.monotonic()
                elif WyomingAudioStop.is_type(event.type):
                    break
        finally:
            await client.disconnect()
            self.stats["stream_bytes"] = byte_count


async def _stream_to_box(
    target: PlaybackTarget,
    text: str,
    voice: str,
    server_host: str,
    server_port: int,
    volume: float,
) -> None:
    logger = logging.getLogger(f"{__name__}.PiperTextToSpeech[{target.name}]")
    started_at = time.monotonic()
    connect_host = await _resolve_connect_host(target.address)
    local_ip = _local_ip_for(connect_host)
    stats: dict[str, Any] = {
        "http_request_event": threading.Event(),
        "stream_done_event": threading.Event(),
    }
    handler = partial(
        WyomingStreamingTTSHandler,
        text=text,
        voice=voice,
        server_host=server_host,
        server_port=server_port,
        stats=stats,
    )
    server = ThreadingHTTPServer((local_ip, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://{local_ip}:{server.server_port}{STREAM_PATH}"
    client = _make_esphome_client(target, "ai-server-box3-tts")
    command_sent_at: float | None = None
    try:
        await client.connect(login=True)
        key = await _media_player_key(client)
        command_started_at = time.monotonic()
        if volume is not None:
            client.media_player_command(key, volume=volume)
            await asyncio.sleep(0.1)
        client.media_player_command(key, media_url=url, announcement=True)
        command_sent_at = time.monotonic()
        logger.info(
            "media command sent url=%s chars=%s setup_ms=%s command_ms=%s",
            url,
            len(text),
            _elapsed_ms(started_at, command_started_at),
            _elapsed_ms(command_started_at, command_sent_at),
        )

        request_seen = await asyncio.to_thread(
            stats["http_request_event"].wait,
            STREAM_REQUEST_TIMEOUT_SECONDS,
        )
        if not request_seen:
            logger.warning(
                "BOX-3 did not request stream within %.1fs",
                STREAM_REQUEST_TIMEOUT_SECONDS,
            )
            return

        done_seen = await asyncio.to_thread(
            stats["stream_done_event"].wait,
            STREAM_DONE_TIMEOUT_SECONDS,
        )
        if not done_seen:
            logger.warning(
                "stream did not finish within timeout",
            )
    finally:
        await client.disconnect()
        server.shutdown()
        server.server_close()

    if stats.get("error") is not None:
        raise RuntimeError("Wyoming TTS stream failed") from stats["error"]

    logger.info(
        "stream complete request_ms=%s first_audio_ms=%s first_byte_ms=%s stream_ms=%s bytes=%s",
        _optional_elapsed_ms(command_sent_at, stats.get("http_request_at")),
        _optional_elapsed_ms(stats.get("http_request_at"), stats.get("first_audio_at")),
        _optional_elapsed_ms(stats.get("http_request_at"), stats.get("first_byte_at")),
        _optional_elapsed_ms(stats.get("http_request_at"), stats.get("stream_done_at")),
        stats.get("stream_bytes", 0),
    )


def wav_header(rate: int, width: int, channels: int) -> bytes:
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


def _can_connect(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_tcp_port(
    host: str,
    port: int,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _can_connect(host, port):
            return
        time.sleep(0.1)

    raise TimeoutError(f"required Wyoming Piper server was not reachable on {host}:{port} within {timeout:.1f}s")


async def _validate_wyoming_server(host: str, port: int, voice: str) -> None:
    _require_wyoming()

    client = AsyncTcpClient(host, port)
    await client.connect()
    saw_audio = False
    try:
        await client.write_event(
            Synthesize(text=WYOMING_PIPER_VALIDATION_TEXT, voice=SynthesizeVoice(name=voice)).event()
        )
        while True:
            event = await client.read_event()
            if event is None:
                raise RuntimeError("Wyoming TTS server closed connection during startup validation")
            if WyomingAudioStart.is_type(event.type):
                continue
            if WyomingAudioChunk.is_type(event.type):
                saw_audio = True
                continue
            if WyomingAudioStop.is_type(event.type):
                if not saw_audio:
                    raise RuntimeError("Wyoming TTS startup validation produced no audio")
                return
    finally:
        await client.disconnect()


async def _resolve_connect_host(host: str, port: int = API_PORT) -> str:
    _require_aioesphomeapi()

    results = await aioesphomeapi.host_resolver.async_resolve_host([host], port, timeout=10.0)
    for result in results:
        sockaddr = result.sockaddr
        if isinstance(sockaddr, tuple) and len(sockaddr) >= 2:
            return str(sockaddr[0])

    return host


def _local_ip_for(host: str, port: int = API_PORT) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, port))
        return sock.getsockname()[0]


def _make_esphome_client(target: PlaybackTarget, client_info: str):
    _require_aioesphomeapi()

    return aioesphomeapi.APIClient(
        target.address,
        API_PORT,
        password=None,
        client_info=client_info,
        noise_psk=target.api_key,
        expected_name=target.expected_name,
    )


async def _media_player_key(client) -> int:
    entities, _ = await client.list_entities_services()
    for entity in entities:
        if type(entity).__name__ == "MediaPlayerInfo":
            return entity.key
    raise RuntimeError("No media player entity exposed by microphone")


def _require_wyoming() -> None:
    if AsyncTcpClient is None:
        raise RuntimeError("wyoming package is required for Piper TTS streaming")


def _require_aioesphomeapi() -> None:
    if aioesphomeapi is None:
        raise RuntimeError("aioesphomeapi package is required for BOX-3 playback")


def _elapsed_ms(started_at: float, ended_at: float) -> int:
    return round((ended_at - started_at) * 1000)


def _optional_elapsed_ms(started_at: float | None, ended_at: float | None) -> int | None:
    if started_at is None or ended_at is None:
        return None
    return _elapsed_ms(started_at, ended_at)
