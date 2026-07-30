from __future__ import annotations

from collections.abc import Collection
from typing import Any, Protocol

from ai_server.conversations.agent_context import AgentExecutionContext
from ai_server.orchestrator.messages import JsonValue, RoutedTask


DomainTask = RoutedTask


class DomainAgent(Protocol):
    def routing_summary(self) -> str:
        raise NotImplementedError

    def known_utterances(self) -> Collection[str]:
        raise NotImplementedError

    async def run_task(
        self,
        conversation: AgentExecutionContext,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def resume_clarification(
        self,
        conversation: AgentExecutionContext,
        task: RoutedTask,
        question: str,
        answer: str,
        active_context: dict[str, Any],
        continuation: JsonValue,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
