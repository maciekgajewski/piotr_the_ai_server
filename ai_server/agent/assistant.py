from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Mapping

from aiohttp import ClientSession

from ai_server.ai_tools.interfaces import Tool
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import TextMessage
from ai_server.ollama import OLLAMA_BASE_URL, OllamaClient, OllamaError


USER_PROMPT_TEMPLATE = """
Available tools:
{tools}

Return schema:
{{"tool": "...","confidence": 0.0}}

User input: {user_input}
"""

SYSTEM_PROMPT = """
You are an intent router.
Return only compact valid JSON.
No reasoning. No explanation. No markdown.
"""

GENERATION_OPTIONS = {
    "num_predict": 32,
    "temperature": 0,
    "num_ctx": 1024,
}
GENERATION_FAILURE_MESSAGE = "Przepraszam, nie mogę teraz odpowiedzieć."


@dataclass(frozen=True)
class ToolRoute:
    tool: str
    confidence: float


class AssistantAgent:
    def __init__(
        self,
        intent_router_model: str,
        tools: Mapping[str, Tool],
        base_url: str = OLLAMA_BASE_URL,
        session: ClientSession | None = None,
        ollama_client: OllamaClient | None = None,
        owns_ollama_client: bool = True,
    ) -> None:
        self._intent_router_model = intent_router_model
        self._tools = dict(tools)
        self._user_prompt_template = _build_user_prompt_template(self._tools)
        self._ollama = ollama_client or OllamaClient(base_url=base_url, session=session)
        self._owns_ollama = owns_ollama_client
        self._logger = logging.getLogger(f"{__name__}.AssistantAgent[{intent_router_model}]")

    async def preload(self) -> None:
        try:
            await self._ollama.chat(
                {
                    "model": self._intent_router_model,
                    "think": False,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "1h",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Return JSON: {\"ok\":true}",
                        },
                    ],
                }
            )
        except Exception as exc:
            raise OllamaError(f"failed to preload Ollama model {self._intent_router_model}") from exc

    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        logger = logging.getLogger(f"{__name__}.AssistantAgent[{conversation.conversation_id}]")
        async for message in endpoint.messages():
            started_at = time.perf_counter()

            try:
                route = await self._route_message(message.text)
            except Exception:
                elapsed_ms = _elapsed_ms(started_at)
                logger.exception(
                    "generation failed request_len=%s duration_ms=%s",
                    len(message.text),
                    elapsed_ms,
                )
                await endpoint.send_message(TextMessage(text=GENERATION_FAILURE_MESSAGE))
                continue

            tool = self._tools.get(route.tool)
            if tool is None:
                elapsed_ms = _elapsed_ms(started_at)
                logger.error(
                    "router selected unknown tool=%s confidence=%s request_len=%s duration_ms=%s",
                    route.tool,
                    route.confidence,
                    len(message.text),
                    elapsed_ms,
                )
                await endpoint.send_message(TextMessage(text=GENERATION_FAILURE_MESSAGE))
                continue

            elapsed_ms = _elapsed_ms(started_at)
            logger.info(
                "router selected tool=%s confidence=%s request_len=%s duration_ms=%s",
                route.tool,
                route.confidence,
                len(message.text),
                elapsed_ms,
            )
            await tool.run(conversation, endpoint, message)

    async def close(self) -> None:
        try:
            for tool in self._tools.values():
                await tool.close()
        finally:
            if self._owns_ollama:
                await self._ollama.close()

    async def _route_message(self, user_input: str) -> ToolRoute:
        response = await self._ollama.chat(
            {
                "model": self._intent_router_model,
                "raw": False,
                "think": False,
                "format": "json",
                "stream": False,
                "keep_alive": "1h",
                "options": GENERATION_OPTIONS,
                "messages": [
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": self._user_prompt_template.format(user_input=user_input),
                    },
                ],
            }
        )

        reply = response["message"]
        assert reply["role"] == "assistant"
        content = reply["content"]
        if not isinstance(content, str):
            raise OllamaError("Ollama response missing string response field")

        return _parse_tool_route(content)


def _elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def _build_user_prompt_template(tools: Mapping[str, Tool]) -> str:
    tool_lines = []
    for tool in tools.values():
        tool_lines.append(f"- {tool.name}: {tool.description}")

    return USER_PROMPT_TEMPLATE.replace("{tools}", "\n".join(tool_lines))


def _parse_tool_route(content: str) -> ToolRoute:
    try:
        raw_route = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("router response must be valid JSON") from exc

    if not isinstance(raw_route, dict):
        raise ValueError("router response must be a JSON object")

    tool = raw_route.get("tool")
    if not isinstance(tool, str) or not tool:
        raise ValueError("router response tool must be a non-empty string")

    confidence = raw_route.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("router response confidence must be a number")

    return ToolRoute(tool=tool, confidence=float(confidence))
