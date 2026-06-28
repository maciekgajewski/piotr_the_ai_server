from __future__ import annotations

import datetime as dt
from dataclasses import asdict, is_dataclass
from typing import Any

from ai_server.domain_agents.weather.interfaces import CurrentWeather, DailyForecast, HourlyForecast, WeatherForecast
from ai_server.utils.polish_numbers import polish_cardinal, polish_decimal
from ai_server.utils.text import ascii_fold, normalize_text


WEEKDAYS = ("poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela")


def format_current_weather(weather: CurrentWeather, *, focus: str | None = None) -> str:
    location = _location_phrase(weather.location)
    temperature = _temperature_phrase(weather.temperature_c)
    if focus == "temperature":
        return f"{location} jest {temperature}."

    parts = [f"{location} jest {temperature}"]
    if weather.weather_description:
        parts.append(weather.weather_description)
    if weather.humidity_percent is not None:
        parts.append(f"wilgotność {_round_number(weather.humidity_percent)} procent")
    if weather.wind_speed_kmh is not None:
        parts.append(f"wiatr {_round_number(weather.wind_speed_kmh)} kilometrów na godzinę")
    if weather.precipitation_mm is not None and weather.precipitation_mm > 0:
        parts.append(f"opad {_format_decimal(weather.precipitation_mm)} milimetra")
    return ", ".join(parts) + "."


def format_forecast(forecast: WeatherForecast) -> str:
    location = _location_phrase(forecast.location)
    if forecast.daily:
        return _format_daily_forecast(forecast, location)
    if forecast.hourly:
        return _format_hourly_forecast(forecast, location)
    return f"Nie mam prognozy dla lokalizacji {forecast.location}."


def weather_to_json(value: Any) -> Any:
    if is_dataclass(value):
        return weather_to_json(asdict(value))
    if isinstance(value, dict):
        return {key: weather_to_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [weather_to_json(item) for item in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def weather_code_description(code: int | None) -> str | None:
    if code is None:
        return None
    if code == 0:
        return "bezchmurnie"
    if code in {1, 2}:
        return "częściowe zachmurzenie"
    if code == 3:
        return "pochmurno"
    if code in {45, 48}:
        return "mgła"
    if code in {51, 53, 55, 56, 57}:
        return "mżawka"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "deszcz"
    if code in {71, 73, 75, 77, 85, 86}:
        return "śnieg"
    if code in {95, 96, 99}:
        return "burza"
    return None


def _format_daily_forecast(forecast: WeatherForecast, location: str) -> str:
    if len(forecast.daily) == 1:
        day = forecast.daily[0]
        date_text = _date_phrase(day.date, forecast.horizon)
        sentence = f"{date_text} {location} będzie od {_temperature_phrase(day.temperature_min_c)} do {_temperature_phrase(day.temperature_max_c)}"
        if day.weather_description:
            sentence += f", {day.weather_description}"
        if day.precipitation_probability_percent is not None:
            sentence += f", szansa opadów {_round_number(day.precipitation_probability_percent)} procent"
        return sentence + "."

    parts = []
    for day in forecast.daily:
        label = WEEKDAYS[day.date.weekday()]
        temperatures = f"od {_round_number(day.temperature_min_c)} do {_round_number(day.temperature_max_c)} stopni"
        detail = f"{label}: {temperatures}"
        if day.weather_description:
            detail += f", {day.weather_description}"
        if day.precipitation_probability_percent is not None:
            detail += f", opady {_round_number(day.precipitation_probability_percent)} procent"
        parts.append(detail)
    horizon = "W następny weekend" if forecast.horizon == "next_weekend" else "W weekend"
    return f"{horizon} {location}: " + "; ".join(parts) + "."


def _format_hourly_forecast(forecast: WeatherForecast, location: str) -> str:
    visible_hours = forecast.hourly[:6]
    parts = []
    for hour in visible_hours:
        label = _hour_phrase(hour.time)
        detail = f"{label}: {_temperature_phrase(hour.temperature_c)}"
        if hour.weather_description:
            detail += f", {hour.weather_description}"
        if hour.precipitation_probability_percent is not None:
            detail += f", opady {_round_number(hour.precipitation_probability_percent)} procent"
        parts.append(detail)
    return f"Prognoza godzinowa {location}: " + "; ".join(parts) + "."


def _date_phrase(date: dt.date, horizon: str) -> str:
    if horizon == "today":
        return "Dzisiaj"
    if horizon == "tomorrow":
        return "Jutro"
    return f"W {WEEKDAYS[date.weekday()]}"


def _location_phrase(location: str) -> str:
    normalized = ascii_fold(normalize_text(location))
    if normalized in {"wroclaw", "wroclawiu", "wroclaw strachowice", "wroclawiu strachowicach"}:
        return "We Wrocławiu"
    if normalized in {"polska", "poland"}:
        return "W Polsce"
    if normalized.startswith(("a", "e", "i", "o", "u")):
        return f"We {location}"
    return f"W {location}"


def _temperature_phrase(value: float | None) -> str:
    if value is None:
        return "brak danych o temperaturze"
    return f"{_round_number(value)} stopni"


def _round_number(value: float | int | None) -> str:
    if value is None:
        return "?"
    rounded = round(float(value))
    return polish_cardinal(int(rounded))


def _format_decimal(value: float) -> str:
    if value == round(value):
        return polish_cardinal(int(value))
    return polish_decimal(value)


def _hour_phrase(value: dt.datetime) -> str:
    return f"o {polish_cardinal(value.hour)} zero zero"
