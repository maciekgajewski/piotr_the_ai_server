from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar


DEFAULT_PROCESSING_UPDATE_INTERVAL_SECONDS = 5.0
ProcessingUpdateCallback = Callable[[], Awaitable[None]]
T = TypeVar("T")


class ProcessingUpdateThrottle:
    def __init__(
        self,
        callback: ProcessingUpdateCallback,
        *,
        interval_seconds: float = DEFAULT_PROCESSING_UPDATE_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._callback = callback
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._last_sent_at: float | None = None
        self._lock = asyncio.Lock()

    def reset(self) -> None:
        self._last_sent_at = None

    async def emit(self) -> None:
        now = self._clock()
        async with self._lock:
            if self._last_sent_at is not None and now - self._last_sent_at < self._interval_seconds:
                return
            self._last_sent_at = now
        await self._callback()


async def emit_processing_update(
    callback: ProcessingUpdateCallback | None,
    logger: logging.Logger,
) -> None:
    if callback is None:
        return
    try:
        await callback()
    except Exception:
        logger.debug("processing update callback failed", exc_info=True)


async def await_with_processing_updates(
    awaitable: Awaitable[T],
    *,
    callback: ProcessingUpdateCallback | None,
    logger: logging.Logger,
    interval_seconds: float = DEFAULT_PROCESSING_UPDATE_INTERVAL_SECONDS,
) -> T:
    await emit_processing_update(callback, logger)
    if callback is None:
        return await awaitable

    heartbeat_task = asyncio.create_task(_processing_update_heartbeat(callback, logger, interval_seconds))
    try:
        return await awaitable
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def _processing_update_heartbeat(
    callback: ProcessingUpdateCallback,
    logger: logging.Logger,
    interval_seconds: float,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        await emit_processing_update(callback, logger)
