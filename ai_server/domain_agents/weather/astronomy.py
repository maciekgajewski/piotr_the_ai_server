from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout

from ai_server.utils import JsonFileStore
from ai_server.utils.polish_numbers import polish_cardinal
from ai_server.utils.text import ascii_fold, normalize_text


ASTRONOMY_URL = "https://api.ipgeolocation.io/v3/astronomy"
ASTRONOMY_STORE_KEY = "weather.astronomy"
ASTRONOMY_REFRESH_SECONDS = 12 * 60 * 60
TODAY_RECORD_KEY = "today"
JUNE_SOLSTICE_RECORD_KEY = "june_solstice"
DECEMBER_SOLSTICE_RECORD_KEY = "december_solstice"
REQUIRED_RECORD_KEYS = (TODAY_RECORD_KEY, JUNE_SOLSTICE_RECORD_KEY, DECEMBER_SOLSTICE_RECORD_KEY)


@dataclass(frozen=True)
class AstronomyRecord:
    date: str
    sunrise: str
    sunset: str
    moonrise: str
    moonset: str
    moon_phase: str
    day_length: str


@dataclass(frozen=True)
class AstronomySnapshot:
    location: str
    last_pull_date: str
    records: dict[str, AstronomyRecord]


class IPGeolocationAstronomyClient:
    def __init__(
        self,
        *,
        api_key: str,
        session: ClientSession | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("IPGeolocation astronomy API key must be a non-empty string")
        self._api_key = api_key
        self._session = session
        self._owns_session = session is None
        self._logger = logging.getLogger(f"{__name__}.IPGeolocationAstronomyClient")

    async def fetch_snapshot(self, *, location: str, now_utc: dt.datetime) -> AstronomySnapshot:
        if not location:
            raise ValueError("astronomy location must be a non-empty string")
        now_utc = _as_utc(now_utc)
        today = now_utc.date()
        records = {
            TODAY_RECORD_KEY: await self.fetch_record(location=location, date=today),
            JUNE_SOLSTICE_RECORD_KEY: await self.fetch_record(location=location, date=dt.date(today.year, 6, 21)),
            DECEMBER_SOLSTICE_RECORD_KEY: await self.fetch_record(location=location, date=dt.date(today.year, 12, 21)),
        }
        return AstronomySnapshot(location=location, last_pull_date=_utc_iso(now_utc), records=records)

    async def fetch_record(self, *, location: str, date: dt.date) -> AstronomyRecord:
        params = {
            "apiKey": self._api_key,
            "location": location,
            "date": date.isoformat(),
        }
        response = await self._fetch_json(ASTRONOMY_URL + "?" + urlencode(params))
        if not isinstance(response, dict):
            raise RuntimeError("IPGeolocation astronomy response must be a JSON object")
        astronomy = response.get("astronomy")
        if not isinstance(astronomy, dict):
            raise RuntimeError("IPGeolocation astronomy response missing astronomy object")
        record = _record_from_astronomy(astronomy)
        self._logger.debug("fetched astronomy record location=%r date=%s", location, record.date)
        return record

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _fetch_json(self, url: str) -> Any:
        session = self._session
        if session is None:
            timeout = ClientTimeout(total=10)
            session = ClientSession(
                timeout=timeout,
                headers={"User-Agent": "piotr-ai-server/1.0 weather-astronomy"},
            )
            self._session = session
        async with session.get(url) as response:
            if response.status >= 400:
                raise RuntimeError(f"IPGeolocation astronomy request failed with status {response.status}")
            return await response.json()


class WeatherAstronomyStore:
    def __init__(self, store: JsonFileStore, *, key: str = ASTRONOMY_STORE_KEY) -> None:
        self._store = store
        self._key = key

    def load(self) -> AstronomySnapshot | None:
        data = self._store.load(self._key)
        if not data:
            return None
        return _snapshot_from_json(data)

    def store_snapshot(self, snapshot: AstronomySnapshot) -> None:
        self._store.store(self._key, _snapshot_to_json(snapshot))

    def load_for_location(self, location: str) -> AstronomySnapshot | None:
        snapshot = self.load()
        if snapshot is None or snapshot.location != location:
            return None
        return snapshot

    def is_fresh(self, snapshot: AstronomySnapshot, *, location: str, now_utc: dt.datetime, max_age_seconds: float) -> bool:
        if snapshot.location != location:
            return False
        pulled_at = _parse_utc(snapshot.last_pull_date)
        if pulled_at is None:
            return False
        age_seconds = (_as_utc(now_utc) - pulled_at).total_seconds()
        return 0 <= age_seconds <= max_age_seconds


class WeatherAstronomyRefresher:
    def __init__(
        self,
        *,
        location: str | None,
        store: WeatherAstronomyStore,
        client: IPGeolocationAstronomyClient,
        refresh_interval_seconds: float = ASTRONOMY_REFRESH_SECONDS,
        now_factory: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._location = location
        self._store = store
        self._client = client
        self._refresh_interval_seconds = refresh_interval_seconds
        self._now_factory = now_factory or (lambda: dt.datetime.now(dt.UTC))
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._logger = logging.getLogger(f"{__name__}.WeatherAstronomyRefresher[{location or 'no-location'}]")

    async def start(self) -> None:
        self._closed = False
        if not self._location:
            self._logger.warning("weather astronomy refresher disabled because server location is not configured")
            return
        if self._task is None or self._task.done():
            self._logger.info(
                "starting weather astronomy refresher location=%r refresh_interval_seconds=%s",
                self._location,
                self._refresh_interval_seconds,
            )
            await self.ensure_fresh()
            self._task = asyncio.create_task(self._refresh_loop())
            self._logger.info("weather astronomy refresher started")

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._logger.info("stopping weather astronomy refresher")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        await self._client.close()
        self._logger.info("weather astronomy refresher stopped")

    async def ensure_fresh(self) -> AstronomySnapshot | None:
        if not self._location:
            return None
        now = _as_utc(self._now_factory())
        snapshot = self._store.load_for_location(self._location)
        if snapshot is not None and self._store.is_fresh(
            snapshot,
            location=self._location,
            now_utc=now,
            max_age_seconds=self._refresh_interval_seconds,
        ):
            self._logger.debug("weather astronomy snapshot is fresh location=%r last_pull_date=%s", self._location, snapshot.last_pull_date)
            return snapshot
        try:
            refreshed = await self._client.fetch_snapshot(location=self._location, now_utc=now)
        except Exception:
            if snapshot is not None:
                self._logger.warning(
                    "weather astronomy refresh failed; using stored snapshot location=%r last_pull_date=%s",
                    self._location,
                    snapshot.last_pull_date,
                    exc_info=True,
                )
                return snapshot
            self._logger.warning("weather astronomy refresh failed and no stored snapshot is available location=%r", self._location, exc_info=True)
            return None
        self._store.store_snapshot(refreshed)
        self._logger.info("weather astronomy snapshot refreshed location=%r last_pull_date=%s", self._location, refreshed.last_pull_date)
        return refreshed

    def latest_snapshot(self) -> AstronomySnapshot | None:
        if not self._location:
            return None
        return self._store.load_for_location(self._location)

    async def _refresh_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self._refresh_interval_seconds)
            try:
                await self.ensure_fresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("weather astronomy periodic refresh failed")


def astronomy_to_json(snapshot: AstronomySnapshot) -> dict[str, Any]:
    return _snapshot_to_json(snapshot)


def astronomy_facts_to_json(snapshot: AstronomySnapshot) -> dict[str, Any]:
    today = snapshot.records.get(TODAY_RECORD_KEY)
    data: dict[str, Any] = {
        "location": snapshot.location,
        "last_pull_date": snapshot.last_pull_date,
    }
    if today is not None:
        data["today"] = asdict(today)
    comparison = day_length_comparison_to_json(snapshot)
    if comparison is not None:
        data["day_length_comparison"] = comparison
    return data


def format_astronomy(snapshot: AstronomySnapshot) -> str:
    today = snapshot.records.get(TODAY_RECORD_KEY)
    if today is None:
        return f"Nie mam danych astronomicznych dla lokalizacji {snapshot.location}."
    parts = [
        f"Dzisiaj dla lokalizacji {snapshot.location} słońce wschodzi o {_time_phrase(today.sunrise)} i zachodzi o {_time_phrase(today.sunset)}.",
        f"Księżyc wschodzi o {_time_phrase(today.moonrise)} i zachodzi o {_time_phrase(today.moonset)}; faza to {_moon_phase_phrase(today.moon_phase)}.",
    ]
    comparison = _day_length_comparison(snapshot)
    if comparison:
        parts.append(comparison)
    return " ".join(parts)


def format_astronomy_for_query(snapshot: AstronomySnapshot, query: str) -> str:
    today = snapshot.records.get(TODAY_RECORD_KEY)
    if today is None:
        return f"Nie mam danych astronomicznych dla lokalizacji {snapshot.location}."
    focus = astronomy_focus_from_query(query)
    location_phrase = _location_phrase(snapshot.location)
    if focus == "sunrise":
        return f"Dzisiaj {location_phrase} wschód słońca jest o {_time_phrase(today.sunrise)}."
    if focus == "sunset":
        return f"Dzisiaj {location_phrase} zachód słońca jest o {_time_phrase(today.sunset)}."
    if focus == "moonrise":
        return f"Dzisiaj {location_phrase} księżyc wschodzi o {_time_phrase(today.moonrise)}."
    if focus == "moonset":
        return f"Dzisiaj {location_phrase} księżyc zachodzi o {_time_phrase(today.moonset)}."
    if focus == "moon_phase":
        return f"Dzisiaj {location_phrase} faza księżyca to {_moon_phase_phrase(today.moon_phase)}."
    if focus == "day_length":
        comparison = _day_length_comparison(snapshot)
        if comparison:
            return f"Dzisiaj {location_phrase} {comparison[0].lower()}{comparison[1:]}"
        return f"Dzisiaj {location_phrase} dzień trwa {_duration_phrase(today.day_length)}."
    return format_astronomy(snapshot)


def astronomy_focus_from_query(query: str) -> str | None:
    normalized = _normalize_query(query)
    if "wschod slonca" in normalized or ("slonce" in normalized and "wschod" in normalized):
        return "sunrise"
    if "zachod slonca" in normalized or ("slonce" in normalized and "zachod" in normalized):
        return "sunset"
    if "wschod ksiezyca" in normalized or ("ksiezyc" in normalized and "wschod" in normalized):
        return "moonrise"
    if "zachod ksiezyca" in normalized or ("ksiezyc" in normalized and "zachod" in normalized):
        return "moonset"
    if "faza ksiezyca" in normalized or ("ksiezyc" in normalized and "faza" in normalized):
        return "moon_phase"
    if "dlugosc dnia" in normalized or "dzien trwa" in normalized or "najdluzsz" in normalized or "najkrotsz" in normalized:
        return "day_length"
    if "slonce" in normalized or "ksiezyc" in normalized:
        return "all"
    return None


def day_length_comparison_to_json(snapshot: AstronomySnapshot) -> dict[str, Any] | None:
    today = snapshot.records.get(TODAY_RECORD_KEY)
    june = snapshot.records.get(JUNE_SOLSTICE_RECORD_KEY)
    december = snapshot.records.get(DECEMBER_SOLSTICE_RECORD_KEY)
    if today is None or june is None or december is None:
        return None
    today_minutes = _duration_minutes(today.day_length)
    june_minutes = _duration_minutes(june.day_length)
    december_minutes = _duration_minutes(december.day_length)
    if today_minutes is None or june_minutes is None or december_minutes is None:
        return None
    longest = max(june_minutes, december_minutes)
    shortest = min(june_minutes, december_minutes)
    return {
        "today_minutes": today_minutes,
        "shorter_than_longest_minutes": max(0, longest - today_minutes),
        "longer_than_shortest_minutes": max(0, today_minutes - shortest),
    }


def _snapshot_to_json(snapshot: AstronomySnapshot) -> dict[str, Any]:
    return {
        "location": snapshot.location,
        "last_pull_date": snapshot.last_pull_date,
        "records": {key: asdict(record) for key, record in snapshot.records.items()},
    }


def _snapshot_from_json(data: dict[str, Any]) -> AstronomySnapshot | None:
    location = data.get("location")
    last_pull_date = data.get("last_pull_date")
    records_data = data.get("records")
    if not isinstance(location, str) or not location:
        return None
    if not isinstance(last_pull_date, str) or not last_pull_date:
        return None
    if not isinstance(records_data, dict):
        return None
    records: dict[str, AstronomyRecord] = {}
    for key in REQUIRED_RECORD_KEYS:
        raw = records_data.get(key)
        if not isinstance(raw, dict):
            return None
        record = _record_from_json(raw)
        if record is None:
            return None
        records[key] = record
    return AstronomySnapshot(location=location, last_pull_date=last_pull_date, records=records)


def _record_from_astronomy(astronomy: dict[str, Any]) -> AstronomyRecord:
    return AstronomyRecord(
        date=_required_string(astronomy, "date"),
        sunrise=_required_string(astronomy, "sunrise"),
        sunset=_required_string(astronomy, "sunset"),
        moonrise=_required_string(astronomy, "moonrise"),
        moonset=_required_string(astronomy, "moonset"),
        moon_phase=_required_string(astronomy, "moon_phase"),
        day_length=_required_string(astronomy, "day_length"),
    )


def _record_from_json(data: dict[str, Any]) -> AstronomyRecord | None:
    try:
        return AstronomyRecord(
            date=_required_string(data, "date"),
            sunrise=_required_string(data, "sunrise"),
            sunset=_required_string(data, "sunset"),
            moonrise=_required_string(data, "moonrise"),
            moonset=_required_string(data, "moonset"),
            moon_phase=_required_string(data, "moon_phase"),
            day_length=_required_string(data, "day_length"),
        )
    except ValueError:
        return None


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"astronomy field {key!r} must be a non-empty string")
    return value


