from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from ai_server.agent_loop.interfaces import HttpSession
from ai_server.ollama_client import OllamaClient, OllamaError


@dataclass(frozen=True)
class _BackoffKey:
    main_model: str
    fallback_model: str


class AgentLoopOllamaConnection:
    def __init__(
        self,
        *,
        base_url: str,
        session: HttpSession | None = None,
        now_factory: Callable[[], float] = time.monotonic,
        ollama_client: OllamaClient | None = None,
        local_first_cloud_grace_seconds: float = 10.0,
    ) -> None:
        if local_first_cloud_grace_seconds <= 0:
            raise ValueError("local_first_cloud_grace_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._ollama_client = ollama_client or OllamaClient(base_url=self._base_url, session=session)
        self._owns_ollama_client = ollama_client is None
        self._now_factory = now_factory
        self._local_first_cloud_grace_seconds = local_first_cloud_grace_seconds
        self._backoff_until: dict[_BackoffKey, float] = {}
        self._logger = logging.getLogger(f"{__name__}.AgentLoopOllamaConnection[{self._base_url}]")

    async def close(self) -> None:
        if self._owns_ollama_client:
            await self._ollama_client.close()

    async def chat(
        self,
        payload: dict[str, Any],
        *,
        model: str,
        fallback_model: str | None,
        fallback_backoff_seconds: float,
        request_timeout_seconds: float | None,
    ) -> dict[str, Any]:
        if fallback_model is None:
            self._logger.info("agent loop Ollama path=cloud cloud_model=%s local_model=%s", model, fallback_model)
            return await self._chat_with_model(
                payload,
                model,
                request_timeout_seconds=request_timeout_seconds,
            )
        if self._is_backing_off(model, fallback_model):
            self._logger.info("agent loop Ollama path=local_backoff cloud_model=%s local_model=%s", model, fallback_model)
            return await self._chat_with_model(
                payload,
                fallback_model,
                request_timeout_seconds=request_timeout_seconds,
            )

        return await self._race_cloud_and_local(
            payload,
            cloud_model=model,
            local_model=fallback_model,
            fallback_backoff_seconds=fallback_backoff_seconds,
            request_timeout_seconds=request_timeout_seconds,
        )

    async def _race_cloud_and_local(
        self,
        payload: dict[str, Any],
        *,
        cloud_model: str,
        local_model: str,
        fallback_backoff_seconds: float,
        request_timeout_seconds: float | None,
    ) -> dict[str, Any]:
        self._logger.info("agent loop Ollama path=race cloud_model=%s local_model=%s", cloud_model, local_model)
        cloud_task = asyncio.create_task(
            self._chat_with_model(
                payload,
                cloud_model,
                request_timeout_seconds=request_timeout_seconds,
            )
        )
        local_task = asyncio.create_task(
            self._chat_with_model(
                payload,
                local_model,
                request_timeout_seconds=request_timeout_seconds,
            )
        )

        try:
            done, _pending = await asyncio.wait({cloud_task, local_task}, return_when=asyncio.FIRST_COMPLETED)
            if cloud_task in done:
                return await self._cloud_completed_first(
                    cloud_task,
                    local_task,
                    cloud_model=cloud_model,
                    local_model=local_model,
                    fallback_backoff_seconds=fallback_backoff_seconds,
                )
            return await self._local_completed_first(
                local_task,
                cloud_task,
                cloud_model=cloud_model,
                local_model=local_model,
                fallback_backoff_seconds=fallback_backoff_seconds,
            )
        except BaseException:
            await _cancel_task(cloud_task)
            await _cancel_task(local_task)
            raise

    async def _cloud_completed_first(
        self,
        cloud_task: asyncio.Task[dict[str, Any]],
        local_task: asyncio.Task[dict[str, Any]],
        *,
        cloud_model: str,
        local_model: str,
        fallback_backoff_seconds: float,
    ) -> dict[str, Any]:
        try:
            result = cloud_task.result()
        except OllamaError as exc:
            self._activate_backoff(
                cloud_model,
                local_model,
                status=exc.status,
                fallback_backoff_seconds=fallback_backoff_seconds,
            )
            self._logger.info("agent loop Ollama path=local_after_cloud_error cloud_model=%s local_model=%s", cloud_model, local_model)
            return await local_task
        self._logger.info("agent loop Ollama race winner=cloud cloud_model=%s local_model=%s", cloud_model, local_model)
        await _cancel_task(local_task)
        return result

    async def _local_completed_first(
        self,
        local_task: asyncio.Task[dict[str, Any]],
        cloud_task: asyncio.Task[dict[str, Any]],
        *,
        cloud_model: str,
        local_model: str,
        fallback_backoff_seconds: float,
    ) -> dict[str, Any]:
        try:
            local_result = local_task.result()
        except Exception as local_exc:
            try:
                cloud_result = await cloud_task
            except OllamaError as cloud_exc:
                self._activate_backoff(
                    cloud_model,
                    local_model,
                    status=cloud_exc.status,
                    fallback_backoff_seconds=fallback_backoff_seconds,
                )
                raise local_exc
            self._logger.info("agent loop Ollama race winner=cloud_after_local_error cloud_model=%s local_model=%s", cloud_model, local_model)
            return cloud_result

        self._logger.info(
            "agent loop Ollama local model completed first; waiting for cloud grace_seconds=%s cloud_model=%s local_model=%s",
            self._local_first_cloud_grace_seconds,
            cloud_model,
            local_model,
        )
        try:
            cloud_result = await asyncio.wait_for(
                asyncio.shield(cloud_task),
                timeout=self._local_first_cloud_grace_seconds,
            )
        except asyncio.TimeoutError:
            self._logger.info("agent loop Ollama race winner=local_after_cloud_grace_timeout cloud_model=%s local_model=%s", cloud_model, local_model)
            await _cancel_task(cloud_task)
            return local_result
        except OllamaError as exc:
            self._activate_backoff(
                cloud_model,
                local_model,
                status=exc.status,
                fallback_backoff_seconds=fallback_backoff_seconds,
            )
            self._logger.info("agent loop Ollama race winner=local_after_cloud_error cloud_model=%s local_model=%s", cloud_model, local_model)
            return local_result
        self._logger.info("agent loop Ollama race winner=cloud_after_local_grace cloud_model=%s local_model=%s", cloud_model, local_model)
        return cloud_result

    def _is_backing_off(self, model: str, fallback_model: str) -> bool:
        key = _BackoffKey(model, fallback_model)
        backoff_until = self._backoff_until.get(key)
        if backoff_until is None:
            return False
        now = self._now_factory()
        if now < backoff_until:
            return True
        self._logger.info("local backoff expired cloud_model=%s local_model=%s", model, fallback_model)
        del self._backoff_until[key]
        return False

    def _activate_backoff(
        self,
        model: str,
        fallback_model: str,
        *,
        status: int | None,
        fallback_backoff_seconds: float,
    ) -> None:
        backoff_until = self._now_factory() + fallback_backoff_seconds
        self._backoff_until[_BackoffKey(model, fallback_model)] = backoff_until
        self._logger.warning(
            "cloud model failed in Ollama status=%s cloud_model=%s local_model=%s local_backoff_seconds=%s",
            status,
            model,
            fallback_model,
            fallback_backoff_seconds,
        )

    async def _chat_with_model(
        self,
        payload: dict[str, Any],
        model: str,
        *,
        request_timeout_seconds: float | None,
    ) -> dict[str, Any]:
        return await self._ollama_client.chat(
            _payload_with_model(payload, model),
            request_timeout_seconds=request_timeout_seconds,
        )


def _payload_with_model(payload: dict[str, Any], model: str) -> dict[str, Any]:
    request_payload = dict(payload)
    request_payload["model"] = model
    return request_payload


async def _cancel_task(task: asyncio.Task[Any]) -> None:
    if task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
