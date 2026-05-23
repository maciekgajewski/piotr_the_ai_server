from __future__ import annotations

from ai_server.ai_tools.interfaces import BaseTool


class WeatherTool(BaseTool):
    name = "weather"
    description = "A tool for providing current weather information. Use this for any weather-related queries."
