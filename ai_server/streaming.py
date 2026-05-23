from __future__ import annotations

from ai_server.interfaces import CommunicationEndpoint, EndpointClosed
from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, UserMessage


async def receive_user_message(endpoint: CommunicationEndpoint) -> UserMessage:
    text_parts: list[str] = []
    saw_begin = False

    while True:
        event = await endpoint.receive()
        if isinstance(event, MessageBegin):
            saw_begin = True
            text_parts.clear()
            continue
        if isinstance(event, MessageFragment):
            if not saw_begin:
                raise ValueError("received message fragment before message begin")
            text_parts.append(event.text)
            continue
        if isinstance(event, MessageEnd):
            if not saw_begin:
                raise ValueError("received message end before message begin")
            return UserMessage(text="".join(text_parts))

        raise ValueError(f"unsupported message event: {type(event).__name__}")


async def send_user_message(endpoint: CommunicationEndpoint, message: UserMessage) -> None:
    await endpoint.send(MessageBegin())
    await endpoint.send(MessageFragment(text=message.text))
    await endpoint.send(MessageEnd())


async def forward_one_message(source: CommunicationEndpoint, destination: CommunicationEndpoint) -> None:
    while True:
        event = await source.receive()
        await destination.send(event)
        if isinstance(event, MessageEnd):
            return