def _day_length_comparison(snapshot: AstronomySnapshot) -> str | None:
    comparison = day_length_comparison_to_json(snapshot)
    if comparison is None:
        return None
    return (
        f"Dzień trwa {_duration_phrase_from_minutes(comparison['today_minutes'])}, "
        f"jest o {_duration_phrase_from_minutes(comparison['shorter_than_longest_minutes'])} krótszy niż najdłuższy "
        f"i o {_duration_phrase_from_minutes(comparison['longer_than_shortest_minutes'])} dłuższy niż najkrótszy dzień roku."
    )


def _duration_minutes(value: str) -> int | None:
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
    except ValueError:
        return None
    if hours < 0 or not 0 <= minutes < 60:
        return None
    return hours * 60 + minutes


def _duration_phrase_from_minutes(total_minutes: int) -> str:
    hours = total_minutes // 60
    minutes = total_minutes % 60
    if hours == 0:
        return f"{polish_cardinal(minutes)} {_minute_word(minutes)}"
    if minutes == 0:
        return f"{polish_cardinal(hours)} {_hour_word(hours)}"
    return f"{polish_cardinal(hours)} {_hour_word(hours)} i {polish_cardinal(minutes)} {_minute_word(minutes)}"


def _time_phrase(value: str) -> str:
    if value == "-:-":
        return "brak"
    parts = value.split(":")
    if len(parts) < 2:
        return value
    try:
        hour = int(parts[0][-2:])
        minute = int(parts[1][:2])
    except ValueError:
        return value
    if minute == 0:
        return polish_cardinal(hour)
    return f"{polish_cardinal(hour)} {polish_cardinal(minute)}"


