from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from aiohttp import WSMsgType

from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, NewConversation, ProcessingUpdate, SessionAttributes
from ai_server.messages import SessionRejected, TextMessage
from ai_server.messages import RequestFollowUp, WaitForNewConversation, WaitForNewMessage
from ai_server.messages import endpoint_event_to_json, session_event_from_json, text_message_to_events

DEFAULT_WEBSOCKET_URL = "ws://127.0.0.1:2137/chat"
WEBSOCKET_HEARTBEAT_SECONDS = 2.0
INTERRUPTED_EXIT_CODE = 130
SystemMessagePrinter = Callable[[str], None]


class WsClientInterrupted(Exception):
    """Raised when the websocket client is interrupted by SIGINT or SIGTERM."""


class WebsocketDisconnected(Exception):
    """Raised when the websocket connection is closed unexpectedly."""


class WebsocketSessionRejected(Exception):
    """Raised when the server rejects the websocket session."""


@dataclass(frozen=True)
class WaitState:
    starts_new_conversation: bool
    follow_up_requested: bool = False
    timeout_seconds: float | None = None


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


def handle_websocket_message(
    websocket,
    message,
    *,
    system_message_printer: SystemMessagePrinter | None = None,
    show_wait_for_new_conversation_message: bool = True,
) -> WaitState | None:
    print_system_message = system_message_printer or _print_system_message
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
        if isinstance(event, ProcessingUpdate):
            print_system_message("processing...")
            return None
        if isinstance(event, SessionRejected):
            raise WebsocketSessionRejected(event.reason)
        if isinstance(event, WaitForNewConversation):
            if show_wait_for_new_conversation_message:
                print_system_message("Conversation ended; waiting for a new conversation.")
            return WaitState(starts_new_conversation=True)
        if isinstance(event, RequestFollowUp):
            if event.timeout_seconds is None:
                print_system_message("Follow-up requested.")
            else:
                print_system_message(f"Follow-up requested; timeout is {event.timeout_seconds:g}s.")
            return WaitState(
                starts_new_conversation=False,
                follow_up_requested=True,
                timeout_seconds=event.timeout_seconds,
            )
        if isinstance(event, WaitForNewMessage):
            return WaitState(starts_new_conversation=False)
        raise RuntimeError(f"unsupported server event: {type(event).__name__}")

    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
        raise WebsocketDisconnected("websocket closed")
    if message.type == WSMsgType.ERROR:
        raise WebsocketDisconnected("websocket connection failed") from websocket.exception()
    raise RuntimeError(f"unsupported websocket message type: {message.type}")


def _print_system_message(text: str) -> None:
    print(text, flush=True)


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
