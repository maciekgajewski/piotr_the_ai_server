from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from typing import Any

from ai_server.config import MicrophoneConfig
from ai_server.microphones.types import MicrophoneContext, MicrophoneUtterance, PlaybackTarget


API_PORT = 6053


class Box3EsphomeMicrophoneDriver:
    def __init__(
        self,
        context: MicrophoneContext,
        playback_target: PlaybackTarget,
        client_info: str = "ai-server-box3-mic",
    ) -> None:
        self.context = context
        self.playback_target = playback_target
        self._client_info = client_info
        self._client = None
        self._unsubscribe = None
        self._stream_started = asyncio.Event()
        self._stream_done = asyncio.Event()
        self._chunks: list[bytes] = []
        self._wake_word: str | None = None

    @classmethod
    def from_config(cls, config: MicrophoneConfig) -> Box3EsphomeMicrophoneDriver:
        expected_name = config.options.get("expected_name")
        if expected_name is not None and not isinstance(expected_name, str):
            raise ValueError(f"microphone {config.name} expected_name must be a string when provided")

        context = MicrophoneContext(type=config.type, name=config.name, location=config.location)
        playback_target = PlaybackTarget(
            type=config.type,
            name=config.name,
            address=config.options["address"],
            api_key=config.options["api_key"],
            expected_name=expected_name,
        )
        return cls(context=context, playback_target=playback_target)

    async def wait_for_utterance(self, capture_seconds: float) -> MicrophoneUtterance:
        await self._ensure_connected()
        self._stream_started.clear()
        self._stream_done.clear()

        await self._stream_started.wait()
        await asyncio.sleep(capture_seconds)
        self._send_voice_assistant_event("VOICE_ASSISTANT_STT_VAD_END")
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stream_done.wait(), timeout=5)
        self._send_voice_assistant_event("VOICE_ASSISTANT_RUN_END")

        chunks = tuple(self._chunks)
        wake_word = self._wake_word
        self._chunks.clear()
        self._wake_word = None
        self._stream_started.clear()
        self._stream_done.clear()
        await asyncio.sleep(0.2)

        return MicrophoneUtterance(audio_chunks=chunks, wake_word=wake_word)

    async def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            return

        import aioesphomeapi

        self._client = aioesphomeapi.APIClient(
            self.playback_target.address,
            API_PORT,
            password=None,
            client_info=self._client_info,
            noise_psk=self.playback_target.api_key,
            expected_name=self.playback_target.expected_name,
        )
        await self._client.connect(login=True)
        connected_address = self._client.connected_address
        if isinstance(connected_address, str) and connected_address:
            self.playback_target = replace(self.playback_target, address=connected_address)
        elif isinstance(connected_address, tuple) and connected_address:
            self.playback_target = replace(self.playback_target, address=str(connected_address[0]))
        self._unsubscribe = self._client.subscribe_voice_assistant(
            handle_start=self._handle_start,
            handle_audio=self._handle_audio,
            handle_stop=self._handle_stop,
        )

    async def _handle_start(
        self,
        _conversation_id: str,
        _flags: int,
        _audio_settings: Any,
        wake_word_phrase: str | None,
    ) -> int:
        self._chunks.clear()
        self._wake_word = wake_word_phrase
        self._stream_done.clear()
        self._stream_started.set()
        return 0

    async def _handle_audio(self, data: bytes, data2: bytes | None = None) -> None:
        if data:
            self._chunks.append(data)
        if data2:
            self._chunks.append(data2)

    async def _handle_stop(self, _aborted: bool) -> None:
        self._stream_done.set()

    def _send_voice_assistant_event(self, event_name: str) -> None:
        import aioesphomeapi

        event = getattr(aioesphomeapi.VoiceAssistantEventType, event_name)
        self._client.send_voice_assistant_event(event, None)
