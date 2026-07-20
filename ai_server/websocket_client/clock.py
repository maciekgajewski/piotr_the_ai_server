from __future__ import annotations

import asyncio
import time


class SystemClock:
    """The production monotonic clock shared by one client engine and presenter."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep_until(self, deadline: float) -> None:
        await asyncio.sleep(max(0, deadline - self.monotonic()))
