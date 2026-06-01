from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ai_server.utils.text import normalize_text


DEFAULT_GRANULARITY = "daily"
VALID_TOOLS = {"get_weather_now", "get_weather_forecast"}
VALID_FOCUSES = {"temperature"}
WEEKDAY_ALIASES = {
    "poniedzialek": "monday",
    "poniedzialku": "monday",
    "monday": "monday",
    "wtorek": "tuesday",
    "wtorku": "tuesday",
    "tuesday": "tuesday",
    "sroda": "wednesday",
    "srode": "wednesday",
    "srodę": "wednesday",
    "wednesday": "wednesday",
    "czwartek": "thursday",
    "czwartku": "thursday",
    "thursday": "thursday",
    "piatek": "friday",
    "piątek": "friday",
    "piatku": "friday",
    "piątku": "friday",
    "friday": "friday",
    "sobota": "saturday",
    "sobote": "saturday",
    "sobotę": "saturday",
    "saturday": "saturday",
    "niedziela": "sunday",
    "niedziele": "sunday",
    "niedzielę": "sunday",
    "sunday": "sunday",
}
SIMPLE_CURRENT_QUERIES = {
    "pogoda",
    "jaka jest pogoda",
    "jaka pogoda",
    "jaka jest temperatura",
    "jaka temperatura",
    "ile stopni",
}


@dataclass(frozen=True)
class ParsedWeatherCommand:
    tool: str
    query: str
    location: str
    focus: str | None
    horizon: str | None
    granularity: str
    simple: bool


def parse_weather_command(command: dict[str, Any], *, default_location: str | None) -> ParsedWeatherCommand:
    query = _string_or_empty(command.get("query"))
    normalized_query = normalize_text(query)
    ascii_query = ascii_fold(normalized_query)
    location = _string_or_empty(command.get("location")) or _extract_location(query) or (default_location or "")
    focus = _focus_from_command(command, ascii_query) or _focus_from_query(ascii_query)
    horizon = normalize_horizon(_string_or_empty(command.get("horizon"))) or _horizon_from_query(ascii_query)
    granularity = normalize_granularity(_string_or_empty(command.get("granularity"))) or _granularity_from_query(ascii_query)
    tool = normalize_tool(_string_or_empty(command.get("tool")))
    if tool is None:
        tool = "get_weather_forecast" if horizon is not None else "get_weather_now"
    if tool == "get_weather_now" and _query_needs_forecast(ascii_query, horizon):
        tool = "get_weather_forecast"
        granularity = "hourly" if _query_needs_hourly(ascii_query) else granularity
    if tool == "get_weather_forecast" and horizon is None:
        horizon = "today"

    return ParsedWeatherCommand(
        tool=tool,
        query=query,
        location=location,
        focus=focus,
        horizon=horizon,
        granularity=granularity,
        simple=is_simple_weather_query(query, tool=tool, horizon=horizon, focus=focus),
    )


def weather_task_from_utterance(user_input: str) -> dict[str, Any] | None:
    normalized_query = normalize_text(user_input)
    ascii_query = ascii_fold(normalized_query)
    if _is_complex_query(ascii_query) or not _looks_like_weather_query(ascii_query):
        return None

    focus = _focus_from_query(ascii_query)
    horizon = _horizon_from_query(ascii_query)
    tool = "get_weather_forecast" if horizon is not None else "get_weather_now"
    command: dict[str, Any] = {
        "tool": tool,
        "query": user_input,
    }
    location = _extract_location(user_input)
    if location:
        command["location"] = location
    if focus is not None:
        command["focus"] = focus
    if horizon is not None:
        command["horizon"] = horizon
        command["granularity"] = _granularity_from_query(ascii_query)

    return {
        "id": "t1",
        "domain": "weather",
        "command": command,
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }


def normalize_tool(value: str) -> str | None:
    if value in VALID_TOOLS:
        return value
    if value in {"now", "current", "current_weather"}:
        return "get_weather_now"
    if value in {"forecast", "weather_forecast"}:
        return "get_weather_forecast"
    return None


def normalize_focus(value: str) -> str | None:
    normalized = ascii_fold(normalize_text(value))
    if normalized in VALID_FOCUSES:
        return normalized
    if normalized in {"temperatura", "temperature", "temp", "stopnie"}:
        return "temperature"
    return None


def normalize_granularity(value: str) -> str:
    normalized = ascii_fold(normalize_text(value))
    if normalized in {"hourly", "godzinowa", "godzinowo", "godzinna"}:
        return "hourly"
    if normalized in {"daily", "dzienna", "dziennie", "dzień", "dzien"}:
        return "daily"
    return DEFAULT_GRANULARITY


