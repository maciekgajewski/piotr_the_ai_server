from __future__ import annotations

from typing import NoReturn, Protocol


class FatalTermination(Protocol):
    async def terminate(self, detail: str) -> NoReturn: ...
