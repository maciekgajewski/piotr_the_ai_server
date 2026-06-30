from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from ai_server.domain_agents.weather.interfaces import CurrentWeather, WeatherForecast, WeatherForecastRequest, WeatherNowRequest, WeatherProvider
from ai_server.utils.text import ascii_fold, normalize_text


LOCAL_WEATHER_REFRESH_SECONDS = 5 * 60
LOCAL_FORECAST_REQUESTS: tuple[tuple[str, str], ...] = (
    ("today", "daily"),
    ("today", "hourly"),
    ("tomorrow", "daily"),
    ("tomorrow", "hourly"),
    ("weekend", "daily"),
    ("next_weekend", "daily"),
)


class WeatherLocalCache:
    def __init__(
        self,
        *,
        location: str | None,
        providers: list[WeatherProvider],
        refresh_interval_seconds: float = LOCAL_WEATHER_REFRESH_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._location = location
        self._providers = providers
        self._refresh_interval_seconds = refresh_interval_seconds
        self._sleep = sleep
        self._current: CurrentWeather | None = None
        self._forecasts: dict[tuple[str, str], WeatherForecast] = {}
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._refresh_lock = asyncio.Lock()
        self._logger = logging.getLogger(f"{__name__}.WeatherLocalCache[{location or 'no-location'}]")

    async def start(self) -> None:
        self._closed = False
        if not self._location:
            self._logger.warning("local weather cache disabled because server location is not configured")
            return
        if self._task is None or self._task.done():
            self._logger.info(
                "starting local weather cache location=%r refresh_interval_seconds=%s",
                self._location,
                self._refresh_interval_seconds,
            )
            await self.refresh_once()
            self._task = asyncio.create_task(self._refresh_loop())
            self._logger.info("local weather cache started")

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._logger.info("stopping local weather cache")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._logger.info("local weather cache stopped")

    def is_local_location(self, location: str) -> bool:
        return self._location is not None and _location_key(location) == _location_key(self._location)

    def current_weather(self, request: WeatherNowRequest) -> CurrentWeather | None:
        if not self.is_local_location(request.location):
            return None
        return self._current

    def weather_forecast(self, request: WeatherForecastRequest) -> WeatherForecast | None:
        if not self.is_local_location(request.location):
            return None
        return self._forecasts.get((request.horizon, request.granularity))

    async def refresh_once(self) -> None:
        if not self._location:
            return
        async with self._refresh_lock:
            current = await self._fetch_current()
            forecasts: dict[tuple[str, str], WeatherForecast] = {}
            for horizon, granularity in LOCAL_FORECAST_REQUESTS:
                forecast = await self._fetch_forecast(horizon=horizon, granularity=granularity)
                if forecast is not None:
                    forecasts[(horizon, granularity)] = forecast
            if current is not None:
                self._current = current
            if forecasts:
                self._forecasts.update(forecasts)
            self._logger.debug(
                "refreshed local weather cache location=%r current=%s forecasts=%s",
                self._location,
                current is not None,
                sorted(f"{horizon}:{granularity}" for horizon, granularity in forecasts),
            )

    async def _refresh_loop(self) -> None:
        while not self._closed:
            await self._sleep(self._refresh_interval_seconds)
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("local weather cache refresh failed")

    async def _fetch_current(self) -> CurrentWeather | None:
        if not self._location:
            return None
        request = WeatherNowRequest(location=self._location)
        for provider in self._providers:
            try:
                weather = await provider.get_weather_now(request)
            except Exception:
                self._logger.debug("local weather provider failed provider=%s request=%s", provider.name, request, exc_info=True)
                continue
            if weather is not None:
                return weather
        return None

    async def _fetch_forecast(self, *, horizon: str, granularity: str) -> WeatherForecast | None:
        if not self._location:
            return None
        request = WeatherForecastRequest(location=self._location, horizon=horizon, granularity=granularity)
        for provider in self._providers:
            try:
                forecast = await provider.get_weather_forecast(request)
            except Exception:
                self._logger.debug("local weather provider failed provider=%s request=%s", provider.name, request, exc_info=True)
                continue
            if forecast is not None:
                return forecast
        return None


def _location_key(value: str) -> str:
    return ascii_fold(normalize_text(value)).strip()
