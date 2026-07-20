from __future__ import annotations

import asyncio
import sys
import time

from ai_server.websocket_client.messages import InputKind, InputResult, InputResultKind


class BatchPresenter:
    is_batch = True

    def __init__(self, messages: tuple[str, ...]) -> None:
        self._messages = list(messages)
        self._blocked_input: asyncio.Future | None = None

    async def present_input(self, kind: InputKind) -> float:
        return time.monotonic()

    async def read_input(self, kind: InputKind) -> InputResult:
        if self._messages:
            return InputResult(InputResultKind.TEXT, self._messages.pop(0), time.monotonic())
        if kind is InputKind.START:
            return InputResult(InputResultKind.EOF, None, time.monotonic())
        self._blocked_input = asyncio.get_running_loop().create_future()
        await self._blocked_input
        raise AssertionError("blocked batch input unexpectedly completed")

    async def cancel_input(self) -> None:
        if self._blocked_input is not None and not self._blocked_input.done():
            self._blocked_input.cancel()
        self._blocked_input = None

    def system(self, text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    def assistant_started(self) -> None:
        return None

    def assistant_text(self, text: str) -> None:
        print(text, end="", flush=True)

    def assistant_finished(self, aborted_reason: str | None = None) -> None:
        print(flush=True)
        if aborted_reason is not None:
            self.system(f"Assistant message aborted: {aborted_reason}.")
