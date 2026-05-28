import logging

from ai_server.ai_tools.calculator.calculator import CalculatorTool
from ai_server.ai_tools.clarify.clarify import ClarifyTool
from ai_server.ai_tools.home_assistant.home_assistant import HomeAssistantTool
from ai_server.ai_tools.interfaces import BaseTool, Tool
from ai_server.ai_tools.time.time import TimeTool
from ai_server.ai_tools.weather.weather import WeatherTool
from ai_server.ai_tools.web_search.web_search import WebSearchTool
from ai_server.ai_tools.wikipedia.wikipedia import WikipediaTool
from ai_server.config import AgentConfig


TOOL_CLASSES = (
    CalculatorTool,
    ClarifyTool,
    HomeAssistantTool,
    TimeTool,
    WeatherTool,
    WebSearchTool,
    WikipediaTool,
)


def create_tools(config: AgentConfig) -> dict[str, Tool]:
    logger = logging.getLogger(f"{__name__}.factory")
    tools: dict[str, Tool] = {}
    for tool_class in TOOL_CLASSES:
        tool = tool_class(config)
        if tool.name in tools:
            raise ValueError(f"duplicate AI tool name: {tool.name}")

        tools[tool.name] = tool
        logger.info("Loaded AI tool name=%s class=%s", tool.name, tool_class.__name__)

    return tools


__all__ = ["BaseTool", "Tool", "create_tools"]
