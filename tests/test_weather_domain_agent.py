import asyncio
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ai_server.domain_agents.weather import (
    CurrentWeather,
    HourlyForecast,
    WeatherDomainAgent,
    WeatherForecast,
    WeatherForecastRequest,
    WeatherNowRequest,
)
from ai_server.domain_agents.weather.formatting import format_current_weather
from ai_server.domain_agents.weather.parser import parse_weather_command, weather_task_from_utterance
from ai_server.domain_agents.weather.providers.imgw import ImgwWeatherProvider, _station_slug
from ai_server.domain_agents.weather.providers.open_meteo import OpenMeteoWeatherProvider
from ai_server.interfaces import Conversation
from ai_server.orchestrator.known_utterances import known_utterance_task


def test_weather_short_path_creates_rich_current_temperature_task() -> None:
    task = weather_task_from_utterance("Jaka jest temperatura?")

    assert task == {
        "id": "t1",
        "domain": "weather",
        "command": {
            "tool": "get_weather_now",
            "query": "Jaka jest temperatura?",
            "focus": "temperature",
        },
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }


def test_weather_short_path_creates_rich_weekend_forecast_task() -> None:
    task = weather_task_from_utterance("Jaka pogoda na wekeend?")

    assert task["command"]["tool"] == "get_weather_forecast"
    assert task["command"]["horizon"] == "weekend"
    assert task["command"]["granularity"] == "daily"
    assert task["command"]["query"] == "Jaka pogoda na wekeend?"


@pytest.mark.parametrize(
    ("utterance", "expected_command"),
    [
        ("Pogoda?", {"tool": "get_weather_now", "query": "Pogoda?"}),
        ("jaka jest pogoda", {"tool": "get_weather_now", "query": "jaka jest pogoda"}),
        (
            "jaka dziś pogoda",
            {"tool": "get_weather_forecast", "query": "jaka dziś pogoda", "horizon": "today", "granularity": "daily"},
        ),
        (
            "jaka jutro pogoda",
            {"tool": "get_weather_forecast", "query": "jaka jutro pogoda", "horizon": "tomorrow", "granularity": "daily"},
        ),
        (
            "jaka pogoda na weekend",
            {
                "tool": "get_weather_forecast",
                "query": "jaka pogoda na weekend",
                "horizon": "weekend",
                "granularity": "daily",
            },
        ),
    ],
)
def test_weather_known_utterances_are_explicit_rich_tasks(utterance: str, expected_command: dict[str, str]) -> None:
    task = known_utterance_task(utterance)

    assert task["domain"] == "weather"
    assert task["command"] == expected_command


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("weekend", "weekend"),
        ("next_weekeend", "next_weekend"),
        ("sobotę", "saturday"),
        ("piątek", "friday"),
    ],
)
def test_weather_parser_normalizes_horizon_variants(raw: str, expected: str) -> None:
    parsed = parse_weather_command(
        {"tool": "get_weather_forecast", "query": "pogoda", "horizon": raw},
        default_location="Wrocław",
    )

    assert parsed.horizon == expected
    assert parsed.location == "Wrocław"


def test_weather_parser_corrects_now_temperature_hint_for_evening_rain_query() -> None:
    parsed = parse_weather_command(
        {
            "tool": "get_weather_now",
            "query": "Czy dziś wieczorem będzie deszcz?",
            "focus": "temperature",
        },
        default_location="Wrocław",
    )

    assert parsed.tool == "get_weather_forecast"
    assert parsed.horizon == "today"
    assert parsed.granularity == "hourly"
    assert parsed.focus is None
    assert not parsed.simple


def test_imgw_provider_resolves_wroclaw_strachowice_alias() -> None:
    assert _station_slug("Wrocław") == "wroclaw"
    assert _station_slug("wroclaw-strachowice") == "wroclaw"
    assert _station_slug("Wrocław Strachowice") == "wroclaw"


def test_imgw_provider_parses_synop_current_weather() -> None:
    session = FakeSession(
        [
            {
                "id_stacji": "12424",
                "stacja": "Wrocław",
                "data_pomiaru": "2026-06-01",
                "godzina_pomiaru": "7",
                "temperatura": "15.7",
                "predkosc_wiatru": "3",
                "kierunek_wiatru": "270",
                "wilgotnosc_wzgledna": "90.0",
                "suma_opadu": "1.4",
                "cisnienie": "1012.3",
            }
        ]
    )
    provider = ImgwWeatherProvider(session=session)

    weather = asyncio.run(provider.get_weather_now(WeatherNowRequest(location="Wrocław-Strachowice")))

    assert weather.location == "Wrocław"
    assert weather.station_name == "Wrocław"
    assert weather.temperature_c == 15.7
    assert weather.wind_speed_kmh == pytest.approx(10.8)
    assert weather.observed_at.isoformat() == "2026-06-01T07:00:00+02:00"
    assert session.urls == ["https://danepubliczne.imgw.pl/api/data/synop/station/wroclaw"]


