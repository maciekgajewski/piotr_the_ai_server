import asyncio
import datetime as dt
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ai_server.agent_loop import AgentReply
from ai_server.domain_agents.weather import (
    CurrentWeather,
    HourlyForecast,
    WeatherDomainAgent,
    WeatherForecast,
    WeatherForecastRequest,
    WeatherNowRequest,
)
from ai_server.domain_agents.weather.agent import WeatherDomainToolSet
from ai_server.domain_agents.weather.fast_lane import weather_task_from_utterance
from ai_server.domain_agents.weather.formatting import format_current_weather
from ai_server.domain_agents.weather.providers.imgw import ImgwWeatherProvider, _station_slug
from ai_server.domain_agents.weather.providers.open_meteo import OpenMeteoWeatherProvider
from ai_server.interfaces import Conversation
from ai_server.orchestrator.known_utterances import collect_known_utterance_tasks, known_utterance_task


def test_weather_planning_contract_routes_minimal_tasks(tmp_path: Path) -> None:
    agent = WeatherDomainAgent(
        model="qwen3:4b-instruct",
        location="Wrocław",
        cache_dir=tmp_path,
        providers=[],
    )

    assert agent.query_capabilities()["weather_state"].command_template == {
        "query": "original weather question",
    }
    assert '{"query": "original weather question"}' in agent.planning_prompt()
    assert "get_weather_forecast" not in agent.planning_prompt()
    assert '"location"' not in agent.planning_prompt()


def test_weather_fast_lane_creates_current_temperature_task() -> None:
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


def test_weather_fast_lane_creates_weekend_forecast_task() -> None:
    task = weather_task_from_utterance("Jaka pogoda w ten weekend?")

    assert task["command"]["tool"] == "get_weather_forecast"
    assert task["command"]["horizon"] == "weekend"
    assert task["command"]["granularity"] == "daily"
    assert task["command"]["query"] == "Jaka pogoda w ten weekend?"


