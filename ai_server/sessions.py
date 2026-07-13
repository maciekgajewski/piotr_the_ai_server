from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ai_server.agent import Agent
from ai_server.interfaces import CommunicationEndpoint, Conversation, ConversationEndpoint, ConversationMedium, EndpointClosed
from ai_server.messages import ConversationEnded, ConversationInputEvent, ConversationOutputEvent, MessageBegin, MessageEnd
from ai_server.messages import ConversationEnded as ConversationEndedEvent
from ai_server.messages import FollowUpRequested, MessageFragment, NewConversation, ProcessingUpdate, ReadyForConversation
from ai_server.messages import SessionAttributes, TextMessage, text_message_to_events
from ai_server.user_settings import ConfigUserSettingsProvider, UserSettingsProvider


class SessionState(Enum):
    HANDSHAKE = "handshake"
    IDLE = "idle"
    AWAITING_USER_MESSAGE = "awaiting_user_message"
    RECEIVING_USER_MESSAGE = "receiving_user_message"
    AGENT_ACTIVE = "agent_active"
    AWAITING_FOLLOW_UP = "awaiting_follow_up"
    ENDING_CONVERSATION = "ending_conversation"
    CLOSED = "closed"


@dataclass
class Session:
    session_id: str
    endpoint: CommunicationEndpoint
    attributes: dict[str, str] | None = None
    require_session_attributes: bool = False
    user_settings: dict[str, dict[str, Any]] | None = None
    user_settings_provider: UserSettingsProvider | None = None
    follow_up_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        self._logger = logging.getLogger(f"{__name__}.Session[{self.session_id}]")
        self.attributes = dict(self.attributes or {})
        self.user_settings = dict(self.user_settings or {})
        if self.user_settings_provider is None:
            self.user_settings_provider = ConfigUserSettingsProvider(self.user_settings)
        assert self.follow_up_timeout_seconds > 0
        if not self.require_session_attributes or "medium" in self.attributes:
            _assert_valid_medium(self.attributes)
        self._state = SessionState.HANDSHAKE if self.require_session_attributes else SessionState.IDLE

    async def run(self, agent: Agent) -> None:
        try:
            if self.require_session_attributes:
                await self._receive_session_attributes()

            while True:
                self._transition(SessionState.IDLE, "ready")
                await self.endpoint.send(ReadyForConversation())
                conversation = await self._receive_new_conversation()
                conversation_endpoint = _SessionConversationEndpoint(self, conversation)
                conversation_logger = logging.getLogger(
                    f"{__name__}.Session[{self.session_id}].Conversation[{conversation.conversation_id}]"
                )
                conversation_logger.info(
                    "started user=%r area=%r medium=%s attributes=%s",
                    conversation.user,
                    conversation.area,
                    conversation.medium.value,
                    conversation.attributes,
                )
                reason = "completed"
                try:
                    await agent.run_conversation(conversation, conversation_endpoint)
                    conversation_endpoint.assert_idle()
                    reason = conversation_endpoint.termination_reason or reason
                except ConversationEnded as exc:
                    reason = exc.reason
                    conversation_logger.info("ended by endpoint")
                finally:
                    self._transition(SessionState.ENDING_CONVERSATION, reason)
                    conversation_endpoint.assert_messages_closed()
                    await self.endpoint.send(ConversationEndedEvent(reason=reason))
                    conversation_logger.info("ended reason=%s", reason)
        except EndpointClosed:
            self._transition(SessionState.CLOSED, "endpoint_closed")
            self._logger.debug("endpoint closed")

    async def _receive_session_attributes(self) -> None:
        event = await self.endpoint.receive()
        assert isinstance(event, SessionAttributes), f"expected SessionAttributes, got {type(event).__name__}"
        _assert_valid_medium(event.attributes)
        existing_medium = self.attributes.get("medium")
        if existing_medium is not None:
            assert event.attributes["medium"] == existing_medium, "Session medium is immutable"
        self.attributes = _merge_attributes(self.attributes or {}, event.attributes)
        _assert_valid_medium(self.attributes)
        await self._assert_known_user(self.attributes.get("user"))
        self._logger.info(
            "session attributes user=%r area=%r medium=%r attributes=%s",
            self.attributes.get("user"),
            self.attributes.get("area"),
            self.attributes.get("medium"),
            self.attributes,
        )
        self._transition(SessionState.IDLE, "session_attributes")

    async def _receive_new_conversation(self) -> Conversation:
        event = await self.endpoint.receive()
        assert isinstance(event, NewConversation), f"expected NewConversation, got {type(event).__name__}"
        assert "medium" not in event.attributes, "NewConversation must not override medium"
        attributes = _merge_attributes(self.attributes or {}, event.attributes)
        await self._assert_known_user(attributes.get("user"))
        assert self.user_settings_provider is not None
        settings = await self.user_settings_provider.settings_for_user(attributes.get("user"))
        conversation = Conversation(
            conversation_id=str(uuid.uuid4()),
            attributes=attributes,
            state={"user_settings": settings},
        )
        self._transition(SessionState.AWAITING_USER_MESSAGE, "new_conversation")
        return conversation

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

    def _transition(self, state: SessionState, cause: str) -> None:
        old_state = self._state
        self._state = state
        self._logger.debug("state transition old=%s cause=%s new=%s", old_state.value, cause, state.value)


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
        follow_up_timeout_seconds: float = 60.0,
    ) -> None:
        session = Session(
            session_id=session_id or str(uuid.uuid4()),
            endpoint=endpoint,
            attributes=attributes,
            require_session_attributes=require_session_attributes,
            user_settings=user_settings,
            user_settings_provider=user_settings_provider,
            follow_up_timeout_seconds=follow_up_timeout_seconds,
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
        self._input_message_id: str | None = None
        self._output_open = False
        self._output_message_id: str | None = None
        self._requires_follow_up_request = False
        self._follow_up_requested = False
        self._closed = False
        self.termination_reason: str | None = None
        self._used_message_ids: set[str] = set()
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
        except ConversationEnded as exc:
            self._closed = True
            self.termination_reason = exc.reason
            raise

        if isinstance(event, MessageBegin):
            assert not self._input_open, "received MessageBegin while input message is open"
            assert not self._output_open, "received MessageBegin while assistant output message is open"
            assert event.message_id not in self._used_message_ids, "reused message_id"
            self._used_message_ids.add(event.message_id)
            self._input_open = True
            self._input_message_id = event.message_id
            self._session._transition(SessionState.RECEIVING_USER_MESSAGE, "message_begin")
            return event
        if isinstance(event, MessageFragment):
            assert self._input_open, "received MessageFragment before MessageBegin"
            assert event.message_id == self._input_message_id, "received MessageFragment for wrong message_id"
            return event
        if isinstance(event, MessageEnd):
            assert self._input_open, "received MessageEnd before MessageBegin"
            assert event.message_id == self._input_message_id, "received MessageEnd for wrong message_id"
            self._input_open = False
            self._input_message_id = None
            self._requires_follow_up_request = True
            self._session._transition(SessionState.AGENT_ACTIVE, "message_end")
            return event

        raise AssertionError(f"unsupported conversation input event: {type(event).__name__}")

    async def send(self, event: ConversationOutputEvent) -> None:
        if isinstance(event, ProcessingUpdate):
            assert not self._output_open, "sent ProcessingUpdate while output message is open"
            await self._session.endpoint.send(event)
            return
        if isinstance(event, MessageBegin):
            assert not self._output_open, "sent MessageBegin while output message is open"
            assert not self._input_open, "sent MessageBegin while user input message is open"
            assert event.message_id not in self._used_message_ids, "reused message_id"
            self._used_message_ids.add(event.message_id)
            self._output_open = True
            self._output_message_id = event.message_id
            await self._session.endpoint.send(event)
            return
        if isinstance(event, MessageFragment):
            assert self._output_open, "sent MessageFragment before MessageBegin"
            assert event.message_id == self._output_message_id, "sent MessageFragment for wrong message_id"
            await self._session.endpoint.send(event)
            return
        if isinstance(event, MessageEnd):
            assert self._output_open, "sent MessageEnd before MessageBegin"
            assert event.message_id == self._output_message_id, "sent MessageEnd for wrong message_id"
            self._output_open = False
            self._output_message_id = None
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
                        text = "".join(text_parts)
                        if not text.strip():
                            self._logger.info("dropped empty input message")
                            self._requires_follow_up_request = False
                            self._closed = True
                            return
                        yield TextMessage(text=text)
                        break
                    raise AssertionError(f"unsupported conversation input event: {type(event).__name__}")
            except ConversationEnded:
                return

    async def send_message(self, message: TextMessage) -> None:
        for event in text_message_to_events(message):
            await self.send(event)

    async def request_follow_up(self) -> None:
        assert self._requires_follow_up_request, "requested follow-up before completing an input message"
        assert not self._follow_up_requested, "requested duplicate follow-up"
        assert not self._input_open, "requested follow-up while input message is open"
        assert not self._output_open, "requested follow-up while output message is open"
        self._follow_up_requested = True
        self._session._transition(SessionState.AWAITING_FOLLOW_UP, "request_follow_up")
        await self._session.endpoint.send(FollowUpRequested(self._session.follow_up_timeout_seconds))

    def assert_idle(self) -> None:
        assert not self._input_open, "conversation ended with open input message"
        assert not self._output_open, "conversation ended with open output message"
        assert not self._follow_up_requested, "conversation ended with requested follow-up not consumed"

    def assert_messages_closed(self) -> None:
        assert not self._input_open, "conversation ended with open input message"
        assert not self._output_open, "conversation ended with open output message"


def _merge_attributes(base: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    merged = dict(base)
    merged.update(overrides)
    return merged


def _assert_valid_medium(attributes: dict[str, str]) -> None:
    raw_medium = attributes.get("medium")
    try:
        assert raw_medium is not None
        ConversationMedium(raw_medium)
    except (AssertionError, ValueError) as exc:
        raise AssertionError(f"conversation.medium must be one of: voice, text; got {raw_medium!r}") from exc
