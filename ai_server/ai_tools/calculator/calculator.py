from __future__ import annotations

from ai_server.ai_tools.interfaces import BaseTool


class CalculatorTool(BaseTool):
    name = "calculator"
    description = "A tool for performing mathematical calculations. Use this for any math-related queries."
