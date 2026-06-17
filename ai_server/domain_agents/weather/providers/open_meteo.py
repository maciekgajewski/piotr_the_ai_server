from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiohttp import ClientSession, ClientTimeout

from ai_server.domain_agents.weather.formatting import weather_code_description
from ai_server.domain_agents.weather.interfaces import (
    CurrentWeather,
    DailyForecast,
    HourlyForecast,
    WeatherForecast,
    WeatherForecastRequest,
    WeatherNowRequest,
)
from ai_server.utils.text import ascii_fold, normalize_text


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
    "rain,showers,snowfall,weather_code,cloud_cover,wind_speed_10m,wind_gusts_10m"
)
HOURLY_FIELDS = (
    "temperature_2m,apparent_temperature,precipitation_probability,precipitation,"
    "weather_code,wind_speed_10m,wind_gusts_10m"
)
DAILY_FIELDS = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
    "precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max"
)


class OpenMeteoWeatherProvider:
    name = "open_meteo"

    def __init__(
        self,
        *,
        cache_dir: Path,
        session: ClientSession | None = None,
        now_factory: Callable[[ZoneInfo], dt.datetime] | None = None,
    ) -> None:
        self._cache_path = cache_dir / "weather" / "open_meteo_geocoding.json"
        self._session = session
        self._owns_session = session is None
        self._now_factory = now_factory or (lambda zone: dt.datetime.now(zone))
        self._logger = logging.getLogger(f"{__name__}.OpenMeteoWeatherProvider")

    async def get_weather_now(self, request: WeatherNowRequest) -> CurrentWeather | None:
        location = await self._resolve_location(request.location)
        if location is None:
            return None
        response = await self._forecast_json(location, include_current=True, forecast_days=1)
        current = response.get("current") if isinstance(response, dict) else None
        if not isinstance(current, dict):
            return None
        timezone = _zone_from_name(_string_or_none(response.get("timezone")) or location.timezone)
        observed_at = _datetime_from_local(_string_or_none(current.get("time")), timezone) or self._now_factory(timezone)
        return CurrentWeather(
            location=location.name,
            provider=self.name,
            observed_at=observed_at,
            station_name=None,
            temperature_c=_float_or_none(current.get("temperature_2m")),
            humidity_percent=_float_or_none(current.get("relative_humidity_2m")),
            pressure_hpa=None,
            wind_speed_kmh=_float_or_none(current.get("wind_speed_10m")),
            wind_direction_deg=None,
            precipitation_mm=_float_or_none(current.get("precipitation")),
            weather_code=_int_or_none(current.get("weather_code")),
            weather_description=weather_code_description(_int_or_none(current.get("weather_code"))),
            apparent_temperature_c=_float_or_none(current.get("apparent_temperature")),
            cloud_cover_percent=_float_or_none(current.get("cloud_cover")),
        )

    async def get_weather_forecast(self, request: WeatherForecastRequest) -> WeatherForecast | None:
        location = await self._resolve_location(request.location)
        if location is None:
            return None
        timezone = _zone_from_name(location.timezone)
        target_dates = _target_dates(request.horizon, self._now_factory(timezone).date())
        response = await self._forecast_json(location, include_current=False, forecast_days=16)
        if not isinstance(response, dict):
            return None
        timezone_name = _string_or_none(response.get("timezone")) or location.timezone
        timezone = _zone_from_name(timezone_name)
        daily = _daily_forecasts(response.get("daily"), target_dates)
        hourly = ()
        if request.granularity == "hourly":
            hourly = _hourly_forecasts(response.get("hourly"), target_dates, timezone)
        return WeatherForecast(
            location=location.name,
            provider=self.name,
            timezone=timezone_name,
            horizon=request.horizon,
            granularity=request.granularity,
            daily=daily,
            hourly=hourly,
        )

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _resolve_location(self, location: str) -> "_OpenMeteoLocation | None":
        normalized_location = ascii_fold(normalize_text(location))
        if not normalized_location:
            return None
        cached = self._load_cache().get(normalized_location)
        if isinstance(cached, dict):
            parsed = _location_from_cache(cached)
            if parsed is not None:
                return parsed

        response = await self._fetch_json(
            GEOCODING_URL
            + "?"
            + urlencode({"name": location, "count": "1", "language": "pl", "format": "json"})
        )
        if not isinstance(response, dict):
            return None
        results = response.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return None
        resolved = _location_from_geocoding(results[0])
        if resolved is None:
            return None
        cache = self._load_cache()
        cache[normalized_location] = {
            "name": resolved.name,
            "latitude": resolved.latitude,
            "longitude": resolved.longitude,
            "timezone": resolved.timezone,
            "country_code": resolved.country_code,
        }
        self._write_cache(cache)
        return resolved

    async def _forecast_json(self, location: "_OpenMeteoLocation", *, include_current: bool, forecast_days: int) -> dict[str, Any]:
        params = {
            "latitude": f"{location.latitude:.6f}",
            "longitude": f"{location.longitude:.6f}",
            "hourly": HOURLY_FIELDS,
            "daily": DAILY_FIELDS,
            "forecast_days": str(forecast_days),
            "timezone": "auto",
        }
        if include_current:
            params["current"] = CURRENT_FIELDS
        response = await self._fetch_json(FORECAST_URL + "?" + urlencode(params))
        return response if isinstance(response, dict) else {}

    async def _fetch_json(self, url: str) -> Any:
        session = self._session
        if session is None:
            timeout = ClientTimeout(total=10)
            session = ClientSession(
                timeout=timeout,
                headers={"User-Agent": "piotr-ai-server/1.0 weather-open-meteo"},
            )
            self._session = session
        async with session.get(url) as response:
            if response.status >= 400:
                raise RuntimeError(f"Open-Meteo request failed with status {response.status}")
            return await response.json()

    def _load_cache(self) -> dict[str, Any]:
        if not self._cache_path.exists():
            return {}
        try:
            with self._cache_path.open("r", encoding="utf-8") as cache_file:
                cache = json.load(cache_file)
        except (OSError, json.JSONDecodeError):
            self._logger.warning("failed to load Open-Meteo geocoding cache path=%s", self._cache_path)
            return {}
        return cache if isinstance(cache, dict) else {}

    def _write_cache(self, cache: dict[str, Any]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cache_path.open("w", encoding="utf-8") as cache_file:
            json.dump(cache, cache_file, ensure_ascii=False, indent=2, sort_keys=True)


class _OpenMeteoLocation:
    def __init__(self, *, name: str, latitude: float, longitude: float, timezone: str, country_code: str | None) -> None:
        self.name = name
        self.latitude = latitude
        self.longitude = longitude
        self.timezone = timezone
        self.country_code = country_code


def _target_dates(horizon: str, today: dt.date) -> set[dt.date]:
    if horizon == "today":
        return {today}
    if horizon == "tomorrow":
        return {today + dt.timedelta(days=1)}
    if horizon in {"weekend", "next_weekend"}:
        days_until_saturday = (5 - today.weekday()) % 7
        if horizon == "next_weekend" or days_until_saturday == 0 and today.weekday() > 5:
            days_until_saturday += 7
        saturday = today + dt.timedelta(days=days_until_saturday)
        return {saturday, saturday + dt.timedelta(days=1)}
    weekday = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }.get(horizon)
    if weekday is None:
        return {today}
    days = (weekday - today.weekday()) % 7
    if days == 0:
        days = 7
    return {today + dt.timedelta(days=days)}


