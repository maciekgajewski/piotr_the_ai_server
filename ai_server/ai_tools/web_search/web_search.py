from __future__ import annotations

from ai_server.ai_tools.interfaces import BaseTool


class WebSearchTool(BaseTool):
    name = "web_search"
    description = (
        "A tool for performing web searches. Use this for any general knowledge queries or when the user "
        "explicitly asks you to search the web."
    )
