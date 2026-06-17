from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any

import aiohttp

from ai_server.microphones.messages import AudioChunk


@dataclass(frozen=True)
class SpeakerRecognitionAudioFormat:
    sample_rate: int
    sample_width: int
    channels: int


@dataclass(frozen=True)
class SpeakerRecognitionResult:
    recognized_user: str | None
    confidence: float
    score: float
    threshold: float
    profile: str | None


class SpeakerRecognitionStream:
    def __init__(
        self,
        *,
        url: str,
        audio_format: SpeakerRecognitionAudioFormat,
        profiles: dict[str, str],
    ) -> None:
        self._url = url.rstrip("/")
        self._audio_format = audio_format
        self._profiles = dict(profiles)
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._run())

    async def send_audio(self, chunk: AudioChunk) -> None:
        await self._queue.put(chunk.data)

    async def end_audio(self) -> None:
        await self._queue.put(None)

    async def result(self) -> SpeakerRecognitionResult | None:
        return await self._task

    def cancel(self) -> None:
        self._task.cancel()

    async def _run(self) -> SpeakerRecognitionResult | None:
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Audio-Sample-Rate": str(self._audio_format.sample_rate),
            "X-Audio-Sample-Width": str(self._audio_format.sample_width),
            "X-Audio-Channels": str(self._audio_format.channels),
            "X-Voice-Profiles": json.dumps(self._profiles, ensure_ascii=False),
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._url}/recognize-stream",
                data=self._audio_chunks(),
                headers=headers,
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        return result_from_payload(payload)

    async def _audio_chunks(self):
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            if chunk:
                yield chunk


class SpeakerRecognitionClient:
    def __init__(
        self,
        *,
        url: str | None,
        timeout_seconds: float,
        profiles: dict[str, str],
    ) -> None:
        self._url = url.rstrip("/") if url else None
        self.timeout_seconds = timeout_seconds
        self._profiles = dict(profiles)

    @property
    def enabled(self) -> bool:
        return self._url is not None and bool(self._profiles)

    def start_stream(self, audio_format: SpeakerRecognitionAudioFormat) -> SpeakerRecognitionStream | None:
        if not self.enabled or self._url is None:
            return None
        return SpeakerRecognitionStream(
            url=self._url,
            audio_format=audio_format,
            profiles=self._profiles,
        )


def voice_profiles_from_users(users: dict[str, dict[str, Any]]) -> dict[str, str]:
    profiles: dict[str, str] = {}
    for user, settings in users.items():
        profile = settings.get("voice_profile") if isinstance(settings, dict) else None
        if isinstance(profile, str) and profile:
            profiles[user] = profile
    return profiles


def result_from_payload(payload: Any) -> SpeakerRecognitionResult:
    if not isinstance(payload, dict):
        raise ValueError("speaker recognition response must be an object")
    return SpeakerRecognitionResult(
        recognized_user=_optional_string(payload.get("recognized_user")),
        confidence=float(payload.get("confidence", 0.0)),
        score=float(payload.get("score", 0.0)),
        threshold=float(payload.get("threshold", 0.0)),
        profile=_optional_string(payload.get("profile")),
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
