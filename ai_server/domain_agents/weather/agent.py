from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from aiohttp import ClientSession

from ai_server.domain_agents.interfaces import DomainTask
from ai_server.domain_agents.weather.formatting import format_current_weather, format_forecast, weather_to_json
from ai_server.domain_agents.weather.interfaces import (
    CurrentWeather,
    WeatherForecast,
    WeatherForecastRequest,
    WeatherNowRequest,
    WeatherProvider,
)
from ai_server.domain_agents.weather.messages import WEATHER_COMPLEX_SYSTEM_PROMPT, WEATHER_LOCATION_CANONICALIZATION_SYSTEM_PROMPT
from ai_server.domain_agents.weather.parser import ParsedWeatherCommand, parse_weather_command
from ai_server.utils.text import normalize_text
from ai_server.domain_agents.weather.providers.imgw import ImgwWeatherProvider
from ai_server.domain_agents.weather.providers.open_meteo import OpenMeteoWeatherProvider
from ai_server.interfaces import Conversation
from ai_server.ollama import OLLAMA_BASE_URL, OllamaClient


class WeatherDomainAgent:
    def __init__(
        self,
        *,
        model: str,
        ollama_url: str = OLLAMA_BASE_URL,
        fallback_model: str | None = None,
        fallback_backoff_seconds: float = 300.0,
        location: str | None,
        cache_dir: Path,
        providers: list[WeatherProvider] | None = None,
        session: ClientSession | None = None,
        ollama_client: OllamaClient | None = None,
    ) -> None:
        self._model = model
        self._ollama_url = ollama_url
        self._fallback_model = fallback_model
        self._fallback_backoff_seconds = fallback_backoff_seconds
        self._location = location
        self._session = session
        self._providers = providers or [
            ImgwWeatherProvider(session=session),
            OpenMeteoWeatherProvider(cache_dir=cache_dir, session=session),
        ]
        self._owns_providers = providers is None
        self._ollama = ollama_client or OllamaClient(base_url=ollama_url, session=session)
        self._owns_ollama = ollama_client is None
        self._fallback_until = 0.0
        self._logger = logging.getLogger(f"{__name__}.WeatherDomainAgent[{model}:{location or 'no-location'}]")

    async def run_task(
        self,
        conversation: Conversation,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        del active_context
        command = task.get("command", {})
        command = command if isinstance(command, dict) else {}
        parsed = parse_weather_command(command, default_location=self._location)
        logger = logging.getLogger(
            f"{__name__}.WeatherDomainAgent[{self._model}:{conversation.conversation_id}:{task.get('id', 'unknown')}]"
        )
        logger.info(
            "running weather task tool=%s location=%r horizon=%r granularity=%s simple=%s",
            parsed.tool,
            parsed.location,
            parsed.horizon,
            parsed.granularity,
            parsed.simple,
        )

        if not parsed.location:
            return _clarification_result("Dla jakiej lokalizacji mam sprawdzić pogodę?")

        if parsed.tool == "get_weather_now":
            weather = await self._get_weather_now(parsed)
            if weather is None:
                canonical = await self._with_canonical_location(parsed)
                if canonical is not None:
                    weather = await self._get_weather_now(canonical)
            if weather is None:
                return _not_found_result(parsed.location)
            return await self._result_for_current_weather(parsed, weather)

        forecast = await self._get_weather_forecast(parsed)
        if forecast is None:
            canonical = await self._with_canonical_location(parsed)
            if canonical is not None:
                forecast = await self._get_weather_forecast(canonical)
        if forecast is None:
            return _not_found_result(parsed.location)
        return await self._result_for_forecast(parsed, forecast)

    async def close(self) -> None:
        if self._owns_providers:
            for provider in self._providers:
                await provider.close()
        if self._owns_ollama:
            await self._ollama.close()

    async def _get_weather_now(self, parsed: ParsedWeatherCommand) -> CurrentWeather | None:
        request = WeatherNowRequest(location=parsed.location, focus=parsed.focus)
        for provider in self._providers:
            try:
                weather = await provider.get_weather_now(request)
            except Exception:
                self._logger.debug("weather provider failed provider=%s request=%s", provider.name, request, exc_info=True)
                continue
            if weather is not None:
                return weather
        return None

    async def _get_weather_forecast(self, parsed: ParsedWeatherCommand) -> WeatherForecast | None:
        request = WeatherForecastRequest(
            location=parsed.location,
            horizon=parsed.horizon or "today",
            granularity=parsed.granularity,
        )
        for provider in self._providers:
            try:
                forecast = await provider.get_weather_forecast(request)
            except Exception:
                self._logger.debug("weather provider failed provider=%s request=%s", provider.name, request, exc_info=True)
                continue
            if forecast is not None:
                return forecast
        return None

    async def _result_for_current_weather(self, parsed: ParsedWeatherCommand, weather: CurrentWeather) -> dict[str, Any]:
        data = weather_to_json(weather)
        if parsed.simple or not parsed.query:
            return _ok_result(
                text=format_current_weather(weather, focus=parsed.focus),
                data={"kind": "current", "weather": data},
                entities=[f"weather.{weather.location}"],
                final_reply_mode="verbatim",
            )
        return _ok_result(
            text=await self._complex_answer(parsed, {"kind": "current", "weather": data}),
            data={"kind": "current", "weather": data},
            entities=[f"weather.{weather.location}"],
            final_reply_mode="verbatim",
        )

    async def _result_for_forecast(self, parsed: ParsedWeatherCommand, forecast: WeatherForecast) -> dict[str, Any]:
        data = weather_to_json(forecast)
        if parsed.simple or not parsed.query:
            return _ok_result(
                text=format_forecast(forecast),
                data={"kind": "forecast", "forecast": data},
                entities=[f"weather.{forecast.location}"],
                final_reply_mode="verbatim",
            )
        return _ok_result(
            text=await self._complex_answer(parsed, {"kind": "forecast", "forecast": data}),
            data={"kind": "forecast", "forecast": data},
            entities=[f"weather.{forecast.location}"],
            final_reply_mode="verbatim",
        )

    async def _complex_answer(self, parsed: ParsedWeatherCommand, weather_data: dict[str, Any]) -> str:
        payload = {
            "query": parsed.query,
            "tool": parsed.tool,
            "location": parsed.location,
            "focus": parsed.focus,
            "horizon": parsed.horizon,
            "granularity": parsed.granularity,
            "weather_data": weather_data,
        }
        response = await self._chat_with_fallback(
            {
                "raw": False,
                "think": False,
                "stream": False,
                "keep_alive": "1h",
                "options": {"num_predict": 192, "temperature": 0, "num_ctx": 4096},
                "messages": [
                    {"role": "system", "content": WEATHER_COMPLEX_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            }
        )
        message = response.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            return "Nie mogę teraz przygotować odpowiedzi pogodowej."
        return message["content"].strip() or "Nie mogę teraz przygotować odpowiedzi pogodowej."

    async def _with_canonical_location(self, parsed: ParsedWeatherCommand) -> ParsedWeatherCommand | None:
        canonical_location = await self._canonicalize_location(parsed)
        if canonical_location is None or normalize_text(canonical_location) == normalize_text(parsed.location):
            return None
        self._logger.info("retrying weather task with canonical location original=%r canonical=%r", parsed.location, canonical_location)
        return replace(parsed, location=canonical_location)

    async def _canonicalize_location(self, parsed: ParsedWeatherCommand) -> str | None:
        payload = {
            "query": parsed.query,
            "location": parsed.location,
            "server_location": self._location,
        }
        try:
            response = await self._chat_with_fallback(
                {
                    "raw": False,
                    "think": False,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "1h",
                    "options": {"num_predict": 64, "temperature": 0, "num_ctx": 2048},
                    "messages": [
                        {"role": "system", "content": WEATHER_LOCATION_CANONICALIZATION_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                }
            )
        except Exception:
            self._logger.debug("weather location canonicalization failed location=%r", parsed.location, exc_info=True)
            return None
        return _parse_canonical_location(response)

    async def _chat_with_fallback(self, payload: dict[str, Any]) -> dict[str, Any]:
        model = self._fallback_model if self._fallback_model and time.monotonic() < self._fallback_until else self._model
        try:
            return await self._ollama.chat({**payload, "model": model})
        except Exception:
            if self._fallback_model is None or model == self._fallback_model:
                raise
            self._fallback_until = time.monotonic() + self._fallback_backoff_seconds
            self._logger.warning("weather DSA model failed, retrying fallback_model=%s", self._fallback_model, exc_info=True)
            return await self._ollama.chat({**payload, "model": self._fallback_model})


def _ok_result(
    *,
    text: str,
    data: dict[str, Any],
    entities: list[str],
    final_reply_mode: str,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "text": text,
        "needs_clarification": False,
        "clarification_question": None,
        "entities": entities,
        "final_reply_mode": final_reply_mode,
        "data": data,
    }


def _clarification_result(question: str) -> dict[str, Any]:
    return {
        "status": "needs_clarification",
        "text": question,
        "needs_clarification": True,
        "clarification_question": question,
        "entities": [],
    }


def _not_found_result(location: str) -> dict[str, Any]:
    return {
        "status": "not_found",
        "text": f"Nie znalazłem danych pogodowych dla lokalizacji: {location}.",
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [],
    }


def _parse_canonical_location(response: dict[str, Any]) -> str | None:
    message = response.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or confidence < 0.55:
        return None
    location = raw.get("location")
    return location.strip() if isinstance(location, str) and location.strip() else None
