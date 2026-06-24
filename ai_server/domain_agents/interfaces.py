from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ai_server.interfaces import Conversation


DomainTask = dict[str, Any]


class DomainAgent(Protocol):
    def known_utterances(self) -> Mapping[str, DomainTask]:
        raise NotImplementedError

    async def run_task(
        self,
        conversation: Conversation,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
