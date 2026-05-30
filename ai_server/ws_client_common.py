from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass

from aiohttp import WSMsgType

from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, NewConversation, SessionAttributes, TextMessage
from ai_server.messages import WaitForNewConversation, WaitForNewMessage
from ai_server.messages import endpoint_event_to_json, session_event_from_json, text_message_to_events

DEFAULT_WEBSOCKET_URL = "ws://127.0.0.1:2137/chat"
INTERRUPTED_EXIT_CODE = 130


class WsClientInterrupted(Exception):
    """Raised when the websocket client is interrupted by SIGINT or SIGTERM."""


class WebsocketDisconnected(Exception):
    """Raised when the websocket connection is closed unexpectedly."""


@dataclass(frozen=True)
class WaitState:
    starts_new_conversation: bool


async def send_session_attributes(websocket, user: str | None, area: str | None) -> None:
    attributes = {}
    if user:
        attributes["user"] = user
    if area:
        attributes["area"] = area
    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes=attributes)))


async def send_user_text(websocket, text: str, starts_new_conversation: bool) -> None:
    if starts_new_conversation:
        await websocket.send_str(endpoint_event_to_json(NewConversation(attributes={})))
    for event in text_message_to_events(TextMessage(text=text)):
        await websocket.send_str(endpoint_event_to_json(event))


def handle_websocket_message(websocket, message) -> WaitState | None:
    if message.type == WSMsgType.TEXT:
        event = session_event_from_json(message.data)
        if isinstance(event, MessageBegin):
            return None
        if isinstance(event, MessageFragment):
            print(event.text, end="", flush=True)
            return None
        if isinstance(event, MessageEnd):
            print(flush=True)
            return None
        if isinstance(event, WaitForNewConversation):
            return WaitState(starts_new_conversation=True)
        if isinstance(event, WaitForNewMessage):
            return WaitState(starts_new_conversation=False)
        raise RuntimeError(f"unsupported server event: {type(event).__name__}")

    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
        raise WebsocketDisconnected("websocket closed")
    if message.type == WSMsgType.ERROR:
        raise WebsocketDisconnected("websocket connection failed") from websocket.exception()
    raise RuntimeError(f"unsupported websocket message type: {message.type}")


async def receive_websocket_message(websocket, stop_event: asyncio.Event):
    receive_task = asyncio.create_task(websocket.receive())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, pending = await asyncio.wait(
            (receive_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task

        if stop_task in done:
            raise WsClientInterrupted()

        return receive_task.result()
    finally:
        for task in (receive_task, stop_task):
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
