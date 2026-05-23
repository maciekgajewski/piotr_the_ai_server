from __future__ import annotations

import logging
import time
from typing import Any

from aiohttp import ClientSession

from ai_server.interfaces import CommunicationEndpoint
from ai_server.messages import UserMessage
from ai_server.streaming import receive_user_message, send_user_message


OLLAMA_BASE_URL = "http://127.0.0.1:11434"

ROUTER_PROMPT = """
You are an assistant for routing user messages to the correct tool. Return ONLY valid JSON! You receive a user message and must determine which tool should handle it. You have access to the following tools:
- calculator: A tool for performing mathematical calculations. Use this for any math-related queries.
- weather: A tool for providing current weather information. Use this for any weather-related queries.
- time: A tool for providing the current time, date or day of week. Use this for any time-related queries.
- home_assistant: A tool for controlling smart home devices. Use this for any queries related to smart home control, air conditioning, lighting, etc.
- web_search: A tool for performing web searches. Use this for any general knowledge queries or when the user explicitly asks you to search the web.
- wikipedia: A tool for retrieving information from Wikipedia. Use this for any queries about historical events, famous people, scientific concepts, etc.
- clarify: A tool for asking the user for clarification. Use this when the user's message is ambiguous and you need more information to determine the correct tool.

Reply ONLY with valid JSON in the following format:
{{
    "tool": "...",
    "confidence": 0.0
}}

User input: {user_input}
"""

GENERATION_OPTIONS = {
    "num_predict": 48,
    "temperature": 0,
    "stop": ["\n"],
}
GENERATION_FAILURE_MESSAGE = "Przepraszam, nie mogę teraz odpowiedzieć."


class OllamaError(Exception):
    """Raised when Ollama cannot generate a response."""


class AssistantAgent:
    def __init__(
        self,
        model: str,
        base_url: str = OLLAMA_BASE_URL,
        session: ClientSession | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._session = session or ClientSession()
        self._owns_session = session is None
        self._logger = logging.getLogger(f"{__name__}.AssistantAgent[{model}]")

    async def preload(self) -> None:
        try:
            await self._post_generate(
                {
                    "model": self._model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": -1,
                }
            )
        except Exception as exc:
            raise OllamaError(f"failed to preload Ollama model {self._model}") from exc

    async def run(self, endpoint: CommunicationEndpoint, session_id: str) -> None:
        logger = logging.getLogger(f"{__name__}.AssistantAgent[{session_id}]")
        while True:
            message = await receive_user_message(endpoint)
            started_at = time.perf_counter()

            try:
                reply = await self._generate_reply(message.text)
            except Exception:
                elapsed_ms = _elapsed_ms(started_at)
                logger.exception(
                    "generation failed request_len=%s duration_ms=%s",
                    len(message.text),
                    elapsed_ms,
                )
                await send_user_message(endpoint, UserMessage(text=GENERATION_FAILURE_MESSAGE))
                continue

            elapsed_ms = _elapsed_ms(started_at)
            logger.debug(
                "request_len=%s reply_len=%s duration_ms=%s",
                len(message.text),
                len(reply),
                elapsed_ms,
            )
            await send_user_message(endpoint, UserMessage(text=reply))

    async def close(self) -> None:
        if self._owns_session:
            await self._session.close()

    async def _generate_reply(self, user_input: str) -> str:
        response = await self._post_generate(
            {
                "model": self._model,
                "raw": False,
                "think" : False,
                "format" : "json",
                "keep_alive": "1h",
                "prompt": ROUTER_PROMPT.format(user_input=user_input),
                "stream": False,
                "options": GENERATION_OPTIONS,
            }
        )

        reply = response.get("response")
        if not isinstance(reply, str):
            raise OllamaError("Ollama response missing string response field")

        return _strip_thinking(reply)

    async def _post_generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._logger.debug("Ollama request: %s", payload)
        async with self._session.post(f"{self._base_url}/api/generate", json=payload) as response:
            if response.status >= 400:
                raise OllamaError(f"Ollama generate failed with status {response.status}")

            body = await response.json()
            if not isinstance(body, dict):
                raise OllamaError("Ollama response must be a JSON object")

            self._logger.debug("Ollama response: %s", body)
            return body


def _elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def _strip_thinking(reply: str) -> str:
    end_tag = "</think>"
    if end_tag not in reply:
        return reply.strip()

    return reply.split(end_tag, maxsplit=1)[1].strip()
