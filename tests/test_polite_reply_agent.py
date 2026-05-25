import asyncio
import logging

import pytest

from ai_server.agent.polite_reply import (
    GENERATION_OPTIONS,
    GENERATION_FAILURE_MESSAGE,
    POLITE_REPLY_PROMPT,
    PoliteReplyAgent,
)
from ai_server.interfaces import Conversation
from ai_server.messages import TextMessage, text_message_to_events
from conftest import FakeConversationEndpoint


class FakeResponse:
    def __init__(self, body: dict, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def json(self) -> dict:
        return self._body


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.requests = []

    def post(self, url: str, json: dict):
        self.requests.append({"url": url, "json": json})
        return self.responses.pop(0)


def test_preload_posts_model_keep_alive() -> None:
    session = FakeSession([FakeResponse({"done": True})])
    agent = PoliteReplyAgent(model="qwen3:4b", session=session)

    asyncio.run(agent.preload())

    assert session.requests == [
        {
            "url": "http://127.0.0.1:11434/api/generate",
            "json": {
                "model": "qwen3:4b",
                "prompt": "",
                "stream": False,
                "keep_alive": -1,
            },
        }
    ]


def test_polite_reply_sends_wrapped_prompt_and_returns_reply(caplog) -> None:
    session = FakeSession([FakeResponse({"response": "Dzień dobry!"})])
    agent = PoliteReplyAgent(model="qwen3:4b", session=session)
    endpoint = FakeConversationEndpoint([TextMessage(text="siema")])
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    with caplog.at_level(logging.DEBUG):
        asyncio.run(agent.run_conversation(conversation, endpoint))

    assert session.requests == [
        {
            "url": "http://127.0.0.1:11434/api/generate",
            "json": {
                "model": "qwen3:4b",
                "raw": True,
                "prompt": POLITE_REPLY_PROMPT.format(user_input="siema"),
                "stream": False,
                "options": GENERATION_OPTIONS,
            },
        }
    ]
    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Dzień dobry!")))

    log_text = caplog.text
    assert any(
        record.name.endswith("PoliteReplyAgent[conversation-1]")
        and "request_len=5 reply_len=12 duration_ms=" in record.message
        for record in caplog.records
    )
    assert "siema" in log_text
    assert "Dzień dobry!" in log_text
    assert "Użytkownik: siema" in log_text
    assert "Ryszard:" in log_text


def test_polite_reply_strips_thinking_block() -> None:
    session = FakeSession([FakeResponse({"response": "<think>sekret</think>\n\nDzień dobry!"})])
    agent = PoliteReplyAgent(model="qwen3:4b", session=session)
    endpoint = FakeConversationEndpoint([TextMessage(text="siema")])
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    asyncio.run(agent.run_conversation(conversation, endpoint))

    assert endpoint.sent == list(text_message_to_events(TextMessage(text="Dzień dobry!")))


def test_polite_reply_sends_generic_apology_on_ollama_error(caplog) -> None:
    session = FakeSession([FakeResponse({"error": "missing model"}, status=500)])
    agent = PoliteReplyAgent(model="qwen3:4b", session=session)
    endpoint = FakeConversationEndpoint([TextMessage(text="tajna wiadomość")])
    conversation = Conversation(conversation_id="conversation-2", attributes={})

    with caplog.at_level(logging.DEBUG):
        asyncio.run(agent.run_conversation(conversation, endpoint))

    assert endpoint.sent == list(text_message_to_events(TextMessage(text=GENERATION_FAILURE_MESSAGE)))
    assert "generation failed request_len=15 duration_ms=" in caplog.text
    assert "tajna wiadomość" in caplog.text
    assert GENERATION_FAILURE_MESSAGE not in caplog.text
