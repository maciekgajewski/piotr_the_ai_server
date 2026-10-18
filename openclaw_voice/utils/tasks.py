from __future__ import annotations

import asyncio
from typing import TypeVar

from openclaw_voice.utils.interfaces import FatalTermination


T = TypeVar("T")


async def cancel_and_join_or_terminate(
    task: asyncio.Task[T], *, timeout: float,
    fatal_termination: FatalTermination, detail: str,
) -> bool:
    """Join canceled local work despite repeated caller cancellation.

    Return whether caller cancellation arrived during cleanup. A worker that
    cannot be joined within the bound requires process-fatal containment.
    """
    cleanup = asyncio.create_task(_cancel_and_join(task, timeout, fatal_termination, detail))
    caller_cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            caller_cancelled = True
    cleanup.result()
    return caller_cancelled


async def _cancel_and_join(
    task: asyncio.Task[T], timeout: float,
    fatal_termination: FatalTermination, detail: str,
) -> None:
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        await fatal_termination.terminate(detail)
        raise AssertionError("fatal termination returned")
    try:
        task.result()
    except asyncio.CancelledError:
        pass
