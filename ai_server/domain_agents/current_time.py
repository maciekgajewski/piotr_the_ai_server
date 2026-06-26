from __future__ import annotations

import datetime as dt
import json
import logging
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientSession, ClientTimeout

from ai_server.domain_agents.interfaces import DomainTask
from ai_server.interfaces import Conversation
from ai_server.utils.text import normalize_text


PLANNING_PROMPT = """
For time tasks:
- Include geo_location or timezone only when the user explicitly asks for a geographic place or timezone.
- For plain questions like "która godzina?", omit geo_location and timezone; the time agent already knows server_location and server_timezone.
- Never copy conversation.area into time.geo_location.

Command shape:
{"query": "original time question", "geo_location": "optional geographic place", "timezone": "optional"}
"""

DEFAULT_TIMEZONE = "UTC"
KNOWN_LOCATION_TIMEZONES = {
    "wroclaw": "Europe/Warsaw",
    "wrocław": "Europe/Warsaw",
    "wroclawiu": "Europe/Warsaw",
    "wrocławiu": "Europe/Warsaw",
    "polska": "Europe/Warsaw",
    "poland": "Europe/Warsaw",
    "jacksonville": "America/New_York",
    "jacksonville fl": "America/New_York",
    "jacksonville florida": "America/New_York",
    "floryda": "America/New_York",
    "florydzie": "America/New_York",
    "florida": "America/New_York",
}
WEEKDAYS = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")
MONTHS = (
    "stycznia",
    "lutego",
    "marca",
    "kwietnia",
    "maja",
    "czerwca",
    "lipca",
    "sierpnia",
    "września",
    "października",
    "listopada",
    "grudnia",
)
NUMBERS_MINUTES = {
    0: "zero",
    1: "jeden",
    2: "dwa",
    3: "trzy",
    4: "cztery",
    5: "pięć",
    6: "sześć",
    7: "siedem",
    8: "osiem",
    9: "dziewięć",
    10: "dziesięć",
    11: "jedenaście",
    12: "dwanaście",
    13: "trzynaście",
    14: "czternaście",
    15: "piętnaście",
    16: "szesnaście",
    17: "siedemnaście",
    18: "osiemnaście",
    19: "dziewiętnaście",
}
TENS_MINUTES = {
    20: "dwadzieścia",
    30: "trzydzieści",
    40: "czterdzieści",
    50: "pięćdziesiąt",
}
NUMBERS_HOURS = {
    0: "zerowa",
    1: "pierwsza",
    2: "druga",
    3: "trzecia",
    4: "czwarta",
    5: "piąta",
    6: "szósta",
    7: "siódma",
    8: "ósma",
    9: "dziewiąta",
    10: "dziesiąta",
    11: "jedenasta",
    12: "dwunasta",
    13: "trzynasta",
    14: "czternasta",
    15: "piętnasta",
    16: "szesnasta",
    17: "siedemnasta",
    18: "osiemnasta",
    19: "dziewiętnasta",
    20: "dwudziesta",
    21: "dwudziesta pierwsza",
    22: "dwudziesta druga",
    23: "dwudziesta trzecia",
}


