from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from aiohttp import ClientSession, ClientTimeout

from ai_server.agent_loop.interfaces import HttpSession


@dataclass(frozen=True)
class _BackoffKey:
    main_model: str
    fallback_model: str


class _OllamaChatStatusError(RuntimeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"Ollama chat failed with status {status}")
        self.status = status


class AgentLoopOllamaConnection:
    def __init__(
        self,
        *,
        base_url: str,
        session: HttpSession | None = None,
        now_factory: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._now_factory = now_factory
        self._backoff_until: dict[_BackoffKey, float] = {}
        self._logger = logging.getLogger(f"{__name__}.AgentLoopOllamaConnection[{self._base_url}]")

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def chat(
        self,
        payload: dict[str, Any],
        *,
        model: str,
        fallback_model: str | None,
        fallback_backoff_seconds: float,
        request_timeout_seconds: float | None,
    ) -> dict[str, Any]:
        if fallback_model is not None and self._is_backing_off(model, fallback_model):
            self._logger.info("using fallback model during backoff model=%s fallback_model=%s", model, fallback_model)
            return await self._post_chat(
                _payload_with_model(payload, fallback_model),
                request_timeout_seconds=request_timeout_seconds,
            )

        try:
            return await self._post_chat(
                _payload_with_model(payload, model),
                request_timeout_seconds=request_timeout_seconds,
            )
        except _OllamaChatStatusError as exc:
            if fallback_model is None:
                raise
            self._activate_backoff(
                model,
                fallback_model,
                status=exc.status,
                fallback_backoff_seconds=fallback_backoff_seconds,
            )
            return await self._post_chat(
                _payload_with_model(payload, fallback_model),
                request_timeout_seconds=request_timeout_seconds,
            )

    def _is_backing_off(self, model: str, fallback_model: str) -> bool:
        key = _BackoffKey(model, fallback_model)
        backoff_until = self._backoff_until.get(key)
        if backoff_until is None:
            return False
        now = self._now_factory()
        if now < backoff_until:
            return True
        self._logger.info("fallback backoff expired model=%s fallback_model=%s", model, fallback_model)
        del self._backoff_until[key]
        return False

    def _activate_backoff(
        self,
        model: str,
        fallback_model: str,
        *,
        status: int,
        fallback_backoff_seconds: float,
    ) -> None:
        backoff_until = self._now_factory() + fallback_backoff_seconds
        self._backoff_until[_BackoffKey(model, fallback_model)] = backoff_until
        self._logger.warning(
            "main model failed in Ollama status=%s model=%s fallback_model=%s fallback_backoff_seconds=%s",
            status,
            model,
            fallback_model,
            fallback_backoff_seconds,
        )

    async def _post_chat(
        self,
        payload: dict[str, Any],
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

        url = f"{self._base_url}/api/chat"
        self._logger.debug("Ollama chat request: %s", payload)
        async with session.post(url, json=payload, timeout=timeout) as response:
            if response.status >= 400:
                raise _OllamaChatStatusError(response.status)
            body = await response.json()
        if not isinstance(body, dict):
            raise ValueError("Ollama chat response must be a JSON object")
        self._logger.debug("Ollama chat response: %s", body)
        return body


def _payload_with_model(payload: dict[str, Any], model: str) -> dict[str, Any]:
    request_payload = dict(payload)
    request_payload["model"] = model
    return request_payload
