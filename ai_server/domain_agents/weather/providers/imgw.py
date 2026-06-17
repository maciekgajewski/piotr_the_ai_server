from __future__ import annotations

import datetime as dt
import logging
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

from ai_server.domain_agents.weather.interfaces import CurrentWeather, WeatherForecast, WeatherForecastRequest, WeatherNowRequest
from ai_server.utils.text import ascii_fold, normalize_text


IMGW_BASE_URL = "https://danepubliczne.imgw.pl/api/data/synop"
IMGW_TIMEZONE = ZoneInfo("Europe/Warsaw")
STATION_ALIASES = {
    "wroclaw": "wroclaw",
    "wroclawiu": "wroclaw",
    "wroclaw strachowice": "wroclaw",
    "wroclawiu strachowice": "wroclaw",
    "wroclaw strachowicach": "wroclaw",
    "wroclaw-strachowice": "wroclaw",
}


class ImgwWeatherProvider:
    name = "imgw"

    def __init__(self, *, session: ClientSession | None = None) -> None:
        self._session = session
        self._owns_session = session is None
        self._logger = logging.getLogger(f"{__name__}.ImgwWeatherProvider")

    async def get_weather_now(self, request: WeatherNowRequest) -> CurrentWeather | None:
        station_slug = _station_slug(request.location)
        if station_slug is None:
            return None
        response = await self._fetch_json(f"{IMGW_BASE_URL}/station/{quote(station_slug)}")
        if not isinstance(response, dict) or response.get("status") is False:
            return None
        return _current_weather_from_synop(response, requested_location=request.location)

    async def get_weather_forecast(self, request: WeatherForecastRequest) -> WeatherForecast | None:
        del request
        return None

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
                headers={"User-Agent": "piotr-ai-server/1.0 weather-imgw"},
            )
            self._session = session
        async with session.get(url) as response:
            if response.status >= 400:
                self._logger.debug("IMGW request failed url=%s status=%s", url, response.status)
                return None
            return await response.json()


def _station_slug(location: str) -> str | None:
    normalized = ascii_fold(normalize_text(location)).replace("-", " ")
    normalized = " ".join(normalized.split())
    if normalized in STATION_ALIASES:
        return STATION_ALIASES[normalized]
    if not normalized:
        return None
    if any(character.isdigit() for character in normalized):
        return None
    if len(normalized.split()) > 2:
        return None
    return normalized.replace(" ", "")


def _current_weather_from_synop(payload: dict[str, Any], *, requested_location: str) -> CurrentWeather:
    station_name = _string_or_none(payload.get("stacja"))
    location = station_name or requested_location
    observed_at = _observed_at(payload)
    wind_speed_ms = _float_or_none(payload.get("predkosc_wiatru"))
    return CurrentWeather(
        location=location,
        provider=ImgwWeatherProvider.name,
        observed_at=observed_at,
        station_name=station_name,
        temperature_c=_float_or_none(payload.get("temperatura")),
        humidity_percent=_float_or_none(payload.get("wilgotnosc_wzgledna")),
        pressure_hpa=_float_or_none(payload.get("cisnienie")),
        wind_speed_kmh=wind_speed_ms * 3.6 if wind_speed_ms is not None else None,
        wind_direction_deg=_int_or_none(payload.get("kierunek_wiatru")),
        precipitation_mm=_float_or_none(payload.get("suma_opadu")),
    )


def _observed_at(payload: dict[str, Any]) -> dt.datetime:
    raw_date = payload.get("data_pomiaru")
    raw_hour = payload.get("godzina_pomiaru")
    if isinstance(raw_date, str) and isinstance(raw_hour, str):
        try:
            date = dt.date.fromisoformat(raw_date)
            hour = int(raw_hour)
            return dt.datetime(date.year, date.month, date.day, hour, tzinfo=IMGW_TIMEZONE)
        except ValueError:
            pass
    return dt.datetime.now(IMGW_TIMEZONE)


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
