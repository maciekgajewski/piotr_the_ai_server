from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ai_server.agent import Agent
from ai_server.interfaces import CommunicationEndpoint, Conversation, ConversationEndpoint, EndpointClosed
from ai_server.messages import ConversationEnded, ConversationInputEvent, ConversationOutputEvent, MessageBegin, MessageEnd
from ai_server.messages import MessageFragment, NewConversation, SessionAttributes, TextMessage, WaitForNewConversation
from ai_server.messages import WaitForNewMessage, text_message_to_events


@dataclass
class Session:
    session_id: str
    endpoint: CommunicationEndpoint
    attributes: dict[str, str] | None = None
    require_session_attributes: bool = False

    def __post_init__(self) -> None:
        self._logger = logging.getLogger(f"{__name__}.Session[{self.session_id}]")
        self.attributes = dict(self.attributes or {})

    async def run(self, agent: Agent) -> None:
        try:
            if self.require_session_attributes:
                await self._receive_session_attributes()

            while True:
                await self.endpoint.send(WaitForNewConversation())
                conversation = await self._receive_new_conversation()
                conversation_endpoint = _SessionConversationEndpoint(self, conversation)
                conversation_logger = logging.getLogger(
                    f"{__name__}.Session[{self.session_id}].Conversation[{conversation.conversation_id}]"
                )
                conversation_logger.info(
                    "started user=%r location=%r attributes=%s",
                    conversation.user,
                    conversation.location,
                    conversation.attributes,
                )
                try:
                    await agent.run_conversation(conversation, conversation_endpoint)
                    conversation_endpoint.assert_idle()
                except ConversationEnded:
                    conversation_logger.info("ended by endpoint")
                finally:
                    conversation_logger.info("ended")
        except EndpointClosed:
            self._logger.debug("endpoint closed")

    async def _receive_session_attributes(self) -> None:
        event = await self.endpoint.receive()
        assert isinstance(event, SessionAttributes), f"expected SessionAttributes, got {type(event).__name__}"
        self.attributes = _merge_attributes(self.attributes or {}, event.attributes)
        self._logger.info(
            "session attributes user=%r location=%r attributes=%s",
            self.attributes.get("user"),
            self.attributes.get("location"),
            self.attributes,
        )

    async def _receive_new_conversation(self) -> Conversation:
        event = await self.endpoint.receive()
        assert isinstance(event, NewConversation), f"expected NewConversation, got {type(event).__name__}"
        attributes = _merge_attributes(self.attributes or {}, event.attributes)
        return Conversation(
            conversation_id=str(uuid.uuid4()),
            attributes=attributes,
        )

    async def receive_conversation_event(self) -> ConversationInputEvent:
        event = await self.endpoint.receive()
        if isinstance(event, ConversationEnded):
            raise event
        assert isinstance(event, (MessageBegin, MessageFragment, MessageEnd)), (
            f"expected conversation message event, got {type(event).__name__}"
        )
        return event


class SessionManager:
    def __init__(self, agent: Agent) -> None:
        self._logger = logging.getLogger(f"{__name__}.SessionManager")
        self._agent = agent
        self._sessions: dict[str, Session] = {}

    async def run_session(
        self,
        endpoint: CommunicationEndpoint,
        attributes: dict[str, str] | None = None,
        require_session_attributes: bool = False,
        session_id: str | None = None,
    ) -> None:
        session = Session(
            session_id=session_id or str(uuid.uuid4()),
            endpoint=endpoint,
            attributes=attributes,
            require_session_attributes=require_session_attributes,
        )
        self._sessions[session.session_id] = session
        session._logger.info("new session")

        try:
            await session.run(self._agent)
        finally:
            self._sessions.pop(session.session_id, None)
            session._logger.info("ended")

    @property
    def session_count(self) -> int:
        return len(self._sessions)


class _SessionConversationEndpoint(ConversationEndpoint):
    def __init__(self, session: Session, conversation: Conversation) -> None:
        self._session = session
        self._conversation = conversation
        self._input_open = False
        self._output_open = False
        self._wait_before_next_input = False
        self._closed = False
        self._logger = logging.getLogger(
            f"{__name__}.ConversationEndpoint[{session.session_id}:{conversation.conversation_id}]"
        )

    async def receive(self) -> ConversationInputEvent:
        if self._closed:
            raise ConversationEnded()

        if self._wait_before_next_input:
            await self._session.endpoint.send(WaitForNewMessage())
            self._wait_before_next_input = False

        try:
            event = await self._session.receive_conversation_event()
        except ConversationEnded:
            self._closed = True
            raise

        if isinstance(event, MessageBegin):
            assert not self._input_open, "received MessageBegin while input message is open"
            self._input_open = True
            return event
        if isinstance(event, MessageFragment):
            assert self._input_open, "received MessageFragment before MessageBegin"
            return event
        if isinstance(event, MessageEnd):
            assert self._input_open, "received MessageEnd before MessageBegin"
            self._input_open = False
            self._wait_before_next_input = True
            return event

        raise AssertionError(f"unsupported conversation input event: {type(event).__name__}")

    async def send(self, event: ConversationOutputEvent) -> None:
        if isinstance(event, MessageBegin):
            assert not self._output_open, "sent MessageBegin while output message is open"
            self._output_open = True
            await self._session.endpoint.send(event)
            return
        if isinstance(event, MessageFragment):
            assert self._output_open, "sent MessageFragment before MessageBegin"
            await self._session.endpoint.send(event)
            return
        if isinstance(event, MessageEnd):
            assert self._output_open, "sent MessageEnd before MessageBegin"
            self._output_open = False
            await self._session.endpoint.send(event)
            return

        raise AssertionError(f"unsupported conversation output event: {type(event).__name__}")

    async def messages(self) -> AsyncIterator[TextMessage]:
        while True:
            text_parts: list[str] = []
            try:
                while True:
                    event = await self.receive()
                    if isinstance(event, MessageBegin):
                        text_parts.clear()
                        continue
                    if isinstance(event, MessageFragment):
                        text_parts.append(event.text)
                        continue
                    if isinstance(event, MessageEnd):
                        yield TextMessage(text="".join(text_parts))
                        break
                    raise AssertionError(f"unsupported conversation input event: {type(event).__name__}")
            except ConversationEnded:
                return

    async def send_message(self, message: TextMessage) -> None:
        for event in text_message_to_events(message):
            await self.send(event)

    def assert_idle(self) -> None:
        assert not self._input_open, "conversation ended with open input message"
        assert not self._output_open, "conversation ended with open output message"


def _merge_attributes(base: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    merged = dict(base)
    merged.update(overrides)
    return merged
