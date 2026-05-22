import asyncio
import logging

import pytest

from ai_server.agent.polite_reply import (
    GENERATION_FAILURE_MESSAGE,
    POLITE_REPLY_PROMPT,
    PoliteReplyAgent,
)
from ai_server.endpoint import EndpointClosed
from ai_server.messages import UserMessage


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


class FakeEndpoint:
    def __init__(self, incoming: list[UserMessage]) -> None:
        self._incoming = incoming
        self.sent = []

    async def receive(self) -> UserMessage:
        if not self._incoming:
            raise EndpointClosed()
        return self._incoming.pop(0)

    async def send(self, msg: UserMessage) -> None:
        self.sent.append(msg)


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
    endpoint = FakeEndpoint([UserMessage(text="siema")])

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(EndpointClosed):
            asyncio.run(agent.run(endpoint, "session-1"))

    assert session.requests == [
        {
            "url": "http://127.0.0.1:11434/api/generate",
            "json": {
                "model": "qwen3:4b",
                "prompt": POLITE_REPLY_PROMPT.format(user_input="siema"),
                "stream": False,
                "think": False,
            },
        }
    ]
    assert endpoint.sent == [UserMessage(text="Dzień dobry!")]

    log_text = caplog.text
    assert "PoliteReplyAgent[session-1] request_len=5 reply_len=12 duration_ms=" in log_text
    assert "siema" in log_text
    assert "Dzień dobry!" in log_text
    assert POLITE_REPLY_PROMPT.format(user_input="siema") in log_text


def test_polite_reply_strips_thinking_block() -> None:
    session = FakeSession([FakeResponse({"response": "<think>sekret</think>\n\nDzień dobry!"})])
    agent = PoliteReplyAgent(model="qwen3:4b", session=session)
    endpoint = FakeEndpoint([UserMessage(text="siema")])

    with pytest.raises(EndpointClosed):
        asyncio.run(agent.run(endpoint, "session-1"))

    assert endpoint.sent == [UserMessage(text="Dzień dobry!")]


def test_polite_reply_sends_generic_apology_on_ollama_error(caplog) -> None:
    session = FakeSession([FakeResponse({"error": "missing model"}, status=500)])
    agent = PoliteReplyAgent(model="qwen3:4b", session=session)
    endpoint = FakeEndpoint([UserMessage(text="tajna wiadomość")])

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(EndpointClosed):
            asyncio.run(agent.run(endpoint, "session-2"))

    assert endpoint.sent == [UserMessage(text=GENERATION_FAILURE_MESSAGE)]
    assert "generation failed request_len=15 duration_ms=" in caplog.text
    assert "tajna wiadomość" in caplog.text
    assert GENERATION_FAILURE_MESSAGE not in caplog.text
