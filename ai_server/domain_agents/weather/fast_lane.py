from __future__ import annotations

from typing import Any

from ai_server.domain_agents.interfaces import DomainTask
from ai_server.utils.text import ascii_fold, normalize_text


FAST_LANE_QUERIES = {
    "pogoda": {"tool": "get_weather_now"},
    "jaka pogoda": {"tool": "get_weather_now"},
    "jaka jest pogoda": {"tool": "get_weather_now"},
    "jaka temperatura": {"tool": "get_weather_now", "focus": "temperature"},
    "jaka jest temperatura": {"tool": "get_weather_now", "focus": "temperature"},
    "jaka dzis pogoda": {"tool": "get_weather_forecast", "horizon": "today", "granularity": "daily"},
    "jaka dzisiaj pogoda": {"tool": "get_weather_forecast", "horizon": "today", "granularity": "daily"},
    "jaka jutro pogoda": {"tool": "get_weather_forecast", "horizon": "tomorrow", "granularity": "daily"},
    "jaka pogoda jutro": {"tool": "get_weather_forecast", "horizon": "tomorrow", "granularity": "daily"},
    "jaka pogoda na weekend": {"tool": "get_weather_forecast", "horizon": "weekend", "granularity": "daily"},
    "jaka pogoda w weekend": {"tool": "get_weather_forecast", "horizon": "weekend", "granularity": "daily"},
    "jaka pogoda w ten weekend": {"tool": "get_weather_forecast", "horizon": "weekend", "granularity": "daily"},
}


def weather_task_from_utterance(user_input: str) -> DomainTask | None:
    command = fast_lane_command_for_query(user_input)
    if command is None:
        return None
    return {
        "id": "t1",
        "domain": "weather",
        "command": command,
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }


def fast_lane_command_from_task_command(command: dict[str, Any]) -> dict[str, str] | None:
    query = command.get("query")
    if not isinstance(query, str):
        return None
    if isinstance(command.get("location"), str) and command["location"].strip():
        return None
    return fast_lane_command_for_query(query)


def fast_lane_command_for_query(query: str) -> dict[str, str] | None:
    fast = FAST_LANE_QUERIES.get(_key(query))
    if fast is None:
        return None
    return {"query": query, **fast}


def _key(value: str) -> str:
    return ascii_fold(normalize_text(value)).strip(" ?.!").strip()
