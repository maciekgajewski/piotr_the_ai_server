from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from ai_server.agent_loop.config import AgentLoopConfig
from ai_server.agent_loop.interfaces import HttpSession
from ai_server.agent_loop.ollama_connection import AgentLoopOllamaConnection
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
        ollama_connection: AgentLoopOllamaConnection | None = None,
        context_message_observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if session is not None and ollama_connection is not None:
            raise ValueError("session and ollama_connection cannot both be provided")
        self._config = config
        self._system_prompt = system_prompt
        self._tools = tools
        self._tool_schemas = tools.get_tool_schemas()
        self._messages: list[dict[str, Any]] = []
        self._ollama_connection = ollama_connection or AgentLoopOllamaConnection(base_url=config.ollama_url, session=session)
        self._owns_ollama_connection = ollama_connection is None
        self._context_message_observer = context_message_observer
        self._eval_count = 0
        self._turn_number = 0
        self._instance_id = f"{id(self):x}"
        self._logger = logging.getLogger(f"{__name__}.AgentLoop[{self._instance_id}:{config.model}]")
        self._append_context_message({"role": "system", "content": system_prompt})

    @property
    def eval_count(self) -> int:
        return self._eval_count

    async def __aenter__(self) -> "AgentLoop":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_ollama_connection:
            self._logger.debug("closing owned AgentLoop Ollama connection")
            await self._ollama_connection.close()

    async def send_user_message(self, message: str) -> AgentReply:
        self._logger.debug(
            "received user message message_len=%s message=%r history_len_before=%s",
            len(message),
            message,
            len(self._messages),
        )
        self._append_context_message({"role": "user", "content": message})
        tool_calls_this_message = 0
        repair_attempts = 0

        try:
            while True:
                started_at = time.perf_counter()
                response = await self._send_chat_request()
                elapsed_ms = round((time.perf_counter() - started_at) * 1000)
                assistant_message = _parse_assistant_message(response)
                self._append_context_message(assistant_message)

                self._record_eval_count(response, elapsed_ms)
                tool_calls = _extract_tool_calls(assistant_message)
                if not tool_calls:
                    content = assistant_message.get("content", "")
                    if not isinstance(content, str):
                        raise ValueError("assistant message content must be a string")
                    self._logger.debug(
                        "returning final assistant reply reply_len=%s reply=%r history_len=%s eval_count=%s",
                        len(content),
                        content,
                        len(self._messages),
                        self._eval_count,
                    )
                    return AgentReply(reply_text=content, end_conversation=False)

                tool_calls_this_message += len(tool_calls)
                self._logger.debug(
                    "assistant requested tool calls count=%s total_tool_calls_this_message=%s tool_calls=%s",
                    len(tool_calls),
                    tool_calls_this_message,
                    tool_calls,
                )
                if tool_calls_this_message > self._config.max_tool_calls_per_message:
                    raise RuntimeError("model exceeded max_tool_calls_per_message")

                for tool_call in tool_calls:
                    try:
                        tool_name, arguments = _parse_tool_call(tool_call)
                        self._logger.info("tool call requested tool=%s arguments=%s", tool_name, arguments)
                        self._logger.debug("calling tool tool=%s arguments=%s", tool_name, arguments)
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
                        tool_error_message = _tool_error_message("invalid_tool_call", str(exc))
                        self._logger.debug("appending corrective tool message message=%s", tool_error_message)
                        self._append_context_message(tool_error_message)
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
                        tool_error_message = _tool_error_message(tool_name, str(exc))
                        self._logger.debug("appending corrective tool message tool=%s message=%s", tool_name, tool_error_message)
                        self._append_context_message(tool_error_message)
                        continue
                    except Exception as exc:
                        self._logger.exception("tool execution failed tool=%s", tool_name)
                        tool_error_message = _tool_error_message(tool_name, str(exc))
                        self._logger.debug("appending tool execution error message tool=%s message=%s", tool_name, tool_error_message)
                        self._append_context_message(tool_error_message)
                        continue

                    serialized_result = json.dumps(result, ensure_ascii=False)
                    self._logger.info("tool call completed tool=%s result=%s", tool_name, serialized_result)
                    self._logger.debug("tool call completed tool=%s result=%s", tool_name, serialized_result)
                    tool_result_message = {"role": "tool", "tool_name": tool_name, "content": serialized_result}
                    self._logger.debug("appending tool result message message=%s", tool_result_message)
                    self._append_context_message(tool_result_message)
        except Exception:
            self._logger.exception("unrecoverable agent loop error")
            return AgentReply(reply_text=MODEL_FAILURE_REPLY, end_conversation=True)

    def _append_context_message(self, message: dict[str, Any]) -> None:
        self._messages.append(message)
        self._logger.info("context message appended message=%s", message)
        if self._context_message_observer is not None:
            self._context_message_observer(message)

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
        response = await self._ollama_connection.chat(
            payload,
            model=self._config.model,
            fallback_model=self._config.fallback_model,
            fallback_backoff_seconds=self._config.fallback_backoff_seconds,
            request_timeout_seconds=self._config.request_timeout_seconds,
        )
        if response.get("done") is not True:
            raise ValueError("Ollama chat response did not finish")
        return response

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