def test_open_meteo_provider_parses_daily_forecast(tmp_path: Path) -> None:
    session = FakeSession(
        [
            {
                "results": [
                    {
                        "name": "Wrocław",
                        "latitude": 51.10286,
                        "longitude": 17.03006,
                        "timezone": "Europe/Warsaw",
                        "country_code": "PL",
                    }
                ]
            },
            {
                "timezone": "Europe/Warsaw",
                "daily": {
                    "time": ["2026-06-06", "2026-06-07"],
                    "weather_code": [3, 61],
                    "temperature_2m_max": [25.0, 22.0],
                    "temperature_2m_min": [12.6, 16.3],
                    "precipitation_sum": [0.0, 0.6],
                    "precipitation_probability_max": [34, 20],
                    "wind_speed_10m_max": [7.6, 17.6],
                    "wind_gusts_10m_max": [19.1, 41.0],
                },
                "hourly": {"time": []},
            },
        ]
    )
    provider = OpenMeteoWeatherProvider(
        cache_dir=tmp_path,
        session=session,
        now_factory=lambda zone: dt.datetime(2026, 6, 1, 9, tzinfo=zone),
    )

    forecast = asyncio.run(
        provider.get_weather_forecast(
            WeatherForecastRequest(location="Wrocław", horizon="weekend", granularity="daily")
        )
    )

    assert forecast.location == "Wrocław"
    assert forecast.provider == "open_meteo"
    assert [day.date.isoformat() for day in forecast.daily] == ["2026-06-06", "2026-06-07"]
    assert forecast.daily[1].weather_description == "deszcz"


def test_weather_domain_agent_formats_simple_current_weather() -> None:
    provider = FakeWeatherProvider(
        current=CurrentWeather(
            location="Wrocław",
            provider="imgw",
            observed_at=dt.datetime(2026, 6, 1, 7, tzinfo=ZoneInfo("Europe/Warsaw")),
            station_name="Wrocław",
            temperature_c=15.7,
            humidity_percent=90.0,
            pressure_hpa=1012.3,
            wind_speed_kmh=10.8,
            wind_direction_deg=270,
            precipitation_mm=1.4,
        )
    )
    agent = WeatherDomainAgent(
        model="qwen3:4b-instruct",
        location="Wrocław",
        cache_dir=Path("/tmp/piotr-test-cache"),
        providers=[provider],
        ollama_client=FakeOllamaClient(),
    )

    result = asyncio.run(
        agent.run_task(
            Conversation(conversation_id="c1", attributes={}),
            {"id": "t1", "domain": "weather", "command": {"tool": "get_weather_now", "query": "Pogoda?"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["final_reply_mode"] == "verbatim"
    assert result["text"] == "We Wrocławiu jest 16 stopni, wilgotność 90 procent, wiatr 11 kilometrów na godzinę, opad 1.4 milimetra."
    assert provider.now_requests == [WeatherNowRequest(location="Wrocław", focus=None)]


def test_weather_domain_agent_uses_forecast_for_evening_rain_even_when_planner_asked_now() -> None:
    forecast = WeatherForecast(
        location="Wrocław",
        provider="fake",
        timezone="Europe/Warsaw",
        horizon="today",
        granularity="hourly",
        hourly=(
            HourlyForecast(
                time=dt.datetime(2026, 6, 1, 18, tzinfo=ZoneInfo("Europe/Warsaw")),
                weather_code=61,
                weather_description="deszcz",
                temperature_c=20.0,
                apparent_temperature_c=20.0,
                precipitation_mm=1.0,
                precipitation_probability_percent=80.0,
                wind_speed_kmh=8.0,
                wind_gusts_kmh=16.0,
            ),
        ),
    )
    provider = FakeWeatherProvider(forecast=forecast)
    ollama = ReplyingOllamaClient("Wieczorem we Wrocławiu prawdopodobnie będzie deszcz.")
    agent = WeatherDomainAgent(
        model="qwen3:4b-instruct",
        location="Wrocław",
        cache_dir=Path("/tmp/piotr-test-cache"),
        providers=[provider],
        ollama_client=ollama,
    )

    result = asyncio.run(
        agent.run_task(
            Conversation(conversation_id="c1", attributes={}),
            {
                "id": "t1",
                "domain": "weather",
                "command": {
                    "tool": "get_weather_now",
                    "query": "Czy dziś wieczorem będzie deszcz?",
                    "focus": "temperature",
                },
            },
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["text"] == "Wieczorem we Wrocławiu prawdopodobnie będzie deszcz."
    assert provider.now_requests == []
    assert provider.forecast_requests == [WeatherForecastRequest(location="Wrocław", horizon="today", granularity="hourly")]


def test_format_current_weather_temperature_focus() -> None:
    weather = CurrentWeather(
        location="Wrocław",
        provider="imgw",
        observed_at=dt.datetime(2026, 6, 1, 7, tzinfo=ZoneInfo("Europe/Warsaw")),
        station_name="Wrocław",
        temperature_c=15.7,
        humidity_percent=None,
        pressure_hpa=None,
        wind_speed_kmh=None,
        wind_direction_deg=None,
        precipitation_mm=None,
    )

    assert format_current_weather(weather, focus="temperature") == "We Wrocławiu jest 16 stopni."


class FakeSession:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.urls = []

    def get(self, url: str):
        self.urls.append(url)
        return FakeResponse(self.responses.pop(0))


class FakeResponse:
    status = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def json(self):
        return self._payload


class FakeWeatherProvider:
    name = "fake"

    def __init__(self, current: CurrentWeather | None = None, forecast: WeatherForecast | None = None) -> None:
        self.current = current
        self.forecast = forecast
        self.now_requests = []
        self.forecast_requests = []

    async def get_weather_now(self, request: WeatherNowRequest):
        self.now_requests.append(request)
        return self.current

    async def get_weather_forecast(self, request):
        self.forecast_requests.append(request)
        return self.forecast

    async def close(self) -> None:
        pass


class FakeOllamaClient:
    async def chat(self, payload: dict):
        raise AssertionError("unexpected weather LLM call")

    async def close(self) -> None:
        pass


class ReplyingOllamaClient:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests = []

    async def chat(self, payload: dict):
        self.requests.append(payload)
        return {"message": {"role": "assistant", "content": self.reply}}

    async def close(self) -> None:
        pass
