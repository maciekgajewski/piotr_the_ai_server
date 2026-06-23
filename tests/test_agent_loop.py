import asyncio
import copy
import logging
from enum import Enum
from typing import Annotated, Any

import pytest

from ai_server.agent_loop import AgentCallableSet, AgentLoop, AgentLoopConfig, MODEL_FAILURE_REPLY
from ai_server.agent_loop.agent_loop_ollama_connection import AgentLoopOllamaConnection


class FakeResponse:
    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def json(self) -> dict[str, Any]:
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, json: dict[str, Any], timeout=None):
        self.requests.append({"url": url, "json": copy.deepcopy(json)})
        return self.responses.pop(0)

    async def close(self) -> None:
        self.closed = True


class LightMode(Enum):
    READING = "reading"
    EVENING = "evening"


class ExampleTools(AgentCallableSet):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @AgentCallableSet.tool
    async def add(
        self,
        a: Annotated[int, "The first number"],
        b: Annotated[int, "The second number"],
        label: str = "sum",
    ) -> dict[str, int | str]:
        """Add two numbers."""
        self.calls.append(("add", {"a": a, "b": b, "label": label}))
        return {"label": label, "value": a + b}

    @AgentCallableSet.tool(name="set_light_mode", description="Set a light mode.")
    async def set_mode(self, mode: Annotated[LightMode, "The desired light mode"]) -> str:
        self.calls.append(("set_mode", {"mode": mode}))
        return mode.value

    @AgentCallableSet.tool
    async def collect(self, values: list[int], metadata: dict[str, str]) -> list[int]:
        """Collect values."""
        self.calls.append(("collect", {"values": values, "metadata": metadata}))
        return values


class SlowTools(AgentCallableSet):
    @AgentCallableSet.tool
    async def wait(self) -> dict[str, str]:
        await asyncio.sleep(0.04)
        return {"status": "ok"}


def test_tool_schema_uses_convention_explicit_params_defaults_and_annotated_descriptions() -> None:
    tools = ExampleTools()

    assert tools.get_tool_schemas() == [
        {
            "type": "function",
            "function": {
                "name": "add",
                "description": "Add two numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "integer", "description": "The first number"},
                        "b": {"type": "integer", "description": "The second number"},
                        "label": {"type": "string", "default": "sum"},
                    },
                    "required": ["a", "b"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "set_light_mode",
                "description": "Set a light mode.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["reading", "evening"],
                            "description": "The desired light mode",
                        }
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "collect",
                "description": "Collect values.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "values": {"type": "array", "items": {"type": "integer"}},
                        "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
                    },
                    "required": ["values", "metadata"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def test_call_tool_validates_arguments_and_converts_enum() -> None:
    tools = ExampleTools()

    result = asyncio.run(tools.call_tool("set_light_mode", {"mode": "reading"}))

    assert result == "reading"
    assert tools.calls == [("set_mode", {"mode": LightMode.READING})]


def test_call_tool_rejects_invalid_arguments() -> None:
    tools = ExampleTools()

    with pytest.raises(ValueError, match="add.a must be an integer"):
        asyncio.run(tools.call_tool("add", {"a": True, "b": 2}))


def test_call_tool_rejects_non_json_return_value() -> None:
    class BadReturnTools(AgentCallableSet):
        @AgentCallableSet.tool
        async def broken(self) -> float:
            return float("nan")

    with pytest.raises(ValueError, match="finite number"):
        asyncio.run(BadReturnTools().call_tool("broken", {}))


def test_call_tool_omits_none_fields_from_dictionary_results() -> None:
    class OptionalReturnTools(AgentCallableSet):
        @AgentCallableSet.tool
        async def optional(self) -> dict[str, Any]:
            return {
                "kept": "value",
                "omitted": None,
                "nested": {"kept": 1, "omitted": None},
                "items": [None, {"kept": True, "omitted": None}],
            }

    result = asyncio.run(OptionalReturnTools().call_tool("optional", {}))

    assert result == {
        "kept": "value",
        "nested": {"kept": 1},
        "items": [None, {"kept": True}],
    }


def test_tool_decorator_rejects_non_async_method() -> None:
    with pytest.raises(TypeError, match="async methods"):

        class BadTools(AgentCallableSet):
            @AgentCallableSet.tool
            def broken(self, value: str) -> str:
                return value


def test_tool_registry_rejects_unsupported_annotation() -> None:
    with pytest.raises(TypeError, match="unsupported JSON tool type annotation"):

        class BadTools(AgentCallableSet):
            @AgentCallableSet.tool
            async def broken(self, value: tuple[str, ...]) -> str:
                return ",".join(value)


def test_agent_loop_returns_final_reply_without_tool_call() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 4,
                    "message": {"role": "assistant", "content": "Cześć!"},
                }
            )
        ]
    )
    loop = AgentLoop(AgentLoopConfig(model="qwen3:4b"), "System.", ExampleTools(), session=session)

    reply = asyncio.run(loop.send_user_message("hej"))

    assert reply.reply_text == "Cześć!"
    assert reply.end_conversation is False
    assert loop.eval_count == 4
    assert session.requests[0]["url"] == "http://127.0.0.1:11434/api/chat"
    assert session.requests[0]["json"]["stream"] is False
    assert session.requests[0]["json"]["messages"] == [
        {"role": "system", "content": "System."},
        {"role": "user", "content": "hej"},
    ]
    assert session.requests[0]["json"]["tools"] == ExampleTools().get_tool_schemas()


