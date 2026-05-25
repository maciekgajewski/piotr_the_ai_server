from __future__ import annotations

from collections.abc import AsyncIterator

from ai_server.interfaces import ConversationEndpoint
from ai_server.messages import ConversationInputEvent, ConversationOutputEvent, MessageBegin, MessageEnd, MessageFragment
from ai_server.messages import TextMessage, text_message_to_events


class FakeConversationEndpoint(ConversationEndpoint):
    def __init__(self, incoming: list[TextMessage] | None = None) -> None:
        self._incoming: list[ConversationInputEvent] = []
        for message in incoming or []:
            self._incoming.extend(text_message_to_events(message))
        self.sent: list[ConversationOutputEvent] = []

    async def receive(self) -> ConversationInputEvent:
        if not self._incoming:
            raise AssertionError("unexpected receive")
        return self._incoming.pop(0)

    async def send(self, event: ConversationOutputEvent) -> None:
        self.sent.append(event)

    async def messages(self) -> AsyncIterator[TextMessage]:
        while self._incoming:
            text_parts: list[str] = []
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
                raise AssertionError(f"unsupported test event: {type(event).__name__}")

    async def send_message(self, message: TextMessage) -> None:
        for event in text_message_to_events(message):
            await self.send(event)
