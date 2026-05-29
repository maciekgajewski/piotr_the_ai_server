from __future__ import annotations

from typing import Any, Protocol

from ai_server.interfaces import Conversation


DomainTask = dict[str, Any]


class DomainAgent(Protocol):
    async def run_task(
        self,
        conversation: Conversation,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