def test_agent_loop_notifies_context_message_observer() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 4,
                    "message": {"role": "assistant", "thinking": "Rozważam.", "content": "Cześć!"},
                }
            )
        ]
    )
    observed_messages: list[dict[str, Any]] = []
    loop = AgentLoop(
        AgentLoopConfig(model="qwen3:4b"),
        "System.",
        ExampleTools(),
        session=session,
        context_message_observer=observed_messages.append,
    )

    reply = asyncio.run(loop.send_user_message("hej"))

    assert reply.reply_text == "Cześć!"
    assert observed_messages == [
        {"role": "system", "content": "System."},
        {"role": "user", "content": "hej"},
        {"role": "assistant", "thinking": "Rozważam.", "content": "Cześć!"},
    ]


def test_agent_loop_emits_processing_updates_for_llm_and_tool_work() -> None:
    async def run() -> list[str]:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "done": True,
                        "eval_count": 1,
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"function": {"name": "wait", "arguments": {}}}],
                        },
                    }
                ),
                FakeResponse(
                    {
                        "done": True,
                        "eval_count": 1,
                        "message": {"role": "assistant", "content": "Gotowe."},
                    }
                ),
            ]
        )
        updates: list[str] = []

        async def emit_update() -> None:
            updates.append("processing")

        loop = AgentLoop(
            AgentLoopConfig(model="qwen3:4b"),
            "System.",
            SlowTools(),
            session=session,
            processing_update_callback=emit_update,
            processing_update_interval_seconds=0.01,
        )

        reply = await loop.send_user_message("poczekaj")

        assert reply.reply_text == "Gotowe."
        return updates

    updates = asyncio.run(run())

    assert len(updates) >= 5


def test_agent_loop_runs_multiple_tool_turns_and_tracks_history() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 3,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "add", "arguments": {"a": 2, "b": 3}}},
                        ],
                    },
                }
            ),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 5,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "set_light_mode", "arguments": {"mode": "evening"}}},
                        ],
                    },
                }
            ),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 7,
                    "message": {"role": "assistant", "content": "Gotowe."},
                }
            ),
        ]
    )
    tools = ExampleTools()
    loop = AgentLoop(AgentLoopConfig(model="qwen3:4b"), "System.", tools, session=session)

    reply = asyncio.run(loop.send_user_message("policz i ustaw światło"))

    assert reply.reply_text == "Gotowe."
    assert reply.end_conversation is False
    assert loop.eval_count == 15
    assert tools.calls == [
        ("add", {"a": 2, "b": 3, "label": "sum"}),
        ("set_mode", {"mode": LightMode.EVENING}),
    ]

    third_request_messages = session.requests[2]["json"]["messages"]
    assert third_request_messages[-4:] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "add", "arguments": {"a": 2, "b": 3}}}],
        },
        {"role": "tool", "tool_name": "add", "content": '{"label": "sum", "value": 5}'},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "set_light_mode", "arguments": {"mode": "evening"}}}],
        },
        {"role": "tool", "tool_name": "set_light_mode", "content": '"evening"'},
    ]