def _duration_phrase(value: str) -> str:
    minutes = _duration_minutes(value)
    if minutes is None:
        return value
    return _duration_phrase_from_minutes(minutes)


def _moon_phase_phrase(value: str) -> str:
    return {
        "NEW_MOON": "nów",
        "WAXING_CRESCENT": "przybywający sierp",
        "FIRST_QUARTER": "pierwsza kwadra",
        "WAXING_GIBBOUS": "przybywający garb",
        "FULL_MOON": "pełnia",
        "WANING_GIBBOUS": "ubywający garb",
        "LAST_QUARTER": "ostatnia kwadra",
        "WANING_CRESCENT": "ubywający sierp",
    }.get(value, value.lower().replace("_", " "))


def _hour_word(value: int) -> str:
    if value == 1:
        return "godzina"
    if 2 <= value % 10 <= 4 and value % 100 not in {12, 13, 14}:
        return "godziny"
    return "godzin"


def _minute_word(value: int) -> str:
    if value == 1:
        return "minuta"
    if 2 <= value % 10 <= 4 and value % 100 not in {12, 13, 14}:
        return "minuty"
    return "minut"


def _normalize_query(query: str) -> str:
    return ascii_fold(normalize_text(query))


def _location_phrase(location: str) -> str:
    if location == "Wrocław":
        return "we Wrocławiu"
    return f"dla lokalizacji {location}"


def _utc_iso(value: dt.datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _as_utc(parsed)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)
