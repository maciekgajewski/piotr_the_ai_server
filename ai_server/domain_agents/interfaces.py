from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ai_server.conversations.agent_context import AgentExecutionContext


DomainTask = dict[str, Any]


@dataclass(frozen=True)
class QueryCapability:
    name: str
    description: str
    intents: tuple[str, ...] = ()
    command_template: Mapping[str, Any] = field(default_factory=dict)
    examples: tuple[Mapping[str, Any], ...] = ()


class DomainAgent(Protocol):
    def planning_prompt(self) -> str:
        raise NotImplementedError

    def query_capabilities(self) -> Mapping[str, QueryCapability]:
        raise NotImplementedError

    def query_capabilities_prompt(self) -> str:
        raise NotImplementedError

    def known_utterances(self) -> Mapping[str, DomainTask]:
        raise NotImplementedError

    async def run_task(
        self,
        conversation: AgentExecutionContext,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
