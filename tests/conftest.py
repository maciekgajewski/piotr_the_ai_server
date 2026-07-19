from __future__ import annotations

from typing import Any

from ai_server.conversations.agent_context import AgentExecutionContext
from ai_server.conversations.agent_runtime import AgentChannel
from ai_server.conversations.contexts import ConversationContext, ConversationMedium
from ai_server.conversations.messages import AssistantMessageCompleted, AssistantMessageStarted, AssistantTextChunk
from ai_server.conversations.messages import ProcessingUpdate, TurnDisposition, TurnDispositionKind, UserMessage


TextMessage = UserMessage


def agent_context(
    conversation_id: str,
    attributes: dict[str, str],
    state: dict[str, Any] | None = None,
    processing_update_callback=None,
    processing_update_interval_seconds: float = 5.0,
) -> AgentExecutionContext:
    raw_state = dict(state or {})
    user_settings = raw_state.pop("user_settings", {})
    context = ConversationContext(
        conversation_id=conversation_id,
        input_session_id=f"test-session-{conversation_id}",
        medium=ConversationMedium(attributes["medium"]),
        user=attributes.get("user"),
        area=attributes.get("area"),
        user_settings=user_settings,
    )
    return AgentExecutionContext(
        conversation=context,
        agent_state=raw_state,
        processing_update_callback=processing_update_callback,
        processing_update_interval_seconds=processing_update_interval_seconds,
    )


def text_message_to_events(message: UserMessage | str):
    text = message.text if isinstance(message, UserMessage) else message
    events = [AssistantMessageStarted()]
    if text:
        events.append(AssistantTextChunk(text))
    events.append(AssistantMessageCompleted())
    return tuple(events)


class FakeAgentChannel:
    def __init__(self, incoming: list[UserMessage] | None = None) -> None:
        self._incoming = list(incoming or [])
        self.sent = []
        self.control_events: list[str] = []
        self.processing_updates: list[ProcessingUpdate] = []
        self._stream_open = False

    async def receive_user_message(self) -> UserMessage:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)

    async def processing_update(self) -> None:
        event = ProcessingUpdate()
        self.processing_updates.append(event)

    async def start_assistant_message(self) -> None:
        assert not self._stream_open
        self._stream_open = True
        self.sent.append(AssistantMessageStarted())

    async def send_text(self, text: str) -> None:
        assert self._stream_open
        self.sent.append(AssistantTextChunk(text))

    async def complete_assistant_message(self) -> None:
        assert self._stream_open
        self._stream_open = False
        self.sent.append(AssistantMessageCompleted())

    async def send_message(self, text: str) -> None:
        if isinstance(text, UserMessage):
            text = text.text
        await self.start_assistant_message()
        if text:
            await self.send_text(text)
        await self.complete_assistant_message()

    async def request_follow_up(self) -> None:
        self.control_events.append("request_follow_up")

    async def end_conversation(self) -> None:
        self.control_events.append("end_conversation")


async def run_agent(agent, conversation: AgentExecutionContext | ConversationContext, channel: FakeAgentChannel) -> None:
    run_execution_context = getattr(agent, "_run_execution_context", None)
    if isinstance(conversation, AgentExecutionContext) and run_execution_context is not None:
        await run_execution_context(conversation, channel)
        return
    context = conversation.conversation if isinstance(conversation, AgentExecutionContext) else conversation
    await agent.run_agent_conversation(context, channel)