def test_agent_loop_sends_corrective_tool_message_for_invalid_params(caplog) -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 2,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"function": {"name": "add", "arguments": {"a": "wrong", "b": 3}}},
                        ],
                    },
                }
            ),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 4,
                    "message": {"role": "assistant", "content": "Nie mogłem użyć narzędzia."},
                }
            ),
        ]
    )
    loop = AgentLoop(AgentLoopConfig(model="qwen3:4b"), "System.", ExampleTools(), session=session)

    with caplog.at_level(logging.WARNING):
        reply = asyncio.run(loop.send_user_message("policz"))

    assert reply.reply_text == "Nie mogłem użyć narzędzia."
    assert reply.end_conversation is False
    assert "invalid tool call tool=add repair_attempt=1" in caplog.text
    assert session.requests[1]["json"]["messages"][-1]["role"] == "tool"
    assert session.requests[1]["json"]["messages"][-1]["tool_name"] == "add"
    assert "add.a must be an integer" in session.requests[1]["json"]["messages"][-1]["content"]


def test_agent_loop_returns_failure_reply_on_unrecoverable_error() -> None:
    session = FakeSession([FakeResponse({"done": False, "message": {"role": "assistant", "content": ""}})])
    loop = AgentLoop(AgentLoopConfig(model="qwen3:4b"), "System.", ExampleTools(), session=session)

    reply = asyncio.run(loop.send_user_message("hej"))

    assert reply.reply_text == MODEL_FAILURE_REPLY
    assert reply.end_conversation is True


def test_agent_loop_retries_with_fallback_model_on_main_model_rejection() -> None:
    session = FakeSession(
        [
            FakeResponse({"error": "rejected"}, status=429),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 4,
                    "message": {"role": "assistant", "content": "Fallback działa."},
                }
            ),
        ]
    )
    loop = AgentLoop(
        AgentLoopConfig(
            model="gpt-oss:20b-cloud",
            fallback_model="qwen3:4b-instruct",
        ),
        "System.",
        ExampleTools(),
        session=session,
    )

    reply = asyncio.run(loop.send_user_message("hej"))

    assert reply.reply_text == "Fallback działa."
    assert reply.end_conversation is False
    assert [request["json"]["model"] for request in session.requests] == [
        "gpt-oss:20b-cloud",
        "qwen3:4b-instruct",
    ]


def test_agent_loop_retries_with_fallback_model_on_main_model_server_error() -> None:
    session = FakeSession(
        [
            FakeResponse({"error": "server error"}, status=500),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 4,
                    "message": {"role": "assistant", "content": "Fallback po 500."},
                }
            ),
        ]
    )
    loop = AgentLoop(
        AgentLoopConfig(
            model="gpt-oss:20b-cloud",
            fallback_model="qwen3:4b-instruct",
        ),
        "System.",
        ExampleTools(),
        session=session,
    )

    reply = asyncio.run(loop.send_user_message("hej"))

    assert reply.reply_text == "Fallback po 500."
    assert reply.end_conversation is False
    assert [request["json"]["model"] for request in session.requests] == [
        "gpt-oss:20b-cloud",
        "qwen3:4b-instruct",
    ]


def test_agent_loop_shared_backoff_uses_fallback_for_matching_model_pair() -> None:
    now = 1000.0
    session = FakeSession(
        [
            FakeResponse({"error": "rejected"}, status=403),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 1,
                    "message": {"role": "assistant", "content": "Pierwszy fallback."},
                }
            ),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 2,
                    "message": {"role": "assistant", "content": "Drugi fallback."},
                }
            ),
        ]
    )
    connection = AgentLoopOllamaConnection(
        base_url="http://ollama:11434",
        session=session,
        now_factory=lambda: now,
    )
    config = AgentLoopConfig(
        model="gpt-oss:20b-cloud",
        ollama_url="http://ollama:11434",
        fallback_model="qwen3:4b-instruct",
        fallback_backoff_seconds=300,
    )

    first_loop = AgentLoop(config, "System.", ExampleTools(), ollama_connection=connection)
    second_loop = AgentLoop(config, "System.", ExampleTools(), ollama_connection=connection)

    first_reply = asyncio.run(first_loop.send_user_message("hej"))
    second_reply = asyncio.run(second_loop.send_user_message("hej znowu"))

    assert first_reply.reply_text == "Pierwszy fallback."
    assert second_reply.reply_text == "Drugi fallback."
    assert [request["json"]["model"] for request in session.requests] == [
        "gpt-oss:20b-cloud",
        "qwen3:4b-instruct",
        "qwen3:4b-instruct",
    ]


