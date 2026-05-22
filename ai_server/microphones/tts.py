from __future__ import annotations

import asyncio
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import socket
import subprocess
import threading
from typing import Any
import urllib.request

from ai_server.config import TtsConfig
from ai_server.microphones.types import PlaybackTarget


API_PORT = 6053
STREAM_PATH = "/tts.wav"
STREAM_CHUNK_BYTES = 8192
WAV_HEADER_BYTES = 44
DEFAULT_CACHE_DIR = Path(".piper-cache")
KNOWN_VOICES = {
    "pl_PL-bass-high": "pl/pl_PL/bass/high",
    "pl_PL-darkman-medium": "pl/pl_PL/darkman/medium",
    "pl_PL-gosia-medium": "pl/pl_PL/gosia/medium",
    "pl_PL-mc_speech-medium": "pl/pl_PL/mc_speech/medium",
    "pl_PL-mls_6892-low": "pl/pl_PL/mls_6892/low",
}
VOICE_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
FIRMWARE_VOLUME_MIN = 0.5
FIRMWARE_VOLUME_MAX = 0.8


class PiperTextToSpeech:
    def __init__(self, config: TtsConfig, cache_dir: Path = DEFAULT_CACHE_DIR, wait: float = 8.0) -> None:
        self._config = config
        self._cache_dir = cache_dir
        self._wait = wait

    async def speak(self, target: PlaybackTarget, text: str) -> None:
        model_path = await asyncio.to_thread(_ensure_voice, self._cache_dir, self._config.voice)
        await _stream_to_box(
            target=target,
            text=text,
            model_path=model_path,
            volume=self._config.volume,
            wait=self._wait,
        )

    async def close(self) -> None:
        pass


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

        self.stats["http_request_seen"] = True
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Connection", "close")
        self.end_headers()

        process = subprocess.Popen(
            ["piper", "--model", str(self.model_path), "--output_file", "-"],
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
        try:
            while True:
                chunk = process.stdout.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                byte_count += len(chunk)
        finally:
            return_code = process.wait()
            stderr = process.stderr.read().decode("utf-8", errors="replace").strip()

        self.stats["stream_bytes"] = byte_count
        self.stats["return_code"] = return_code
        self.stats["stderr"] = stderr


async def _stream_to_box(
    target: PlaybackTarget,
    text: str,
    model_path: Path,
    volume: float,
    wait: float,
) -> None:
    connect_host = await _resolve_connect_host(target.address)
    local_ip = _local_ip_for(connect_host)
    stats: dict[str, Any] = {}
    handler = partial(StreamingTTSHandler, text=text, model_path=model_path, stats=stats)
    server = ThreadingHTTPServer((local_ip, 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://{local_ip}:{server.server_port}{STREAM_PATH}"
    client = _make_esphome_client(target, "ai-server-box3-tts")
    try:
        await client.connect(login=True)
        key = await _media_player_key(client)
        if volume is not None:
            client.media_player_command(key, volume=volume)
            await asyncio.sleep(0.1)
        client.media_player_command(key, media_url=url, announcement=True)
        await asyncio.sleep(wait)
    finally:
        await client.disconnect()
        server.shutdown()
        server.server_close()

    if stats.get("return_code") not in (None, 0):
        raise RuntimeError(f"piper exited with {stats['return_code']}: {stats.get('stderr', '')}")


def _ensure_voice(cache_dir: Path, voice: str) -> Path:
    directory = cache_dir / "voices" / voice
    model_path = directory / f"{voice}.onnx"
    config_path = directory / f"{voice}.onnx.json"
    voice_dir = KNOWN_VOICES.get(voice)
    if voice_dir is None:
        raise ValueError(f"unknown Piper voice: {voice}")

    model_url = f"{VOICE_BASE_URL}/{voice_dir}/{voice}.onnx"
    config_url = f"{VOICE_BASE_URL}/{voice_dir}/{voice}.onnx.json"
    if not model_path.exists():
        _download(model_url, model_path)
    if not config_path.exists():
        _download(config_url, config_path)
    return model_path


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = destination.with_suffix(destination.suffix + ".tmp")
    with urllib.request.urlopen(url) as response, tmp_path.open("wb") as output:
        shutil.copyfileobj(response, output)
    tmp_path.replace(destination)


async def _resolve_connect_host(host: str, port: int = API_PORT) -> str:
    import aioesphomeapi.host_resolver

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
    import aioesphomeapi

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
