from __future__ import annotations

import asyncio

import pytest

from ai_server.interfaces import CommunicationEndpoint, Conversation
from ai_server.messages import MessageBegin, SessionAttributes
from ai_server.sessions import Session, _SessionConversationEndpoint


class FakeEndpoint(CommunicationEndpoint):
    def __init__(self) -> None:
        self.incoming = asyncio.Queue()
        self.outgoing = []

    async def receive(self):
        return await self.incoming.get()

    async def send(self, event) -> None:
        self.outgoing.append(event)


@pytest.mark.parametrize("attributes", [{}, {"medium": "invalid"}])
def test_trusted_local_session_rejects_missing_or_invalid_medium(attributes) -> None:
    """CP-ATTR-001: constructor-supplied attributes obey the same medium contract."""
    with pytest.raises(AssertionError, match="conversation.medium must be one of"):
        Session(session_id="session-1", endpoint=FakeEndpoint(), attributes=attributes)


def test_handshake_session_rejects_medium_change() -> None:
    """CP-ATTR-002: an established constructor medium cannot be replaced."""

    async def run() -> None:
        endpoint = FakeEndpoint()
        endpoint.incoming.put_nowait(SessionAttributes(attributes={"medium": "voice"}))
        session = Session(
            session_id="session-1",
            endpoint=endpoint,
            attributes={"medium": "text"},
            require_session_attributes=True,
        )

        with pytest.raises(AssertionError, match="Session medium is immutable"):
            await session._receive_session_attributes()

    asyncio.run(run())


def test_input_begin_is_rejected_while_assistant_output_is_open() -> None:
    """CP-FLOOR-001: user input cannot overlap assistant output."""

    async def run() -> None:
        endpoint, conversation_endpoint = _conversation_endpoint()
        await conversation_endpoint.send(MessageBegin("assistant-1"))
        endpoint.incoming.put_nowait(MessageBegin("user-1"))

        with pytest.raises(AssertionError, match="assistant output message is open"):
            await conversation_endpoint.receive()

    asyncio.run(run())


def test_output_begin_is_rejected_while_user_input_is_open() -> None:
    """CP-FLOOR-001: assistant output cannot overlap user input."""

    async def run() -> None:
        endpoint, conversation_endpoint = _conversation_endpoint()
        endpoint.incoming.put_nowait(MessageBegin("user-1"))
        assert await conversation_endpoint.receive() == MessageBegin("user-1")

        with pytest.raises(AssertionError, match="user input message is open"):
            await conversation_endpoint.send(MessageBegin("assistant-1"))

    asyncio.run(run())


def _conversation_endpoint() -> tuple[FakeEndpoint, _SessionConversationEndpoint]:
    endpoint = FakeEndpoint()
    session = Session(
        session_id="session-1",
        endpoint=endpoint,
        attributes={"medium": "text"},
    )
    conversation = Conversation(
        conversation_id="conversation-1",
        attributes={"medium": "text"},
    )
    return endpoint, _SessionConversationEndpoint(session, conversation)
