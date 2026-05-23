from __future__ import annotations

from ai_server.ai_tools.interfaces import BaseTool


class HomeAssistantTool(BaseTool):
    name = "home_assistant"
    description = (
        "A tool for controlling smart home devices. Use this for any queries related to smart home control, "
        "air conditioning, lighting, etc."
    )