class CurrentTimeDomainAgent:
    def __init__(
        self,
        *,
        timezone: str | None,
        location: str | None,
        cache_dir: Path,
        now_factory: Callable[[ZoneInfo], dt.datetime] | None = None,
        timezone_resolver: "TimezoneResolver | None" = None,
    ) -> None:
        self._timezone = timezone or DEFAULT_TIMEZONE
        self._location = location
        self._cache_dir = cache_dir
        self._now_factory = now_factory or (lambda zone: dt.datetime.now(zone))
        self._timezone_resolver = timezone_resolver or TimezoneResolver(
            cache_dir=cache_dir,
            configured_location=location,
            configured_timezone=self._timezone,
        )
        self._logger = logging.getLogger(f"{__name__}.CurrentTimeDomainAgent[{self._timezone}:{location or 'no-location'}]")

    def known_utterances(self) -> dict[str, DomainTask]:
        return {
            "Która godzina?": _known_task("time", {"query": "Która godzina?"}),
        }

    def planning_prompt(self) -> str:
        return PLANNING_PROMPT

    async def run_task(
        self,
        conversation: Conversation,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        del active_context
        command = task.get("command", {})
        command = command if isinstance(command, dict) else {}
        query = _string_or_empty(command.get("query"))
        target_timezone = _string_or_empty(command.get("timezone"))
        command_location = _string_or_empty(command.get("geo_location"))
        query_location = _extract_location(query)
        target_location = command_location if _command_geo_location(command_location, conversation) else query_location

        if target_timezone:
            timezone = target_timezone
            timezone_source = "command_timezone"
        elif target_location:
            try:
                timezone = await self._timezone_resolver.resolve(target_location)
                timezone_source = "location"
            except (RuntimeError, ZoneInfoNotFoundError):
                return _unknown_timezone_result(target_location)
        else:
            timezone = self._timezone
            timezone_source = "configured_timezone"
            target_location = self._location or ""

        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            return _unknown_timezone_result(target_location or target_timezone)

        now = self._now_factory(zone)
        response_kind = _response_kind(command, query)
        text = _format_reply(
            now,
            response_kind,
            location=target_location,
            concise=_is_short_local_time_question(
                query=query,
                response_kind=response_kind,
                timezone_source=timezone_source,
                target_timezone=target_timezone,
            ),
        )
        self._logger.info(
            "resolved current time query=%r location=%r timezone=%s source=%s response_kind=%s",
            query,
            target_location,
            timezone,
            timezone_source,
            response_kind,
        )
        result = {
            "status": "ok",
            "text": text,
            "needs_clarification": False,
            "clarification_question": None,
            "entities": [f"timezone.{timezone}"],
            "timezone": timezone,
            "location": target_location or None,
            "datetime": now.isoformat(),
            "date": now.date().isoformat(),
            "time": now.strftime("%H:%M"),
            "components": {
                "year": now.year,
                "month": now.month,
                "month_name": MONTHS[now.month - 1],
                "day": now.day,
                "day_of_week": WEEKDAYS[now.weekday()],
                "hour": now.hour,
                "minute": now.minute,
            },
        }
        if response_kind == "current_time":
            result["final_reply_mode"] = "verbatim"
        return result

    async def close(self) -> None:
        await self._timezone_resolver.close()


class TimezoneResolver:
    def __init__(
        self,
        *,
        cache_dir: Path,
        configured_location: str | None,
        configured_timezone: str | None,
        session: ClientSession | None = None,
    ) -> None:
        self._cache_path = cache_dir / "timezones" / "locations.json"
        self._configured_location = configured_location
        self._configured_timezone = configured_timezone
        self._session = session
        self._owns_session = session is None
        self._logger = logging.getLogger(f"{__name__}.TimezoneResolver")

    async def resolve(self, location: str) -> str:
        normalized_location = normalize_text(location)
        if not normalized_location:
            return self._configured_timezone or DEFAULT_TIMEZONE
        if self._configured_location and normalized_location == normalize_text(self._configured_location):
            return self._configured_timezone or DEFAULT_TIMEZONE
        if normalized_location in KNOWN_LOCATION_TIMEZONES:
            return KNOWN_LOCATION_TIMEZONES[normalized_location]

        cache = self._load_cache()
        cached_record = cache.get(normalized_location)
        if isinstance(cached_record, dict):
            cached_timezone = cached_record.get("timezone")
            if isinstance(cached_timezone, str) and cached_timezone:
                return cached_timezone

        timezone = await self._resolve_online(location)
        cache[normalized_location] = {
            "location": location,
            "timezone": timezone,
            "source": "online",
            "cached_at": dt.datetime.now(dt.UTC).isoformat(),
        }
        self._write_cache(cache)
        return timezone

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _resolve_online(self, location: str) -> str:
        geocode = await self._fetch_json(
            "https://nominatim.openstreetmap.org/search?"
            + urlencode({"q": location, "format": "json", "limit": "1"})
        )
        if not isinstance(geocode, list) or not geocode:
            raise ZoneInfoNotFoundError(f"cannot geocode location: {location}")
        first_result = geocode[0]
        if not isinstance(first_result, dict):
            raise ZoneInfoNotFoundError(f"invalid geocode response for location: {location}")
        latitude = first_result.get("lat")
        longitude = first_result.get("lon")
        if not isinstance(latitude, str) or not isinstance(longitude, str):
            raise ZoneInfoNotFoundError(f"geocode response missing coordinates for location: {location}")

        timezone_response = await self._fetch_json(
            "https://timeapi.io/api/TimeZone/coordinate?"
            + urlencode({"latitude": latitude, "longitude": longitude})
        )
        if not isinstance(timezone_response, dict):
            raise ZoneInfoNotFoundError(f"invalid timezone response for location: {location}")
        timezone = timezone_response.get("timeZone")
        if not isinstance(timezone, str) or not timezone:
            raise ZoneInfoNotFoundError(f"timezone response missing timeZone for location: {location}")
        ZoneInfo(timezone)
        return timezone

    async def _fetch_json(self, url: str) -> Any:
        session = self._session
        if session is None:
            timeout = ClientTimeout(total=10)
            session = ClientSession(
                timeout=timeout,
                headers={"User-Agent": "piotr-ai-server/1.0 local-timezone-resolver"},
            )
            self._session = session
        async with session.get(url) as response:
            if response.status >= 400:
                raise RuntimeError(f"timezone lookup failed with status {response.status}")
            return await response.json()

    def _load_cache(self) -> dict[str, Any]:
        if not self._cache_path.exists():
            return {}
        try:
            with self._cache_path.open("r", encoding="utf-8") as cache_file:
                cache = json.load(cache_file)
        except (OSError, json.JSONDecodeError):
            self._logger.warning("failed to load timezone cache path=%s", self._cache_path)
            return {}
        return cache if isinstance(cache, dict) else {}

    def _write_cache(self, cache: dict[str, Any]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cache_path.open("w", encoding="utf-8") as cache_file:
            json.dump(cache, cache_file, ensure_ascii=False, indent=2, sort_keys=True)


def _response_kind(command: dict[str, Any], query: str) -> str:
    intent = _string_or_empty(command.get("intent"))
    if intent in {"current_time", "current_date", "day_of_week", "month", "year"}:
        return intent
    normalized_query = normalize_text(query)
    if "dzien tygodnia" in normalized_query or "dzień tygodnia" in normalized_query:
        return "day_of_week"
    if "ktory rok" in normalized_query or "który rok" in normalized_query:
        return "year"
    if "miesiac" in normalized_query or "miesiąc" in normalized_query:
        return "month"
    if "data" in normalized_query or "jaki dzien" in normalized_query or "jaki dzień" in normalized_query:
        return "current_date"
    return "current_time"


def _format_reply(now: dt.datetime, response_kind: str, *, location: str, concise: bool = False) -> str:
    if concise and response_kind == "current_time":
        return _format_time_text(now)
    location_text = f" w {_display_location(location)}" if location else ""
    if response_kind == "day_of_week":
        return f"Dzisiaj{location_text} jest {WEEKDAYS[now.weekday()]}."
    if response_kind == "year":
        return f"Jest {now.year} rok."
    if response_kind == "month":
        return f"Jest {MONTHS[now.month - 1]}."
    if response_kind == "current_date":
        return f"Dzisiaj{location_text} jest {now.day} {MONTHS[now.month - 1]} {now.year}."
    return f"Teraz{location_text} jest {_format_time_text(now)}."


def _format_time_text(now: dt.datetime) -> str:
    return f"{NUMBERS_HOURS[now.hour]} {_format_minute_text(now.minute)}"


def _format_minute_text(minute: int) -> str:
    if minute < 10:
        return f"{NUMBERS_MINUTES[0]} {NUMBERS_MINUTES[minute]}"
    if minute < 20:
        return NUMBERS_MINUTES[minute]
    tens = minute - minute % 10
    rest = minute % 10
    if rest == 0:
        return TENS_MINUTES[tens]
    return f"{TENS_MINUTES[tens]} {NUMBERS_MINUTES[rest]}"


def _unknown_timezone_result(location: str) -> dict[str, Any]:
    return {
        "status": "needs_clarification",
        "text": f"Nie znam strefy czasowej dla: {location}.",
        "needs_clarification": True,
        "clarification_question": "Dla jakiego miasta lub strefy czasowej mam podać czas?",
        "entities": [],
    }


def _known_task(domain: str, command: dict[str, Any]) -> DomainTask:
    return {
        "id": "t1",
        "domain": domain,
        "command": command,
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }


def _display_location(location: str) -> str:
    normalized_location = normalize_text(location)
    if normalized_location in {"wroclaw", "wrocław", "wroclawiu", "wrocławiu"}:
        return "Wrocławiu"
    if normalized_location in {"floryda", "florydzie", "florida"}:
        return "Florydzie"
    if normalized_location in {"office", "biuro", "biurze"}:
        return "biurze"
    return location


def _extract_location(query: str) -> str:
    if not query:
        return ""
    normalized_query = query.strip()
    for pattern in (
        r"\b(?:w|we)\s+(.+)$",
        r"\bna\s+(.+)$",
    ):
        match = re.search(pattern, normalized_query, flags=re.IGNORECASE)
        if match is not None:
            location = match.group(1).strip(" ?.!")
            if location:
                return location
    return ""


def _command_geo_location(command_location: str, conversation: Conversation) -> bool:
    if not command_location:
        return False
    if conversation.area and normalize_text(command_location) == normalize_text(conversation.area):
        return False
    return True


def _is_short_local_time_question(
    *,
    query: str,
    response_kind: str,
    timezone_source: str,
    target_timezone: str,
) -> bool:
    if response_kind != "current_time" or target_timezone or timezone_source != "configured_timezone":
        return False
    normalized_query = normalize_text(query)
    return normalized_query in {
        "ktora godzina",
        "która godzina",
        "ktora jest godzina",
        "która jest godzina",
        "jaka godzina",
        "jaki czas",
    }


def _string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""