def normalize_horizon(value: str) -> str | None:
    normalized = ascii_fold(normalize_text(value))
    if not normalized:
        return None
    normalized = normalized.replace("next weekeend", "next weekend")
    if normalized in {"today", "dzis", "dzisiaj"}:
        return "today"
    if normalized in {"tomorrow", "jutro"}:
        return "tomorrow"
    if normalized in {"weekend", "wekend", "weekeend"}:
        return "weekend"
    if normalized in {"next weekend", "next weekeend", "nastepny weekend", "przyszly weekend", "kolejny weekend"}:
        return "next_weekend"
    return WEEKDAY_ALIASES.get(normalized)


def is_simple_weather_query(query: str, *, tool: str, horizon: str | None, focus: str | None) -> bool:
    normalized_query = normalize_text(query)
    ascii_query = ascii_fold(normalized_query)
    if _is_complex_query(ascii_query):
        return False
    if tool == "get_weather_now":
        return ascii_query in SIMPLE_CURRENT_QUERIES or focus == "temperature"
    if tool == "get_weather_forecast" and horizon in {"today", "weekend", "next_weekend"}:
        return any(word in ascii_query for word in ("pogoda", "temperatura", "weekend", "dzis", "dzisiaj"))
    return False


def ascii_fold(value: str) -> str:
    value = value.translate(str.maketrans({"ł": "l", "Ł": "L"}))
    folded = unicodedata.normalize("NFKD", value)
    return "".join(character for character in folded if not unicodedata.combining(character))


def _looks_like_weather_query(ascii_query: str) -> bool:
    if not ascii_query:
        return False
    if "pogoda" in ascii_query or "temperatura" in ascii_query or "stopni" in ascii_query:
        return True
    return False


def _focus_from_query(ascii_query: str) -> str | None:
    if "temperatura" in ascii_query or " stopni" in f" {ascii_query}" or ascii_query.startswith("ile stopni"):
        return "temperature"
    return None


def _focus_from_command(command: dict[str, Any], ascii_query: str) -> str | None:
    focus = normalize_focus(_string_or_empty(command.get("focus")))
    if focus is None:
        return None
    if ascii_query and _focus_from_query(ascii_query) != focus:
        return None
    return focus


def _horizon_from_query(ascii_query: str) -> str | None:
    if "nastepny weekend" in ascii_query or "przyszly weekend" in ascii_query or "next weekend" in ascii_query:
        return "next_weekend"
    if re.search(r"\bwe+ke*n+d\b", ascii_query) or "weekend" in ascii_query:
        return "weekend"
    if "dzisiaj" in ascii_query or "dzis" in ascii_query:
        return "today"
    if "jutro" in ascii_query:
        return "tomorrow"
    for alias, weekday in WEEKDAY_ALIASES.items():
        if re.search(rf"\b{re.escape(ascii_fold(alias))}\b", ascii_query):
            return weekday
    return None


def _granularity_from_query(ascii_query: str) -> str:
    if _query_needs_hourly(ascii_query):
        return "hourly"
    if "godzin" in ascii_query or "co godzine" in ascii_query or "co godzina" in ascii_query:
        return "hourly"
    return DEFAULT_GRANULARITY


def _query_needs_forecast(ascii_query: str, horizon: str | None) -> bool:
    if horizon is not None:
        return True
    return any(marker in ascii_query for marker in ("bedzie", "wieczor", "wieczorem", "rano", "po poludniu", "popoludniu", "noc"))


def _query_needs_hourly(ascii_query: str) -> bool:
    return any(marker in ascii_query for marker in ("wieczor", "wieczorem", "rano", "po poludniu", "popoludniu", "noc"))


def _extract_location(query: str) -> str:
    if not query:
        return ""
    for pattern in (
        r"\b(?:w|we)\s+(.+?)(?:\s+(?:dzisiaj|dziś|dzis|jutro|na\s+weekend|w\s+weekend|we\s+weekend))?[?.!]*$",
        r"\bdla\s+(.+?)[?.!]*$",
    ):
        match = re.search(pattern, query.strip(), flags=re.IGNORECASE)
        if match is None:
            continue
        location = match.group(1).strip(" ?.!")
        if location and ascii_fold(normalize_text(location)) not in {"weekend", "wekend", "weekeend"}:
            return location
    return ""


def _is_complex_query(ascii_query: str) -> bool:
    return any(
        marker in ascii_query
        for marker in (
            "czy ",
            "paras",
            "kiedy",
            "najlepiej",
            "porownaj",
            "porownanie",
            "spacer",
            "biegan",
            "rower",
            "ubrac",
            "załozyc",
            "zalozyc",
        )
    )


def _string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""
