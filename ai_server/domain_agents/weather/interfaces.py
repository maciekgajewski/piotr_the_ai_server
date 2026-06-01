from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WeatherNowRequest:
    location: str
    focus: str | None = None


@dataclass(frozen=True)
class WeatherForecastRequest:
    location: str
    horizon: str
    granularity: str


@dataclass(frozen=True)
class CurrentWeather:
    location: str
    provider: str
    observed_at: dt.datetime
    station_name: str | None
    temperature_c: float | None
    humidity_percent: float | None
    pressure_hpa: float | None
    wind_speed_kmh: float | None
    wind_direction_deg: int | None
    precipitation_mm: float | None
    weather_code: int | None = None
    weather_description: str | None = None
    apparent_temperature_c: float | None = None
    cloud_cover_percent: float | None = None


@dataclass(frozen=True)
class DailyForecast:
    date: dt.date
    weather_code: int | None
    weather_description: str | None
    temperature_min_c: float | None
    temperature_max_c: float | None
    precipitation_sum_mm: float | None
    precipitation_probability_percent: float | None
    wind_speed_max_kmh: float | None
    wind_gusts_max_kmh: float | None


@dataclass(frozen=True)
class HourlyForecast:
    time: dt.datetime
    weather_code: int | None
    weather_description: str | None
    temperature_c: float | None
    apparent_temperature_c: float | None
    precipitation_mm: float | None
    precipitation_probability_percent: float | None
    wind_speed_kmh: float | None
    wind_gusts_kmh: float | None


@dataclass(frozen=True)
class WeatherForecast:
    location: str
    provider: str
    timezone: str | None
    horizon: str
    granularity: str
    daily: tuple[DailyForecast, ...] = ()
    hourly: tuple[HourlyForecast, ...] = ()


class WeatherProvider(Protocol):
    name: str

    async def get_weather_now(self, request: WeatherNowRequest) -> CurrentWeather | None:
        raise NotImplementedError

    async def get_weather_forecast(self, request: WeatherForecastRequest) -> WeatherForecast | None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
