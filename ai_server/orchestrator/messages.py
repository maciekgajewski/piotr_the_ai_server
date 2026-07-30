from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


RoutePlanKind = Literal[
    "single_task",
    "multi_task",
    "followup",
    "clarification_answer",
    "chat",
]
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class RoutedTask(TypedDict):
    id: str
    domain: str
    request: str
    depends_on: list[str]


class RouteContextUpdates(TypedDict):
    salient_entities: list[str]
    active_domain: str | None


class RoutePlan(TypedDict):
    kind: RoutePlanKind
    confidence: float
    tasks: list[RoutedTask]
    context_updates: RouteContextUpdates
    needs_clarification: bool
    clarification_question: str | None


class DomainClarification(TypedDict):
    owner: Literal["dsa"]
    task: RoutedTask
    question: str
    continuation: NotRequired[JsonValue]


class RouteClarification(TypedDict):
    owner: Literal["router"]
    request: str
    question: str


PendingClarification = DomainClarification | RouteClarification
