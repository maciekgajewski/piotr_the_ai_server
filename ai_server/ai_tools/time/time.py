from __future__ import annotations

from ai_server.ai_tools.interfaces import BaseTool


class TimeTool(BaseTool):
    name = "time"
    description = "A tool for providing the current time, date or day of week. Use this for any time-related queries."
