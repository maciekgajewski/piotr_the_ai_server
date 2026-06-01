from __future__ import annotations

import copy

from ai_server.domain_agents.interfaces import DomainTask
from ai_server.domain_agents.weather.parser import weather_task_from_utterance
from ai_server.utils.text import normalize_text


KNOWN_UTTERANCE_TASKS: dict[str, DomainTask] = {
    normalize_text("Która godzina?"): {
        "id": "t1",
        "domain": "time",
        "command": {"query": "Która godzina?"},
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    },
    normalize_text("Pogoda?"): {
        "id": "t1",
        "domain": "weather",
        "command": {"tool": "get_weather_now", "query": "Pogoda?"},
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    },
    normalize_text("jaka jest pogoda"): {
        "id": "t1",
        "domain": "weather",
        "command": {"tool": "get_weather_now", "query": "jaka jest pogoda"},
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    },
    normalize_text("jaka dziś pogoda"): {
        "id": "t1",
        "domain": "weather",
        "command": {
            "tool": "get_weather_forecast",
            "query": "jaka dziś pogoda",
            "horizon": "today",
            "granularity": "daily",
        },
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    },
    normalize_text("jaka jutro pogoda"): {
        "id": "t1",
        "domain": "weather",
        "command": {
            "tool": "get_weather_forecast",
            "query": "jaka jutro pogoda",
            "horizon": "tomorrow",
            "granularity": "daily",
        },
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    },
    normalize_text("jaka pogoda na weekend"): {
        "id": "t1",
        "domain": "weather",
        "command": {
            "tool": "get_weather_forecast",
            "query": "jaka pogoda na weekend",
            "horizon": "weekend",
            "granularity": "daily",
        },
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    },
}


def known_utterance_task(user_input: str) -> DomainTask | None:
    task = KNOWN_UTTERANCE_TASKS.get(normalize_text(user_input))
    if task is not None:
        task = copy.deepcopy(task)
        command = task.get("command")
        if isinstance(command, dict) and "query" in command:
            command["query"] = user_input
        return task
    return weather_task_from_utterance(user_input)
