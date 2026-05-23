from __future__ import annotations

from ai_server.ai_tools.interfaces import BaseTool


class WikipediaTool(BaseTool):
    name = "wikipedia"
    description = (
        "A tool for retrieving information from Wikipedia. Use this for any queries about historical events, "
        "famous people, scientific concepts, etc."
    )
