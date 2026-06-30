from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Annotated, Any, Callable

from aiohttp import ClientSession

from ai_server.agent_loop import AgentCallableSet, AgentLoop, AgentLoopConfig, AgentLoopOllamaConnection
from ai_server.domain_agents.interfaces import DomainTask, QueryCapability
from ai_server.domain_agents.weather.astronomy import (
    IPGeolocationAstronomyClient,
    WeatherAstronomyRefresher,
    WeatherAstronomyStore,
    astronomy_to_json,
    format_astronomy,
)
from ai_server.domain_agents.weather.formatting import format_current_weather, format_forecast, weather_to_json
from ai_server.domain_agents.weather.interfaces import (
    CurrentWeather,
    WeatherForecast,
    WeatherForecastRequest,
    WeatherNowRequest,
    WeatherProvider,
)
from ai_server.domain_agents.weather.fast_lane import fast_lane_command_from_task_command, known_weather_utterances
from ai_server.domain_agents.weather.local_cache import WeatherLocalCache
from ai_server.domain_agents.weather.messages import WEATHER_AGENT_SYSTEM_PROMPT
from ai_server.domain_agents.weather.providers.imgw import ImgwWeatherProvider
from ai_server.domain_agents.weather.providers.open_meteo import OpenMeteoWeatherProvider
from ai_server.interfaces import Conversation
from ai_server.ollama_client import OLLAMA_BASE_URL
from ai_server.utils import JsonFileStore
from ai_server.utils.conversation_style import reply_style_instruction, system_prompt_with_reply_style
from ai_server.utils.polish_numbers import polish_cardinal, polish_decimal
from ai_server.utils.text import ascii_fold, normalize_text