@pytest.mark.parametrize(
    "utterance",
    [
        "Ile stopni?",
        "Jaka pogoda w ten weekend w Gdańsku?",
        "Czy dziś wieczorem będzie deszcz?",
        "Jaka pogoda na wekeend?",
    ],
)
def test_weather_fast_lane_avoids_ambiguous_or_location_queries(utterance: str) -> None:
    assert weather_task_from_utterance(utterance) is None


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
        (
            "jaka pogoda w ten weekend",
            {
                "tool": "get_weather_forecast",
                "query": "jaka pogoda w ten weekend",
                "horizon": "weekend",
                "granularity": "daily",
            },
        ),
    ],
)
def test_weather_known_utterances_are_explicit_rich_tasks(
    utterance: str,
    expected_command: dict[str, str],
    tmp_path: Path,
) -> None:
    agent = WeatherDomainAgent(
        model="qwen3:4b-instruct",
        location="Wrocław",
        cache_dir=tmp_path,
        providers=[],
    )
    task = known_utterance_task(utterance, collect_known_utterance_tasks({"weather": agent}))

    assert task["domain"] == "weather"
    assert task["command"] == expected_command


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
        ollama_connection=FakeOllamaConnection(),
    )

    result = asyncio.run(
        agent.run_task(
            Conversation(conversation_id="c1", attributes={"medium": "voice"}),
            {"id": "t1", "domain": "weather", "command": {"tool": "get_weather_now", "query": "Pogoda?"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["final_reply_mode"] == "verbatim"
    assert (
        result["text"]
        == "We Wrocławiu jest szesnaście stopni, wilgotność dziewięćdziesiąt procent, "
        "wiatr jedenaście kilometrów na godzinę, opad jeden przecinek cztery milimetra."
    )
    assert provider.now_requests == [WeatherNowRequest(location="Wrocław", focus=None)]


def test_weather_domain_agent_runs_agent_loop_for_non_fast_lane_query() -> None:
    loop_factory = FakeLoopFactory(
        json.dumps(
            {
                "status": "ok",
                "text": "Wieczorem we Wrocławiu prawdopodobnie będzie deszcz.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": ["weather.Wrocław"],
            },
            ensure_ascii=False,
        )
    )
    agent = WeatherDomainAgent(
        model="qwen3:4b-instruct",
        ollama_url="http://ollama:11434",
        fallback_model="qwen3:4b-fallback",
        fallback_backoff_seconds=120,
        location="Wrocław",
        cache_dir=Path("/tmp/piotr-test-cache"),
        providers=[FakeWeatherProvider()],
        loop_factory=loop_factory.factory,
        ollama_connection=FakeOllamaConnection(),
    )

    result = asyncio.run(
        agent.run_task(
            Conversation(conversation_id="c1", attributes={"medium": "voice"}),
            {
                "id": "t1",
                "domain": "weather",
                "command": {
                    "tool": "get_weather_now",
                    "query": "Czy dziś wieczorem będzie deszcz?",
                    "location": "Europe/Warsaw",
                    "horizon": "tomorrow",
                    "focus": "temperature",
                },
            },
            {"active_domain": "weather"},
        )
    )

    assert result["status"] == "ok"
    assert result["final_reply_mode"] == "verbatim"
    assert result["text"] == "Wieczorem we Wrocławiu prawdopodobnie będzie deszcz."
    assert loop_factory.config.model == "qwen3:4b-instruct"
    assert loop_factory.config.ollama_url == "http://ollama:11434"
    assert loop_factory.config.fallback_model == "qwen3:4b-fallback"
    assert loop_factory.config.fallback_backoff_seconds == 120
    assert isinstance(loop_factory.tools, WeatherDomainToolSet)
    payload = json.loads(loop_factory.loop.user_message)
    assert payload["task"]["command"] == {"query": "Czy dziś wieczorem będzie deszcz?"}
    assert payload["conversation"]["server_location"] == "Wrocław"


def test_weather_domain_agent_removes_celsius_degree_symbol_from_agent_loop_reply() -> None:
    loop_factory = FakeLoopFactory(
        json.dumps(
            {
                "status": "ok",
                "text": "W Gdańsku będzie około 20°C i opady 35 procent.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": ["weather.Gdańsk"],
            },
            ensure_ascii=False,
        )
    )
    agent = WeatherDomainAgent(
        model="qwen3:4b-instruct",
        location="Wrocław",
        cache_dir=Path("/tmp/piotr-test-cache"),
        providers=[FakeWeatherProvider()],
        loop_factory=loop_factory.factory,
        ollama_connection=FakeOllamaConnection(),
    )

    result = asyncio.run(
        agent.run_task(
            Conversation(conversation_id="c1", attributes={"medium": "voice"}),
            {"id": "t1", "domain": "weather", "command": {"query": "Jaka pogoda w Gdańsku?"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["text"] == "W Gdańsku będzie około dwadzieścia stopni i opady trzydzieści pięć procent."
    assert "°" not in result["text"]
    assert not any(character.isdigit() for character in result["text"])


def test_weather_domain_agent_rejects_non_json_agent_loop_reply() -> None:
    agent = WeatherDomainAgent(
        model="qwen3:4b-instruct",
        location="Wrocław",
        cache_dir=Path("/tmp/piotr-test-cache"),
        providers=[FakeWeatherProvider()],
        loop_factory=FakeLoopFactory("to nie jest json").factory,
        ollama_connection=FakeOllamaConnection(),
    )

    result = asyncio.run(
        agent.run_task(
            Conversation(conversation_id="c1", attributes={"medium": "voice"}),
            {"id": "t1", "domain": "weather", "command": {"query": "Czy brać parasol?"}},
            {},
        )
    )

    assert result["status"] == "failed"
    assert result["text"] == "Nie mogę teraz przygotować odpowiedzi pogodowej."


def test_weather_toolset_fetches_hourly_forecast_with_canonical_location() -> None:
    forecast = WeatherForecast(
        location="Gdańsk",
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
    toolset = WeatherDomainToolSet([provider], default_location="Wrocław")

    result = asyncio.run(
        toolset.get_weather_forecast(
            location="Gdańsk",
            horizon="dzisiaj",
            granularity="godzinowa",
        )
    )

    assert result["status"] == "ok"
    assert result["kind"] == "forecast"
    assert result["forecast"]["location"] == "Gdańsk"
    assert provider.forecast_requests == [WeatherForecastRequest(location="Gdańsk", horizon="today", granularity="hourly")]


def test_weather_toolset_reports_not_found_for_unknown_location() -> None:
    forecast = WeatherForecast(
        location="Szklarska Poręba",
        provider="fake",
        timezone="Europe/Warsaw",
        horizon="weekend",
        granularity="daily",
    )
    provider = LocationSensitiveWeatherProvider(
        forecast=forecast,
        forecast_location="Szklarska Poręba",
    )
    toolset = WeatherDomainToolSet([provider], default_location="Wrocław")

    result = asyncio.run(toolset.get_weather_forecast(location="Szkarskiej Porębie", horizon="weekend"))

    assert result["status"] == "not_found"
    assert provider.forecast_requests == [WeatherForecastRequest(location="Szkarskiej Porębie", horizon="weekend", granularity="daily")]


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

    assert format_current_weather(weather, focus="temperature") == "We Wrocławiu jest szesnaście stopni."


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


class LocationSensitiveWeatherProvider:
    name = "location_sensitive"

    def __init__(self, *, forecast: WeatherForecast, forecast_location: str) -> None:
        self._forecast = forecast
        self._forecast_location = forecast_location
        self.forecast_requests = []

    async def get_weather_now(self, request):
        return None

    async def get_weather_forecast(self, request):
        self.forecast_requests.append(request)
        if request.location == self._forecast_location:
            return self._forecast
        return None

    async def close(self) -> None:
        pass


class FakeLoopFactory:
    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.config = None
        self.tools = None
        self.loop = None

    def factory(self, config: Any, system_prompt: str, tools: Any, ollama_connection: Any, **kwargs: Any) -> "FakeLoop":
        self.config = config
        self.tools = tools
        self.loop = FakeLoop(self.reply_text)
        return self.loop


class FakeLoop:
    def __init__(self, reply_text: str) -> None:
        self._reply_text = reply_text
        self.user_message = ""

    async def __aenter__(self) -> "FakeLoop":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        pass

    async def send_user_message(self, message: str) -> AgentReply:
        self.user_message = message
        return AgentReply(reply_text=self._reply_text, end_conversation=False)


class FakeOllamaConnection:
    async def close(self) -> None:
        pass
