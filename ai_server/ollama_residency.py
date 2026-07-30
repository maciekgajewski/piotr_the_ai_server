from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ai_server.ollama_connection import OllamaConnection


MODEL_RESIDENCY_CHECK_INTERVAL_SECONDS = 60.0


class OllamaResidencySupervisor:
    def __init__(
        self,
        connection: OllamaConnection,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if connection.protected_model is None:
            raise ValueError("Ollama residency supervision requires a protected model")
        self._connection = connection
        self._sleep = sleep
        self._logger = logging.getLogger(
            f"{__name__}.OllamaResidencySupervisor[{connection.protected_model}]"
        )

    async def verify_startup(self) -> None:
        self._logger.info(
            "verifying startup GPU residency model=%s",
            self._connection.protected_model,
        )
        await self._connection.ensure_protected_model_gpu_residency()
        self._logger.info(
            "startup GPU residency verified model=%s",
            self._connection.protected_model,
        )

    async def run(self) -> None:
        self._logger.info(
            "periodic GPU residency supervision started model=%s interval_seconds=%s",
            self._connection.protected_model,
            MODEL_RESIDENCY_CHECK_INTERVAL_SECONDS,
        )
        while True:
            await self._sleep(MODEL_RESIDENCY_CHECK_INTERVAL_SECONDS)
            self._logger.debug(
                "running periodic GPU residency verification model=%s",
                self._connection.protected_model,
            )
            await self._connection.ensure_protected_model_gpu_residency()
