import asyncio
import logging

import pytest

from ai_server.utils.processing import ProcessingUpdateThrottle, await_with_processing_updates


async def _done() -> str:
    return "done"


def test_processing_update_throttle_limits_nested_immediate_updates() -> None:
    async def run() -> list[float]:
        now = 0.0
        updates: list[float] = []

        async def emit_update() -> None:
            updates.append(now)

        throttle = ProcessingUpdateThrottle(emit_update, interval_seconds=10.0, clock=lambda: now)

        await await_with_processing_updates(
            _done(),
            callback=throttle.emit,
            logger=logging.getLogger(__name__),
            interval_seconds=10.0,
        )
        await await_with_processing_updates(
            _done(),
            callback=throttle.emit,
            logger=logging.getLogger(__name__),
            interval_seconds=10.0,
        )
        now = 9.9
        await throttle.emit()
        now = 10.0
        await throttle.emit()

        return updates

    assert asyncio.run(run()) == [0.0, 10.0]


def test_processing_update_throttle_reset_allows_new_request_immediate_update() -> None:
    async def run() -> list[float]:
        now = 0.0
        updates: list[float] = []

        async def emit_update() -> None:
            updates.append(now)

        throttle = ProcessingUpdateThrottle(emit_update, interval_seconds=10.0, clock=lambda: now)

        await throttle.emit()
        now = 3.0
        await throttle.emit()
        throttle.reset()
        await throttle.emit()

        return updates

    assert asyncio.run(run()) == [0.0, 3.0]


def test_processing_update_throttle_rejects_non_positive_interval() -> None:
    async def emit_update() -> None:
        pass

    with pytest.raises(ValueError, match="interval_seconds must be positive"):
        ProcessingUpdateThrottle(emit_update, interval_seconds=0)
