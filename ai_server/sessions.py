from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ai_server.agent import Agent
from ai_server.interfaces import CommunicationEndpoint, Conversation, ConversationEndpoint, EndpointClosed
from ai_server.messages import ConversationEnded, ConversationInputEvent, ConversationOutputEvent, MessageBegin, MessageEnd
from ai_server.messages import MessageFragment, NewConversation, ProcessingUpdate, RequestFollowUp, SessionAttributes, TextMessage
from ai_server.messages import WaitForNewConversation, text_message_to_events
from ai_server.user_settings import ConfigUserSettingsProvider, UserSettingsProvider


@dataclass
class Session:
    session_id: str
    endpoint: CommunicationEndpoint
    attributes: dict[str, str] | None = None
    require_session_attributes: bool = False
    user_settings: dict[str, dict[str, Any]] | None = None
    user_settings_provider: UserSettingsProvider | None = None

    def __post_init__(self) -> None:
        self._logger = logging.getLogger(f"{__name__}.Session[{self.session_id}]")
        self.attributes = dict(self.attributes or {})
        self.user_settings = dict(self.user_settings or {})
        if self.user_settings_provider is None:
            self.user_settings_provider = ConfigUserSettingsProvider(self.user_settings)

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
                    "started user=%r area=%r attributes=%s",
                    conversation.user,
                    conversation.area,
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
        await self._assert_known_user(self.attributes.get("user"))
        self._logger.info(
            "session attributes user=%r area=%r attributes=%s",
            self.attributes.get("user"),
            self.attributes.get("area"),
            self.attributes,
        )

    async def _receive_new_conversation(self) -> Conversation:
        event = await self.endpoint.receive()
        assert isinstance(event, NewConversation), f"expected NewConversation, got {type(event).__name__}"
        attributes = _merge_attributes(self.attributes or {}, event.attributes)
        await self._assert_known_user(attributes.get("user"))
        assert self.user_settings_provider is not None
        settings = await self.user_settings_provider.settings_for_user(attributes.get("user"))
        return Conversation(
            conversation_id=str(uuid.uuid4()),
            attributes=attributes,
            state={"user_settings": settings},
        )

    async def _assert_known_user(self, user: str | None) -> None:
        if user is None:
            return
        assert self.user_settings_provider is not None
        user_exists = await self.user_settings_provider.user_exists(user)
        assert user_exists, f"unknown user: {user}"

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
        user_settings: dict[str, dict[str, Any]] | None = None,
        user_settings_provider: UserSettingsProvider | None = None,
    ) -> None:
        session = Session(
            session_id=session_id or str(uuid.uuid4()),
            endpoint=endpoint,
            attributes=attributes,
            require_session_attributes=require_session_attributes,
            user_settings=user_settings,
            user_settings_provider=user_settings_provider,
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
        self._requires_follow_up_request = False
        self._follow_up_requested = False
        self._closed = False
        self._logger = logging.getLogger(
            f"{__name__}.ConversationEndpoint[{session.session_id}:{conversation.conversation_id}]"
        )

    async def receive(self) -> ConversationInputEvent:
        if self._closed:
            raise ConversationEnded()

        if self._requires_follow_up_request:
            if not self._follow_up_requested:
                self._closed = True
                raise ConversationEnded()
            self._requires_follow_up_request = False
            self._follow_up_requested = False

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
            self._requires_follow_up_request = True
            return event

        raise AssertionError(f"unsupported conversation input event: {type(event).__name__}")

    async def send(self, event: ConversationOutputEvent) -> None:
        if isinstance(event, ProcessingUpdate):
            assert not self._output_open, "sent ProcessingUpdate while output message is open"
            await self._session.endpoint.send(event)
            return
        if isinstance(event, RequestFollowUp):
            assert self._requires_follow_up_request, "sent RequestFollowUp before completing an input message"
            assert not self._follow_up_requested, "sent duplicate RequestFollowUp"
            assert not self._input_open, "sent RequestFollowUp while input message is open"
            assert not self._output_open, "sent RequestFollowUp while output message is open"
            self._follow_up_requested = True
            await self._session.endpoint.send(event)
            return
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
        assert not self._follow_up_requested, "conversation ended with requested follow-up not consumed"


def _merge_attributes(base: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    merged = dict(base)
    merged.update(overrides)
    return merged
