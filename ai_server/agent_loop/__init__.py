"""Standalone Ollama agent loop utilities.

Use ``ToolClass`` as a base class for async tool collections. The short form
uses the method name as the tool name and the method docstring as its
description::

    class MathTools(ToolClass):
        @ToolClass.tool
        async def add(self, a: int, b: int) -> int:
            "Add two numbers."
            return a + b

The explicit form overrides the convention. Parameter descriptions use
``typing.Annotated``::

    class MathTools(ToolClass):
        @ToolClass.tool(name="multiply_numbers", description="Multiply two numbers.")
        async def multiply(
            self,
            a: Annotated[int, "The first number"],
            b: Annotated[int, "The second number"],
        ) -> int:
            return a * b
"""

from ai_server.agent_loop.agent_loop import AgentLoop, MODEL_FAILURE_REPLY
from ai_server.agent_loop.config import AgentLoopConfig
from ai_server.agent_loop.messages import AgentReply
from ai_server.agent_loop.tool_class import ToolClass


__all__ = [
    "AgentLoop",
    "AgentLoopConfig",
    "AgentReply",
    "MODEL_FAILURE_REPLY",
    "ToolClass",
]
