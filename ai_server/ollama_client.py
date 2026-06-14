from __future__ import annotations

import logging
from typing import Any, Protocol

from aiohttp import ClientSession, ClientTimeout


OLLAMA_BASE_URL = "http://127.0.0.1:11434"


class _HttpResponse(Protocol):
    status: int

    async def __aenter__(self) -> "_HttpResponse":
        raise NotImplementedError

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        raise NotImplementedError

    async def json(self) -> dict[str, Any]:
        raise NotImplementedError


class _HttpSession(Protocol):
    def post(self, url: str, json: dict[str, Any], timeout: ClientTimeout | None = None) -> _HttpResponse:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class OllamaError(Exception):
    """Raised when Ollama cannot serve a request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OllamaClient:
    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        session: _HttpSession | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._logger = logging.getLogger(f"{__name__}.OllamaClient[{self._base_url}]")

    async def generate(self, payload: dict[str, Any], *, request_timeout_seconds: float | None = None) -> dict[str, Any]:
        return await self._post("/api/generate", payload, "generate", request_timeout_seconds=request_timeout_seconds)

    async def chat(self, payload: dict[str, Any], *, request_timeout_seconds: float | None = None) -> dict[str, Any]:
        return await self._post("/api/chat", payload, "chat", request_timeout_seconds=request_timeout_seconds)

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        operation: str,
        *,
        request_timeout_seconds: float | None,
    ) -> dict[str, Any]:
        session = self._session
        if session is None:
            session = ClientSession()
            self._session = session

        timeout = None
        if request_timeout_seconds is not None:
            timeout = ClientTimeout(total=request_timeout_seconds)

        url = f"{self._base_url}{path}"
        self._logger.debug("Ollama request: %s", payload)
        response_context = session.post(url, json=payload) if timeout is None else session.post(url, json=payload, timeout=timeout)
        async with response_context as response:
            if response.status >= 400:
                raise OllamaError(f"Ollama {operation} failed with status {response.status}", status=response.status)

            body = await response.json()
            if not isinstance(body, dict):
                raise OllamaError("Ollama response must be a JSON object")

            self._logger.debug("Ollama response: %s", body)
            return body
