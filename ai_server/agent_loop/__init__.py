"""Standalone Ollama agent loop utilities.

Use ``AgentCallableSet`` as a base class for async tool collections. The short form
uses the method name as the tool name and the method docstring as its
description::

    class MathTools(AgentCallableSet):
        @AgentCallableSet.tool
        async def add(self, a: int, b: int) -> int:
            "Add two numbers."
            return a + b

The explicit form overrides the convention. Parameter descriptions use
``typing.Annotated``::

    class MathTools(AgentCallableSet):
        @AgentCallableSet.tool(name="multiply_numbers", description="Multiply two numbers.")
        async def multiply(
            self,
            a: Annotated[int, "The first number"],
            b: Annotated[int, "The second number"],
        ) -> int:
            return a * b
"""

from ai_server.agent_loop.agent_loop import AgentLoop, MODEL_FAILURE_REPLY
from ai_server.agent_loop.agent_callable_set import AgentCallableSet
from ai_server.agent_loop.config import AgentLoopConfig
from ai_server.agent_loop.messages import AgentReply
from ai_server.agent_loop.ollama_connection import AgentLoopOllamaConnection


__all__ = [
    "AgentLoop",
    "AgentLoopConfig",
    "AgentLoopOllamaConnection",
    "AgentReply",
    "AgentCallableSet",
    "MODEL_FAILURE_REPLY",
]