def test_agent_loop_backoff_is_scoped_to_model_pair() -> None:
    now = 1000.0
    session = FakeSession(
        [
            FakeResponse({"error": "rejected"}, status=403),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 1,
                    "message": {"role": "assistant", "content": "Fallback."},
                }
            ),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 2,
                    "message": {"role": "assistant", "content": "Main."},
                }
            ),
        ]
    )
    connection = AgentLoopOllamaConnection(
        base_url="http://ollama:11434",
        session=session,
        now_factory=lambda: now,
    )

    first_loop = AgentLoop(
        AgentLoopConfig(
            model="gpt-oss:20b-cloud",
            ollama_url="http://ollama:11434",
            fallback_model="qwen3:4b-instruct",
        ),
        "System.",
        ExampleTools(),
        ollama_connection=connection,
    )
    second_loop = AgentLoop(
        AgentLoopConfig(
            model="gpt-oss:120b-cloud",
            ollama_url="http://ollama:11434",
            fallback_model="qwen3:4b-instruct",
        ),
        "System.",
        ExampleTools(),
        ollama_connection=connection,
    )

    asyncio.run(first_loop.send_user_message("hej"))
    second_reply = asyncio.run(second_loop.send_user_message("hej znowu"))

    assert second_reply.reply_text == "Main."
    assert [request["json"]["model"] for request in session.requests] == [
        "gpt-oss:20b-cloud",
        "qwen3:4b-instruct",
        "gpt-oss:120b-cloud",
    ]


def test_agent_loop_retries_main_model_after_backoff_expires() -> None:
    now = 1000.0

    def current_time() -> float:
        return now

    session = FakeSession(
        [
            FakeResponse({"error": "rejected"}, status=403),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 1,
                    "message": {"role": "assistant", "content": "Fallback."},
                }
            ),
            FakeResponse(
                {
                    "done": True,
                    "eval_count": 2,
                    "message": {"role": "assistant", "content": "Main wrócił."},
                }
            ),
        ]
    )
    connection = AgentLoopOllamaConnection(
        base_url="http://ollama:11434",
        session=session,
        now_factory=current_time,
    )
    config = AgentLoopConfig(
        model="gpt-oss:20b-cloud",
        ollama_url="http://ollama:11434",
        fallback_model="qwen3:4b-instruct",
        fallback_backoff_seconds=10,
    )

    first_loop = AgentLoop(config, "System.", ExampleTools(), ollama_connection=connection)
    asyncio.run(first_loop.send_user_message("hej"))
    now = 1011.0
    second_loop = AgentLoop(config, "System.", ExampleTools(), ollama_connection=connection)
    second_reply = asyncio.run(second_loop.send_user_message("hej znowu"))

    assert second_reply.reply_text == "Main wrócił."
    assert [request["json"]["model"] for request in session.requests] == [
        "gpt-oss:20b-cloud",
        "qwen3:4b-instruct",
        "gpt-oss:20b-cloud",
    ]


def test_agent_loop_without_fallback_keeps_failure_behavior_on_rejection() -> None:
    session = FakeSession([FakeResponse({"error": "rejected"}, status=429)])
    loop = AgentLoop(AgentLoopConfig(model="gpt-oss:20b-cloud"), "System.", ExampleTools(), session=session)

    reply = asyncio.run(loop.send_user_message("hej"))

    assert reply.reply_text == MODEL_FAILURE_REPLY
    assert reply.end_conversation is True
    assert [request["json"]["model"] for request in session.requests] == ["gpt-oss:20b-cloud"]


def test_agent_loop_config_validates_fallback_fields() -> None:
    with pytest.raises(ValueError, match="fallback_model must be a non-empty string"):
        AgentLoopConfig(model="main", fallback_model="")

    with pytest.raises(ValueError, match="fallback_backoff_seconds must be positive"):
        AgentLoopConfig(model="main", fallback_model="fallback", fallback_backoff_seconds=0)