def _daily_forecasts(raw_daily: Any, target_dates: set[dt.date]) -> tuple[DailyForecast, ...]:
    if not isinstance(raw_daily, dict):
        return ()
    dates = [_date_from_iso(value) for value in _list(raw_daily.get("time"))]
    result = []
    for index, date in enumerate(dates):
        if date is None or date not in target_dates:
            continue
        weather_code = _int_at(raw_daily, "weather_code", index)
        result.append(
            DailyForecast(
                date=date,
                weather_code=weather_code,
                weather_description=weather_code_description(weather_code),
                temperature_min_c=_float_at(raw_daily, "temperature_2m_min", index),
                temperature_max_c=_float_at(raw_daily, "temperature_2m_max", index),
                precipitation_sum_mm=_float_at(raw_daily, "precipitation_sum", index),
                precipitation_probability_percent=_float_at(raw_daily, "precipitation_probability_max", index),
                wind_speed_max_kmh=_float_at(raw_daily, "wind_speed_10m_max", index),
                wind_gusts_max_kmh=_float_at(raw_daily, "wind_gusts_10m_max", index),
            )
        )
    return tuple(result)


def _hourly_forecasts(raw_hourly: Any, target_dates: set[dt.date], timezone: ZoneInfo) -> tuple[HourlyForecast, ...]:
    if not isinstance(raw_hourly, dict):
        return ()
    times = [_datetime_from_local(value, timezone) for value in _list(raw_hourly.get("time"))]
    result = []
    for index, time in enumerate(times):
        if time is None or time.date() not in target_dates:
            continue
        weather_code = _int_at(raw_hourly, "weather_code", index)
        result.append(
            HourlyForecast(
                time=time,
                weather_code=weather_code,
                weather_description=weather_code_description(weather_code),
                temperature_c=_float_at(raw_hourly, "temperature_2m", index),
                apparent_temperature_c=_float_at(raw_hourly, "apparent_temperature", index),
                precipitation_mm=_float_at(raw_hourly, "precipitation", index),
                precipitation_probability_percent=_float_at(raw_hourly, "precipitation_probability", index),
                wind_speed_kmh=_float_at(raw_hourly, "wind_speed_10m", index),
                wind_gusts_kmh=_float_at(raw_hourly, "wind_gusts_10m", index),
            )
        )
    return tuple(result)


def _location_from_geocoding(raw: dict[str, Any]) -> _OpenMeteoLocation | None:
    name = _string_or_none(raw.get("name"))
    latitude = _float_or_none(raw.get("latitude"))
    longitude = _float_or_none(raw.get("longitude"))
    timezone = _string_or_none(raw.get("timezone"))
    if name is None or latitude is None or longitude is None or timezone is None:
        return None
    return _OpenMeteoLocation(
        name=name,
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
        country_code=_string_or_none(raw.get("country_code")),
    )


def _location_from_cache(raw: dict[str, Any]) -> _OpenMeteoLocation | None:
    return _location_from_geocoding(raw)


def _zone_from_name(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _date_from_iso(value: Any) -> dt.date | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def _datetime_from_local(value: Any, timezone: ZoneInfo) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value).replace(tzinfo=timezone)
    except ValueError:
        return None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _float_at(mapping: dict[str, Any], key: str, index: int) -> float | None:
    values = mapping.get(key)
    if not isinstance(values, list) or index >= len(values):
        return None
    return _float_or_none(values[index])


def _int_at(mapping: dict[str, Any], key: str, index: int) -> int | None:
    values = mapping.get(key)
    if not isinstance(values, list) or index >= len(values):
        return None
    return _int_or_none(values[index])


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
