from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from typing import Any, AsyncIterator, Protocol

from aiohttp import ClientSession, ClientTimeout


OLLAMA_BASE_URL = "http://127.0.0.1:11434"
GPU_RESIDENCY_REQUEST_TIMEOUT_SECONDS = 60.0


class _HttpResponse(Protocol):
    status: int

    async def __aenter__(self) -> "_HttpResponse":
        raise NotImplementedError

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        raise NotImplementedError

    async def json(self) -> dict[str, Any]:
        raise NotImplementedError


class _HttpSession(Protocol):
    def get(self, url: str, timeout: ClientTimeout | None = None) -> _HttpResponse:
        raise NotImplementedError

    def post(self, url: str, json: dict[str, Any], timeout: ClientTimeout | None = None) -> _HttpResponse:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


class OllamaError(Exception):
    """Raised when Ollama cannot serve a request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OllamaResidencyError(OllamaError):
    """Raised when the protected local model cannot be kept fully in GPU memory."""


class _RequestGate:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._active_requests = 0
        self._exclusive_active = False
        self._exclusive_waiters = 0

    @asynccontextmanager
    async def request(self) -> AsyncIterator[None]:
        async with self._condition:
            await self._condition.wait_for(lambda: not self._exclusive_active and self._exclusive_waiters == 0)
            self._active_requests += 1
        try:
            yield
        finally:
            async with self._condition:
                assert self._active_requests > 0
                self._active_requests -= 1
                self._condition.notify_all()

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        async with self._condition:
            self._exclusive_waiters += 1
            try:
                await self._condition.wait_for(lambda: not self._exclusive_active and self._active_requests == 0)
                self._exclusive_active = True
            finally:
                self._exclusive_waiters -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            async with self._condition:
                assert self._exclusive_active
                self._exclusive_active = False
                self._condition.notify_all()


class OllamaConnection:
    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        session: _HttpSession | None = None,
        protected_model: str | None = None,
    ) -> None:
        if protected_model is not None and not protected_model:
            raise ValueError("protected_model must be a non-empty string when provided")
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._protected_model = protected_model
        self._request_gate = _RequestGate()
        self._closed = False
        self._logger = logging.getLogger(f"{__name__}.OllamaConnection[{self._base_url}]")

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def protected_model(self) -> str | None:
        return self._protected_model

    async def generate(self, payload: dict[str, Any], *, request_timeout_seconds: float | None = None) -> dict[str, Any]:
        return await self._request(
            "/api/generate",
            self._pin_protected_model(payload),
            "generate",
            request_timeout_seconds=request_timeout_seconds,
        )

    async def chat(self, payload: dict[str, Any], *, request_timeout_seconds: float | None = None) -> dict[str, Any]:
        return await self._request(
            "/api/chat",
            self._pin_protected_model(payload),
            "chat",
            request_timeout_seconds=request_timeout_seconds,
        )

    async def ensure_protected_model_gpu_residency(self) -> None:
        protected_model = self._protected_model
        if protected_model is None:
            return

        async with self._request_gate.exclusive():
            self._raise_if_closed()
            running_models = await self._running_models_unleased(
                request_timeout_seconds=GPU_RESIDENCY_REQUEST_TIMEOUT_SECONDS,
            )
            protected_entry = _find_model(running_models, protected_model)
            if protected_entry is not None and _is_fully_gpu_resident(protected_entry):
                self._logger.debug(
                    "protected model is fully GPU-resident model=%s size=%s size_vram=%s",
                    protected_model,
                    protected_entry["size"],
                    protected_entry["size_vram"],
                )
                return

            self._logger.warning(
                "protected model GPU residency check failed; starting repair model=%s state=%s",
                protected_model,
                _residency_state(protected_entry),
            )
            for running_model in running_models:
                if running_model is protected_entry:
                    continue
                running_model_name = _model_name(running_model)
                if running_model_name is None:
                    continue
                self._logger.warning(
                    "unloading competing Ollama model during residency repair protected_model=%s model=%s",
                    protected_model,
                    running_model_name,
                )
                await self._post_unleased(
                    "/api/generate",
                    {"model": running_model_name, "stream": False, "keep_alive": 0},
                    "unload",
                    request_timeout_seconds=GPU_RESIDENCY_REQUEST_TIMEOUT_SECONDS,
                )

            if protected_entry is not None:
                self._logger.warning("unloading protected model before residency reload model=%s", protected_model)
                await self._post_unleased(
                    "/api/generate",
                    {"model": protected_model, "stream": False, "keep_alive": 0},
                    "unload",
                    request_timeout_seconds=GPU_RESIDENCY_REQUEST_TIMEOUT_SECONDS,
                )

            self._logger.warning("reloading protected model for GPU residency model=%s", protected_model)
            await self._post_unleased(
                "/api/generate",
                {"model": protected_model, "stream": False, "keep_alive": -1},
                "load",
                request_timeout_seconds=GPU_RESIDENCY_REQUEST_TIMEOUT_SECONDS,
            )

            repaired_models = await self._running_models_unleased(
                request_timeout_seconds=GPU_RESIDENCY_REQUEST_TIMEOUT_SECONDS,
            )
            repaired_entry = _find_model(repaired_models, protected_model)
            if repaired_entry is None or not _is_fully_gpu_resident(repaired_entry):
                raise OllamaResidencyError(
                    f"protected Ollama model {protected_model} is not fully GPU-resident after repair: "
                    f"{_residency_state(repaired_entry)}"
                )
            self._logger.info(
                "protected model GPU residency repaired model=%s size=%s size_vram=%s",
                protected_model,
                repaired_entry["size"],
                repaired_entry["size_vram"],
            )

    async def close(self) -> None:
        async with self._request_gate.exclusive():
            if self._closed:
                return
            self._closed = True
            if self._owns_session and self._session is not None:
                await self._session.close()
                self._session = None

    def _pin_protected_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("model") != self._protected_model:
            return payload
        pinned_payload = dict(payload)
        pinned_payload["keep_alive"] = -1
        return pinned_payload

    async def _request(
        self,
        path: str,
        payload: dict[str, Any],
        operation: str,
        *,
        request_timeout_seconds: float | None,
    ) -> dict[str, Any]:
        async with self._request_gate.request():
            self._raise_if_closed()
            return await self._post_unleased(
                path,
                payload,
                operation,
                request_timeout_seconds=request_timeout_seconds,
            )

    async def _running_models_unleased(
        self,
        *,
        request_timeout_seconds: float | None,
    ) -> list[dict[str, Any]]:
        body = await self._get_unleased(
            "/api/ps",
            "list running models",
            request_timeout_seconds=request_timeout_seconds,
        )
        models = body.get("models")
        if not isinstance(models, list) or not all(isinstance(model, dict) for model in models):
            raise OllamaError("Ollama list running models response must contain a models list")
        return models

    async def _post_unleased(
        self,
        path: str,
        payload: dict[str, Any],
        operation: str,
        *,
        request_timeout_seconds: float | None,
    ) -> dict[str, Any]:
        session = self._session_for_request()

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

    async def _get_unleased(
        self,
        path: str,
        operation: str,
        *,
        request_timeout_seconds: float | None,
    ) -> dict[str, Any]:
        session = self._session_for_request()
        timeout = None
        if request_timeout_seconds is not None:
            timeout = ClientTimeout(total=request_timeout_seconds)
        url = f"{self._base_url}{path}"
        self._logger.debug("Ollama GET request path=%s", path)
        response_context = session.get(url) if timeout is None else session.get(url, timeout=timeout)
        async with response_context as response:
            if response.status >= 400:
                raise OllamaError(f"Ollama {operation} failed with status {response.status}", status=response.status)
            body = await response.json()
            if not isinstance(body, dict):
                raise OllamaError("Ollama response must be a JSON object")
            self._logger.debug("Ollama response: %s", body)
            return body

    def _session_for_request(self) -> _HttpSession:
        self._raise_if_closed()
        session = self._session
        if session is None:
            session = ClientSession()
            self._session = session
        return session

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise OllamaError("Ollama connection is closed")


def _find_model(running_models: list[dict[str, Any]], configured_model: str) -> dict[str, Any] | None:
    matches = [model for model in running_models if model.get("model") == configured_model or model.get("name") == configured_model]
    if len(matches) > 1:
        raise OllamaError(f"Ollama returned multiple running entries for model {configured_model}")
    return matches[0] if matches else None


def _model_name(model: dict[str, Any]) -> str | None:
    name = model.get("model")
    if isinstance(name, str) and name:
        return name
    name = model.get("name")
    if isinstance(name, str) and name:
        return name
    return None


def _is_fully_gpu_resident(model: dict[str, Any]) -> bool:
    size = model.get("size")
    size_vram = model.get("size_vram")
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
        and isinstance(size_vram, int)
        and not isinstance(size_vram, bool)
        and size_vram == size
    )


def _residency_state(model: dict[str, Any] | None) -> str:
    if model is None:
        return "not_loaded"
    return f"size={model.get('size')!r} size_vram={model.get('size_vram')!r}"
