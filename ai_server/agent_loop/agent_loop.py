from __future__ import annotations

import json
import logging
import time
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from ai_server.agent_loop.config import AgentLoopConfig
from ai_server.agent_loop.interfaces import HttpSession
from ai_server.agent_loop.messages import AgentReply
from ai_server.agent_loop.agent_callable_set import AgentCallableSet


MODEL_FAILURE_REPLY = "Model się zesrał"


class AgentLoop:
    def __init__(
        self,
        config: AgentLoopConfig,
        system_prompt: str,
        tools: AgentCallableSet,
        session: HttpSession | None = None,
    ) -> None:
        self._config = config
        self._system_prompt = system_prompt
        self._tools = tools
        self._tool_schemas = tools.get_tool_schemas()
        self._messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        self._session = session
        self._owns_session = session is None
        self._eval_count = 0
        self._turn_number = 0
        self._instance_id = f"{id(self):x}"
        self._logger = logging.getLogger(f"{__name__}.AgentLoop[{self._instance_id}:{config.model}]")

    @property
    def eval_count(self) -> int:
        return self._eval_count

    async def __aenter__(self) -> "AgentLoop":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def send_user_message(self, message: str) -> AgentReply:
        self._messages.append({"role": "user", "content": message})
        tool_calls_this_message = 0
        repair_attempts = 0

        try:
            while True:
                started_at = time.perf_counter()
                response = await self._send_chat_request()
                elapsed_ms = round((time.perf_counter() - started_at) * 1000)
                assistant_message = _parse_assistant_message(response)
                self._messages.append(assistant_message)

                self._record_eval_count(response, elapsed_ms)
                tool_calls = _extract_tool_calls(assistant_message)
                if not tool_calls:
                    content = assistant_message.get("content", "")
                    if not isinstance(content, str):
                        raise ValueError("assistant message content must be a string")
                    return AgentReply(reply_text=content, end_conversation=False)

                tool_calls_this_message += len(tool_calls)
                if tool_calls_this_message > self._config.max_tool_calls_per_message:
                    raise RuntimeError("model exceeded max_tool_calls_per_message")

                for tool_call in tool_calls:
                    try:
                        tool_name, arguments = _parse_tool_call(tool_call)
                    except ValueError as exc:
                        repair_attempts += 1
                        self._logger.warning(
                            "invalid tool call repair_attempt=%s max_repair_attempts=%s error=%s",
                            repair_attempts,
                            self._config.max_tool_repair_attempts,
                            exc,
                        )
                        if repair_attempts > self._config.max_tool_repair_attempts:
                            raise RuntimeError("model exceeded max_tool_repair_attempts") from exc
                        self._messages.append(_tool_error_message("invalid_tool_call", str(exc)))
                        continue

                    try:
                        result = await self._tools.call_tool(tool_name, arguments)
                    except ValueError as exc:
                        repair_attempts += 1
                        self._logger.warning(
                            "invalid tool call tool=%s repair_attempt=%s max_repair_attempts=%s error=%s",
                            tool_name,
                            repair_attempts,
                            self._config.max_tool_repair_attempts,
                            exc,
                        )
                        if repair_attempts > self._config.max_tool_repair_attempts:
                            raise RuntimeError("model exceeded max_tool_repair_attempts") from exc
                        self._messages.append(_tool_error_message(tool_name, str(exc)))
                        continue
                    except Exception as exc:
                        self._logger.exception("tool execution failed tool=%s", tool_name)
                        self._messages.append(_tool_error_message(tool_name, str(exc)))
                        continue

                    serialized_result = json.dumps(result, ensure_ascii=False)
                    self._logger.debug("tool call completed tool=%s result=%s", tool_name, serialized_result)
                    self._messages.append({"role": "tool", "tool_name": tool_name, "content": serialized_result})
        except Exception:
            self._logger.exception("unrecoverable agent loop error")
            return AgentReply(reply_text=MODEL_FAILURE_REPLY, end_conversation=True)

    async def _send_chat_request(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": self._messages,
            "stream": False,
        }
        if self._tool_schemas:
            payload["tools"] = self._tool_schemas
        if self._config.options:
            payload["options"] = self._config.options
        if self._config.keep_alive is not None:
            payload["keep_alive"] = self._config.keep_alive
        if self._config.think is not None:
            payload["think"] = self._config.think

        self._turn_number += 1
        self._logger.debug("turn=%s request=%s", self._turn_number, payload)
        response = await self._post_chat(payload)
        if response.get("done") is not True:
            raise ValueError("Ollama chat response did not finish")
        return response

    async def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._session
        if session is None:
            timeout = ClientTimeout(total=self._config.request_timeout_seconds)
            session = ClientSession(timeout=timeout)
            self._session = session

        url = f"{self._config.ollama_url.rstrip('/')}/api/chat"
        async with session.post(url, json=payload) as response:
            if response.status >= 400:
                raise RuntimeError(f"Ollama chat failed with status {response.status}")
            body = await response.json()
        if not isinstance(body, dict):
            raise ValueError("Ollama chat response must be a JSON object")
        return body

    def _record_eval_count(self, response: dict[str, Any], elapsed_ms: int) -> None:
        eval_count = response.get("eval_count", 0)
        if isinstance(eval_count, bool) or not isinstance(eval_count, int):
            raise ValueError("Ollama eval_count must be an integer")
        self._eval_count += eval_count
        self._logger.debug(
            "turn=%s response eval_count=%s total_eval_count=%s duration_ms=%s response=%s",
            self._turn_number,
            eval_count,
            self._eval_count,
            elapsed_ms,
            response,
        )


def _parse_assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    message = response.get("message")
    if not isinstance(message, dict):
        raise ValueError("Ollama chat response must contain message object")
    role = message.get("role", "assistant")
    if role != "assistant":
        raise ValueError("Ollama chat message role must be assistant")

    assistant_message = dict(message)
    assistant_message["role"] = "assistant"
    return assistant_message


def _extract_tool_calls(assistant_message: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls = assistant_message.get("tool_calls", [])
    if tool_calls is None:
        return []
    if not isinstance(tool_calls, list):
        raise ValueError("assistant tool_calls must be a list")
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            raise ValueError("assistant tool_call must be an object")
    return tool_calls


def _parse_tool_call(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = tool_call.get("function")
    if not isinstance(function, dict):
        raise ValueError("tool call function must be an object")
    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("tool call function.name must be a non-empty string")
    arguments = function.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("tool call function.arguments must be an object")
    return name, arguments


def _tool_error_message(tool_name: str, message: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_name": tool_name,
        "content": json.dumps(
            {
                "error": "tool_call_failed",
                "message": message,
            },
            ensure_ascii=False,
        ),
    }
