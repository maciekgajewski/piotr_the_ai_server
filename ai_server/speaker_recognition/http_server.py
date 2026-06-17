from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any
import wave

from aiohttp import web

from ai_server.speaker_recognition.profiles import RecognitionResult, load_named_profiles, load_profiles, recognize_speaker
from ai_server.speaker_recognition.speechbrain_ecapa import SpeechBrainEcapaEmbedder


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 2140
DEFAULT_THRESHOLD = 0.45


class SpeakerRecognitionHttpServer:
    def __init__(
        self,
        *,
        profiles_dir: Path,
        embedder: SpeechBrainEcapaEmbedder,
        threshold: float,
    ) -> None:
        self._profiles_dir = profiles_dir
        self._embedder = embedder
        self._threshold = threshold
        self._profiles = load_profiles(profiles_dir)

    def app(self) -> web.Application:
        app = web.Application()
        app.add_routes(
            [
                web.get("/health", self.health),
                web.post("/recognize", self.recognize),
                web.post("/recognize-stream", self.recognize_stream),
                web.post("/reload", self.reload),
            ]
        )
        return app

    async def health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "profile_count": len(self._profiles)})

    async def reload(self, _request: web.Request) -> web.Response:
        self._profiles = load_profiles(self._profiles_dir)
        return web.json_response({"status": "ok", "profile_count": len(self._profiles)})

    async def recognize(self, request: web.Request) -> web.Response:
        payload = await request.read()
        if not payload:
            raise web.HTTPBadRequest(text="request body must contain WAV audio")
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio_file:
            audio_file.write(payload)
            audio_file.flush()
            embedding = self._embedder.embed_wav(Path(audio_file.name))
        result = recognize_speaker(embedding, self._profiles, threshold=self._threshold)
        return web.json_response(result_to_json(result))

    async def recognize_stream(self, request: web.Request) -> web.Response:
        profiles = load_named_profiles(parse_voice_profiles_header(request))
        if not profiles:
            raise web.HTTPBadRequest(text="X-Voice-Profiles must contain at least one profile")
        audio_format = audio_format_from_headers(request)
        payload = bytearray()
        async for chunk in request.content.iter_chunked(8192):
            payload.extend(chunk)
        if not payload:
            raise web.HTTPBadRequest(text="request body must contain PCM audio")

        with tempfile.NamedTemporaryFile(suffix=".wav") as audio_file:
            write_pcm_wav(Path(audio_file.name), bytes(payload), audio_format)
            embedding = self._embedder.embed_wav(Path(audio_file.name))
        result = recognize_speaker(embedding, profiles, threshold=self._threshold)
        return web.json_response(result_to_json(result))


def result_to_json(result: RecognitionResult) -> dict[str, Any]:
    return {
        "recognized_user": result.recognized_user,
        "confidence": result.confidence,
        "score": result.score,
        "threshold": result.threshold,
        "margin_to_second_best": result.margin_to_second_best,
        "profile": str(result.profile_path) if result.profile_path is not None else None,
    }


def parse_voice_profiles_header(request: web.Request) -> dict[str, str]:
    raw_value = request.headers.get("X-Voice-Profiles", "{}")
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise web.HTTPBadRequest(text="X-Voice-Profiles must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="X-Voice-Profiles must be a JSON object")
    profiles = {}
    for user, path in payload.items():
        if not isinstance(user, str) or not user or not isinstance(path, str) or not path:
            raise web.HTTPBadRequest(text="X-Voice-Profiles must map user names to profile paths")
        profiles[user] = path
    return profiles


def audio_format_from_headers(request: web.Request) -> tuple[int, int, int]:
    sample_rate = positive_int_header(request, "X-Audio-Sample-Rate")
    sample_width = positive_int_header(request, "X-Audio-Sample-Width")
    channels = positive_int_header(request, "X-Audio-Channels")
    if sample_rate != 16000:
        raise web.HTTPBadRequest(text=f"X-Audio-Sample-Rate must be 16000, got {sample_rate}")
    if sample_width != 2:
        raise web.HTTPBadRequest(text=f"X-Audio-Sample-Width must be 2, got {sample_width}")
    if channels != 1:
        raise web.HTTPBadRequest(text=f"X-Audio-Channels must be 1, got {channels}")
    return sample_rate, sample_width, channels


def positive_int_header(request: web.Request, name: str) -> int:
    raw_value = request.headers.get(name)
    if raw_value is None:
        raise web.HTTPBadRequest(text=f"{name} is required")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"{name} must be an integer") from exc
    if value <= 0:
        raise web.HTTPBadRequest(text=f"{name} must be positive")
    return value


def write_pcm_wav(path: Path, payload: bytes, audio_format: tuple[int, int, int]) -> None:
    sample_rate, sample_width, channels = audio_format
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(payload)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve SpeechBrain ECAPA-TDNN speaker recognition over HTTP.")
    parser.add_argument("--profiles-dir", type=Path, required=True)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--model-source", default="speechbrain/spkrec-ecapa-voxceleb")
    parser.add_argument("--model-savedir", type=Path, default=None)
    parser.add_argument("--device", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = SpeakerRecognitionHttpServer(
        profiles_dir=args.profiles_dir,
        embedder=SpeechBrainEcapaEmbedder(
            model_source=args.model_source,
            model_savedir=args.model_savedir,
            device=args.device,
        ),
        threshold=args.threshold,
    )
    web.run_app(server.app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