PLANNING_PROMPT = """
For weather tasks:
- Only route the utterance to the weather domain. The weather agent owns parsing current versus forecast,
  astronomy facts, forecast horizon, location, and focus.
- Do not include weather tool names, locations, horizons, granularities, astronomy fields, or focus fields.

Command shape:
{"query": "original weather question"}
"""


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
        data_store: JsonFileStore | None = None,
        providers: list[WeatherProvider] | None = None,
        ipgeolocation_api_key: str | None = None,
        session: ClientSession | None = None,
        astronomy_refresher: WeatherAstronomyRefresher | None = None,
        local_weather_cache: WeatherLocalCache | None = None,
        ollama_connection: AgentLoopOllamaConnection | None = None,
        loop_factory: Callable[..., AgentLoop] = AgentLoop,
        processing_update_interval_seconds: float = 5.0,
    ) -> None:
        self._model = model
        self._ollama_url = ollama_url
        self._fallback_model = fallback_model
        self._fallback_backoff_seconds = fallback_backoff_seconds
        self._location = location
        self._providers = providers or [
            ImgwWeatherProvider(session=session),
            OpenMeteoWeatherProvider(cache_dir=cache_dir, session=session),
        ]
        self._astronomy_refresher = astronomy_refresher or _create_astronomy_refresher(
            location=location,
            data_store=data_store,
            api_key=ipgeolocation_api_key,
            session=session,
        )
        self._local_weather_cache = local_weather_cache or WeatherLocalCache(location=location, providers=self._providers)
        self._owns_providers = providers is None
        self._ollama_connection = ollama_connection or AgentLoopOllamaConnection(base_url=ollama_url, session=session)
        self._owns_ollama_connection = ollama_connection is None
        self._loop_factory = loop_factory
        self._processing_update_interval_seconds = processing_update_interval_seconds
        self._started = False
        self._logger = logging.getLogger(f"{__name__}.WeatherDomainAgent[{model}:{location or 'no-location'}]")

    async def ensure_started(self) -> None:
        if self._started:
            return
        if self._local_weather_cache is not None:
            self._logger.info("starting local weather cache")
            await self._local_weather_cache.start()
            self._logger.info("local weather cache ready")
        if self._astronomy_refresher is not None:
            self._logger.info("starting weather astronomy support")
            await self._astronomy_refresher.start()
            self._logger.info("weather astronomy support ready")
        self._started = True

    def known_utterances(self) -> dict[str, DomainTask]:
        return known_weather_utterances()

    def query_capabilities(self) -> dict[str, QueryCapability]:
        return {
            "weather_state": QueryCapability(
                name="Current weather, forecast, and sky facts",
                description=(
                    "Read current weather, forecasts, sunrise, sunset, moonrise, moonset, moon phase, "
                    "or day-length facts for the default location or an explicitly named place."
                ),
                command_template={
                    "query": "original weather question",
                },
            )
        }

    def query_capabilities_prompt(self) -> str:
        return ""

    def planning_prompt(self) -> str:
        return PLANNING_PROMPT

    async def run_task(
        self,
        conversation: Conversation,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        command = task.get("command", {})
        command = command if isinstance(command, dict) else {}
        logger = logging.getLogger(
            f"{__name__}.WeatherDomainAgent[{self._model}:{conversation.conversation_id}:{task.get('id', 'unknown')}]"
        )
        fast_command = fast_lane_command_from_task_command(command)
        if fast_command is not None:
            logger.info(
                "weather DSA using fast-lane conversation_id=%s task_id=%s tool=%s",
                conversation.conversation_id,
                task.get("id", "unknown"),
                fast_command["tool"],
            )
            return await self._run_fast_lane(fast_command, logger)

        task = _minimal_task_for_agent_loop(task)

        toolset = WeatherDomainToolSet(
            self._providers,
            default_location=self._location,
            local_weather_cache=self._local_weather_cache,
            astronomy_refresher=self._astronomy_refresher,
            logger_name=f"{__name__}.WeatherDomainToolSet[{conversation.conversation_id}:{task.get('id', 'unknown')}]",
        )
        loop_config = AgentLoopConfig(
            model=self._model,
            ollama_url=self._ollama_url,
            fallback_model=self._fallback_model,
            fallback_backoff_seconds=self._fallback_backoff_seconds,
            options={"num_predict": 512, "temperature": 0, "num_ctx": 4096},
            keep_alive="1h",
        )
        payload = {
            "task": task,
            "active_context": active_context,
            "conversation": {
                "user": conversation.user,
                "area": conversation.area,
                "medium": conversation.medium.value,
                "reply_style": reply_style_instruction(conversation.medium),
                "server_location": self._location,
                "user_settings": conversation.user_settings,
            },
        }
        logger.info(
            "weather DSA LLM request conversation_id=%s task_id=%s cloud_model=%s local_model=%s intent=%s payload_len=%s",
            conversation.conversation_id,
            task.get("id", "unknown"),
            self._model,
            self._fallback_model,
            _task_intent(task),
            len(json.dumps(payload, ensure_ascii=False)),
        )
        logger.debug("running Weather DSA agent loop task=%s active_context=%s", task, active_context)
        async with self._loop_factory(
            config=loop_config,
            system_prompt=system_prompt_with_reply_style(WEATHER_AGENT_SYSTEM_PROMPT, conversation.medium),
            tools=toolset,
            ollama_connection=self._ollama_connection,
            processing_update_callback=conversation.processing_update_callback,
            processing_update_interval_seconds=self._processing_update_interval_seconds,
        ) as loop:
            reply = await loop.send_user_message(json.dumps(payload, ensure_ascii=False))
        prompt_tokens = getattr(reply, "prompt_eval_count", None)
        completion_tokens = getattr(reply, "eval_count", None)
        duration_ms = getattr(reply, "duration_ms", None)
        logger.info(
            "weather DSA LLM reply conversation_id=%s task_id=%s cloud_model=%s local_model=%s end_conversation=%s "
            "reply_len=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s duration_ms=%s",
            conversation.conversation_id,
            task.get("id", "unknown"),
            self._model,
            self._fallback_model,
            reply.end_conversation,
            len(reply.reply_text),
            prompt_tokens,
            completion_tokens,
            _token_total(prompt_tokens, completion_tokens),
            duration_ms,
        )
        logger.debug("Weather DSA raw reply=%r end_conversation=%s", reply.reply_text, reply.end_conversation)
        if reply.end_conversation:
            logger.info(
                "weather DSA failed conversation_id=%s task_id=%s reason=end_conversation",
                conversation.conversation_id,
                task.get("id", "unknown"),
            )
            return _failed_result("Nie mogę teraz sprawdzić pogody.")
        try:
            result = _parse_domain_reply(reply.reply_text)
        except ValueError as exc:
            logger.warning(
                "weather DSA failed invalid model reply conversation_id=%s task_id=%s parse_error=%s reply=%r",
                conversation.conversation_id,
                task.get("id", "unknown"),
                exc,
                _abbreviate(reply.reply_text),
            )
            logger.debug("rejecting invalid Weather DSA reply=%r", reply.reply_text)
            return _failed_result("Nie mogę teraz przygotować odpowiedzi pogodowej.")
        logger.info(
            "weather DSA completed from model final JSON conversation_id=%s task_id=%s status=%s",
            conversation.conversation_id,
            task.get("id", "unknown"),
            result.get("status"),
        )
        return result

    async def close(self) -> None:
        if self._local_weather_cache is not None:
            await self._local_weather_cache.close()
        if self._astronomy_refresher is not None:
            await self._astronomy_refresher.close()
        if self._owns_providers:
            for provider in self._providers:
                await provider.close()
        if self._owns_ollama_connection:
            await self._ollama_connection.close()

    async def _run_fast_lane(self, command: dict[str, str], logger: logging.Logger) -> dict[str, Any]:
        location = self._location or ""
        tool = command["tool"]
        focus = command.get("focus")
        horizon = command.get("horizon")
        granularity = command.get("granularity", "daily")
        logger.info(
            "running fast-lane weather task tool=%s location=%r horizon=%r granularity=%s",
            tool,
            location,
            horizon,
            granularity,
        )

        if not location:
            return _clarification_result("Dla jakiej lokalizacji mam sprawdzić pogodę?")

        if tool == "get_weather_now":
            request = WeatherNowRequest(location=location, focus=focus)
            weather = await self._get_weather_now(request)
            if weather is None:
                return _not_found_result(location)
            return _result_for_current_weather(weather, focus=focus)

        request = WeatherForecastRequest(location=location, horizon=horizon or "today", granularity=granularity)
        forecast = await self._get_weather_forecast(request)
        if forecast is None:
            return _not_found_result(location)
        return _result_for_forecast(forecast)

    async def _get_weather_now(self, request: WeatherNowRequest) -> CurrentWeather | None:
        if self._local_weather_cache is not None:
            cached = self._local_weather_cache.current_weather(request)
            if cached is not None:
                self._logger.debug("serving current weather from local cache request=%s", request)
                return cached
        for provider in self._providers:
            try:
                weather = await provider.get_weather_now(request)
            except Exception:
                self._logger.debug("weather provider failed provider=%s request=%s", provider.name, request, exc_info=True)
                continue
            if weather is not None:
                return weather
        return None

    async def _get_weather_forecast(self, request: WeatherForecastRequest) -> WeatherForecast | None:
        if self._local_weather_cache is not None:
            cached = self._local_weather_cache.weather_forecast(request)
            if cached is not None:
                self._logger.debug("serving weather forecast from local cache request=%s", request)
                return cached
        for provider in self._providers:
            try:
                forecast = await provider.get_weather_forecast(request)
            except Exception:
                self._logger.debug("weather provider failed provider=%s request=%s", provider.name, request, exc_info=True)
                continue
            if forecast is not None:
                return forecast
        return None


class WeatherDomainToolSet(AgentCallableSet):
    def __init__(
        self,
        providers: list[WeatherProvider],
        *,
        default_location: str | None,
        local_weather_cache: WeatherLocalCache | None = None,
        astronomy_refresher: WeatherAstronomyRefresher | None = None,
        logger_name: str | None = None,
    ) -> None:
        self._providers = providers
        self._default_location = default_location
        self._local_weather_cache = local_weather_cache
        self._astronomy_refresher = astronomy_refresher
        self._logger = logging.getLogger(logger_name or f"{__name__}.{type(self).__name__}")

    @AgentCallableSet.tool(
        description=(
            "Fetch current weather observations. Omit location only for the assistant server's local weather. "
            "Use a canonical geographic place name when the user named one."
        )
    )
    async def get_current_weather(
        self,
        location: Annotated[str | None, "Optional canonical geographic place name, for example Gdańsk."] = None,
        focus: Annotated[str | None, "Optional focus. Use temperature only when the user asked about temperature."] = None,
    ) -> dict[str, Any]:
        resolved_location = self._resolved_location(location)
        if resolved_location is None:
            return _tool_clarification("Dla jakiej lokalizacji mam sprawdzić pogodę?")
        normalized_focus = _normalize_tool_focus(focus)
        if focus is not None and normalized_focus is None:
            return _tool_invalid("focus", "Use focus='temperature' or omit focus.")
        request = WeatherNowRequest(location=resolved_location, focus=normalized_focus)
        if self._local_weather_cache is not None:
            cached_weather = self._local_weather_cache.current_weather(request)
            if cached_weather is not None:
                data = weather_to_json(cached_weather)
                return {
                    "status": "ok",
                    "kind": "current",
                    "formatted_text": format_current_weather(cached_weather, focus=normalized_focus),
                    "weather": data,
                    "entities": [f"weather.{cached_weather.location}"],
                    "source": "local_cache",
                }
        for provider in self._providers:
            try:
                weather = await provider.get_weather_now(request)
            except Exception:
                self._logger.debug("weather provider failed provider=%s request=%s", provider.name, request, exc_info=True)
                continue
            if weather is not None:
                data = weather_to_json(weather)
                return {
                    "status": "ok",
                    "kind": "current",
                    "formatted_text": format_current_weather(weather, focus=normalized_focus),
                    "weather": data,
                    "entities": [f"weather.{weather.location}"],
                }
        return _tool_not_found(resolved_location)

    @AgentCallableSet.tool(
        description=(
            "Fetch a weather forecast. Choose horizon and granularity from the user phrase. "
            "Omit location only for the assistant server's local forecast."
        )
    )
    async def get_weather_forecast(
        self,
        horizon: Annotated[
            str,
            "today, tomorrow, weekend, next_weekend, monday, tuesday, wednesday, thursday, friday, saturday, or sunday.",
        ],
        location: Annotated[str | None, "Optional canonical geographic place name, for example Gdańsk."] = None,
        granularity: Annotated[str, "daily or hourly. Use hourly for later today, tonight, evening, rain timing, or yes/no event questions."] = "daily",
        focus: Annotated[str | None, "Optional focus. Use temperature only when the user asked about temperature."] = None,
    ) -> dict[str, Any]:
        resolved_location = self._resolved_location(location)
        if resolved_location is None:
            return _tool_clarification("Dla jakiej lokalizacji mam sprawdzić pogodę?")
        normalized_horizon = _normalize_tool_horizon(horizon)
        if normalized_horizon is None:
            return _tool_invalid("horizon", "Use a supported forecast horizon.")
        normalized_granularity = _normalize_tool_granularity(granularity)
        if normalized_granularity is None:
            return _tool_invalid("granularity", "Use granularity='daily' or granularity='hourly'.")
        normalized_focus = _normalize_tool_focus(focus)
        if focus is not None and normalized_focus is None:
            return _tool_invalid("focus", "Use focus='temperature' or omit focus.")

        request = WeatherForecastRequest(
            location=resolved_location,
            horizon=normalized_horizon,
            granularity=normalized_granularity,
        )
        if self._local_weather_cache is not None:
            cached_forecast = self._local_weather_cache.weather_forecast(request)
            if cached_forecast is not None:
                data = weather_to_json(cached_forecast)
                return {
                    "status": "ok",
                    "kind": "forecast",
                    "formatted_text": format_forecast(cached_forecast),
                    "forecast": data,
                    "focus": normalized_focus,
                    "entities": [f"weather.{cached_forecast.location}"],
                    "source": "local_cache",
                }
        for provider in self._providers:
            try:
                forecast = await provider.get_weather_forecast(request)
            except Exception:
                self._logger.debug("weather provider failed provider=%s request=%s", provider.name, request, exc_info=True)
                continue
            if forecast is not None:
                data = weather_to_json(forecast)
                return {
                    "status": "ok",
                    "kind": "forecast",
                    "formatted_text": format_forecast(forecast),
                    "forecast": data,
                    "focus": normalized_focus,
                    "entities": [f"weather.{forecast.location}"],
                }
        return _tool_not_found(resolved_location)

    @AgentCallableSet.tool(
        description=(
            "Fetch configured-location astronomy facts: sunrise, sunset, moonrise, moonset, moon phase, "
            "day length, and comparison with the year's shortest and longest days. Use for sun, moon, or day-length questions."
        )
    )
    async def get_astronomy_facts(self) -> dict[str, Any]:
        if self._astronomy_refresher is None:
            return _tool_failed("Astronomy data source is not configured.")
        snapshot = await self._astronomy_refresher.ensure_fresh()
        if snapshot is None:
            return _tool_failed("No astronomy data is available for the configured location.")
        data = astronomy_to_json(snapshot)
        return {
            "status": "ok",
            "kind": "astronomy",
            "formatted_text": format_astronomy(snapshot),
            "astronomy": data,
            "entities": [f"astronomy.{snapshot.location}"],
        }

    def _resolved_location(self, location: str | None) -> str | None:
        if isinstance(location, str) and location.strip():
            return location.strip()
        if self._default_location:
            return self._default_location
        return None


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


def _result_for_current_weather(weather: CurrentWeather, *, focus: str | None) -> dict[str, Any]:
    data = weather_to_json(weather)
    return _ok_result(
        text=format_current_weather(weather, focus=focus),
        data={"kind": "current", "weather": data},
        entities=[f"weather.{weather.location}"],
        final_reply_mode="verbatim",
    )


def _result_for_forecast(forecast: WeatherForecast) -> dict[str, Any]:
    data = weather_to_json(forecast)
    return _ok_result(
        text=format_forecast(forecast),
        data={"kind": "forecast", "forecast": data},
        entities=[f"weather.{forecast.location}"],
        final_reply_mode="verbatim",
    )


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


def _failed_result(text: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "text": text,
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [],
    }


def _create_astronomy_refresher(
    *,
    location: str | None,
    data_store: JsonFileStore | None,
    api_key: str | None,
    session: ClientSession | None,
) -> WeatherAstronomyRefresher | None:
    if api_key is None:
        return None
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("agent.domain_agents.weather.ipgeolocation_api_key must be a non-empty string when provided")
    if data_store is None:
        raise ValueError("weather astronomy support requires a JsonFileStore")
    client = IPGeolocationAstronomyClient(api_key=api_key.strip(), session=session)
    store = WeatherAstronomyStore(data_store)
    return WeatherAstronomyRefresher(location=location, store=store, client=client)


def _tool_failed(text: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "text": text,
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [],
    }


def _parse_domain_reply(content: str) -> dict[str, Any]:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("Weather DSA reply must be valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("Weather DSA reply must be a JSON object")
    status = raw.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("Weather DSA reply status must be a non-empty string")
    text = raw.get("text")
    if not isinstance(text, str):
        raise ValueError("Weather DSA reply text must be a string")
    needs_clarification = raw.get("needs_clarification", status == "needs_clarification")
    if not isinstance(needs_clarification, bool):
        raise ValueError("Weather DSA reply needs_clarification must be a boolean")
    clarification_question = raw.get("clarification_question")
    if clarification_question is not None and not isinstance(clarification_question, str):
        raise ValueError("Weather DSA reply clarification_question must be a string or null")
    entities = raw.get("entities", [])
    if not isinstance(entities, list) or any(not isinstance(entity, str) for entity in entities):
        raise ValueError("Weather DSA reply entities must be a list of strings")

    parsed = dict(raw)
    parsed["text"] = _sanitize_reply_text(text)
    parsed["needs_clarification"] = needs_clarification
    parsed["clarification_question"] = clarification_question
    parsed["entities"] = entities
    parsed.setdefault("final_reply_mode", "verbatim")
    return parsed


def _task_intent(task: DomainTask) -> str:
    command = task.get("command")
    if not isinstance(command, dict):
        return "unknown"
    intent = command.get("intent")
    if isinstance(intent, str) and intent:
        return intent
    tool = command.get("tool")
    return tool if isinstance(tool, str) and tool else "unknown"


def _token_total(prompt_tokens: int | None, completion_tokens: int | None) -> int | None:
    if prompt_tokens is None or completion_tokens is None:
        return None
    return prompt_tokens + completion_tokens


def _abbreviate(text: str, limit: int = 300) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _sanitize_reply_text(text: str) -> str:
    sanitized = re.sub(r"(?<=\d)\s*(?:°\s*C|℃|°)", " stopni", text, flags=re.IGNORECASE)
    sanitized = re.sub(r"(?:°\s*C|℃)", "stopni", sanitized, flags=re.IGNORECASE)
    sanitized = sanitized.replace("°", "")
    sanitized = re.sub(r"\b-?\d+(?:[\.,]\d+)?\b", _number_match_to_words, sanitized)
    return " ".join(sanitized.split())


def _number_match_to_words(match: re.Match[str]) -> str:
    text = match.group(0)
    if "." in text or "," in text:
        return polish_decimal(float(text.replace(",", ".")))
    return polish_cardinal(int(text))


def _minimal_task_for_agent_loop(task: DomainTask) -> DomainTask:
    command = task.get("command")
    query = command.get("query") if isinstance(command, dict) else None
    return {
        "id": task.get("id"),
        "domain": task.get("domain"),
        "command": {"query": query} if isinstance(query, str) else {},
        "depends_on": task.get("depends_on", []),
        "status": task.get("status", "ready"),
        "clarification_question": task.get("clarification_question"),
    }


def _ascii_key(value: str) -> str:
    return ascii_fold(normalize_text(value)).strip(" ?.!").strip()


def _normalize_tool_horizon(value: str) -> str | None:
    normalized = _ascii_key(value).replace("next weekeend", "next weekend")
    if normalized in {"today", "dzis", "dzisiaj"}:
        return "today"
    if normalized in {"tomorrow", "jutro"}:
        return "tomorrow"
    if normalized in {"weekend", "wekend", "weekeend"}:
        return "weekend"
    if normalized in {"next weekend", "nastepny weekend", "przyszly weekend", "kolejny weekend"}:
        return "next_weekend"
    return {
        "poniedzialek": "monday",
        "poniedzialku": "monday",
        "monday": "monday",
        "wtorek": "tuesday",
        "wtorku": "tuesday",
        "tuesday": "tuesday",
        "sroda": "wednesday",
        "srode": "wednesday",
        "wednesday": "wednesday",
        "czwartek": "thursday",
        "czwartku": "thursday",
        "thursday": "thursday",
        "piatek": "friday",
        "piatku": "friday",
        "friday": "friday",
        "sobota": "saturday",
        "sobote": "saturday",
        "saturday": "saturday",
        "niedziela": "sunday",
        "niedziele": "sunday",
        "sunday": "sunday",
    }.get(normalized)


def _normalize_tool_granularity(value: str) -> str | None:
    normalized = _ascii_key(value)
    if normalized in {"daily", "dzienna", "dziennie", "dzien"}:
        return "daily"
    if normalized in {"hourly", "godzinowa", "godzinowo", "godzinna"}:
        return "hourly"
    return None


def _normalize_tool_focus(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = _ascii_key(value)
    if normalized in {"temperature", "temperatura", "temp", "stopnie"}:
        return "temperature"
    return None


def _tool_clarification(question: str) -> dict[str, Any]:
    return {"status": "needs_clarification", "message": question}


def _tool_invalid(field: str, message: str) -> dict[str, Any]:
    return {"status": "invalid_request", "field": field, "message": message}


def _tool_not_found(location: str) -> dict[str, Any]:
    return {"status": "not_found", "message": f"Nie znalazłem danych pogodowych dla lokalizacji: {location}."}
