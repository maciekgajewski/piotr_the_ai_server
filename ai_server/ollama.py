from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientSession


OLLAMA_BASE_URL = "http://127.0.0.1:11434"


class OllamaError(Exception):
    """Raised when Ollama cannot serve a request."""


class OllamaClient:
    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        session: ClientSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session or ClientSession()
        self._owns_session = session is None
        self._logger = logging.getLogger(f"{__name__}.OllamaClient[{self._base_url}]")

    async def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/api/generate", payload, "generate")

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/api/chat", payload, "chat")

    async def close(self) -> None:
        if self._owns_session:
            await self._session.close()

    async def _post(self, path: str, payload: dict[str, Any], operation: str) -> dict[str, Any]:
        self._logger.debug("Ollama request: %s", payload)
        async with self._session.post(f"{self._base_url}{path}", json=payload) as response:
            if response.status >= 400:
                raise OllamaError(f"Ollama {operation} failed with status {response.status}")

            body = await response.json()
            if not isinstance(body, dict):
                raise OllamaError("Ollama response must be a JSON object")

            self._logger.debug("Ollama response: %s", body)
            return body
