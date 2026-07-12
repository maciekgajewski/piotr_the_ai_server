# Agent Loop

## Document status

- **Authority:** Component reference
- **Audience:** Agents changing `ai_server.agent_loop`
- **Read when:** Modifying the Ollama agent loop, fallback policy, or tool conventions

`ai_server.agent_loop` is an Ollama `/api/chat` loop with tool calling.
It keeps loop-specific fallback/backoff policy in `AgentLoopOllamaConnection`
and uses `ai_server.ollama_client.OllamaClient` for the HTTP transport.

## Tool conventions

Inherit from `AgentCallableSet` and decorate async instance methods. The convention
form uses the method name as the tool name and the method docstring as the tool
description.

```python
from ai_server.agent_loop import AgentCallableSet


class MathTools(AgentCallableSet):
    @AgentCallableSet.tool
    async def add(self, a: int, b: int) -> int:
        """Add two numbers."""
        return a + b
```

The explicit form lets you override name and description. Use
`typing.Annotated` for parameter descriptions in the generated JSON schema.

```python
from typing import Annotated

from ai_server.agent_loop import AgentCallableSet


class MathTools(AgentCallableSet):
    @AgentCallableSet.tool(name="multiply_numbers", description="Multiply two numbers.")
    async def multiply(
        self,
        a: Annotated[int, "The first number"],
        b: Annotated[int, "The second number"],
    ) -> int:
        return a * b
```

Tool parameters and return values must be JSON-serializable. Supported
annotations include `str`, `int`, `float`, `bool`, `Enum`, `dict[str, T]`,
`list[T]`, nested combinations of those, and `None` for return values.
Parameters without defaults are required; parameters with defaults are optional.

## AgentLoop

```python
from ai_server.agent_loop import AgentLoop, AgentLoopConfig


tools = MathTools()
loop = AgentLoop(
    config=AgentLoopConfig(model="qwen3:4b"),
    system_prompt="You are a concise assistant. Use tools when useful.",
    tools=tools,
)

reply = await loop.send_user_message("What is (2 + 3) * 4?")
print(reply.reply_text)
print(reply.end_conversation)
print(loop.eval_count)
await loop.close()
```

`send_user_message()` returns `AgentReply(reply_text, end_conversation)`.
`end_conversation` is `True` only after an unrecoverable error, in which case
the reply text is `Model się zesrał`.
