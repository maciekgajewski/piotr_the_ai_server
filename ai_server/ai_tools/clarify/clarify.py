from __future__ import annotations

from ai_server.ai_tools.interfaces import BaseTool


class ClarifyTool(BaseTool):
    name = "clarify"
    description = (
        "A tool for asking the user for clarification. Use this when the user's message is ambiguous and you "
        "need more information to determine the correct tool."
    )
