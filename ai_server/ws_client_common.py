from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from aiohttp import WSMsgType

from ai_server.websocket_messages import AssistantMessageAborted, AssistantMessageCompleted, AssistantMessageStarted
from ai_server.websocket_messages import AssistantTextChunk, ConversationEnded, ConversationReady, ConversationStarted
from ai_server.websocket_messages import FollowUpMessage, FollowUpRequested, FollowUpTimedOut, ProcessingUpdate
from ai_server.websocket_messages import ProtocolRejected, SessionAccepted, SessionStart, StartConversation
from ai_server.websocket_messages import client_event_to_json, server_event_from_json


DEFAULT_WEBSOCKET_URL = "ws://127.0.0.1:2137/chat"
WEBSOCKET_HEARTBEAT_SECONDS = 2.0
INTERRUPTED_EXIT_CODE = 130
SystemMessagePrinter = Callable[[str], None]


class WsClientInterrupted(Exception):
    pass


class WebsocketDisconnected(Exception):
    pass


class WebsocketSessionRejected(Exception):
    pass


@dataclass(frozen=True)
class WaitState:
    starts_new_conversation: bool
    follow_up_requested: bool = False
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class ConversationTerminated:
    reason: str


def validate_follow_up_timeout(value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 or not math.isfinite(value):
        raise ValueError("follow_up_timeout_seconds must be a positive finite number")
    return float(value)


async def send_session_start(websocket, user: str | None, area: str | None) -> None:
    await websocket.send_str(client_event_to_json(SessionStart(user=user, area=area)))


async def send_user_text(websocket, text: str, starts_new_conversation: bool) -> None:
    event = StartConversation(text) if starts_new_conversation else FollowUpMessage(text)
    await websocket.send_str(client_event_to_json(event))


async def send_follow_up_timed_out(websocket) -> None:
    await websocket.send_str(client_event_to_json(FollowUpTimedOut()))


def handle_websocket_message(
    websocket,
    message,
    *,
    follow_up_timeout_seconds: float,
    system_message_printer: SystemMessagePrinter | None = None,
    show_wait_for_new_conversation_message: bool = True,
) -> WaitState | ConversationTerminated | None:
    del websocket
    timeout_seconds = validate_follow_up_timeout(follow_up_timeout_seconds)
    print_system_message = system_message_printer or _print_system_message
    if message.type == WSMsgType.TEXT:
        event = server_event_from_json(message.data)
        if isinstance(event, (SessionAccepted, ConversationStarted, AssistantMessageStarted)):
            return None
        if isinstance(event, AssistantTextChunk):
            print(event.text, end="", flush=True)
            return None
        if isinstance(event, AssistantMessageCompleted):
            print(flush=True)
            return None
        if isinstance(event, AssistantMessageAborted):
            print(flush=True)
            print_system_message(f"Assistant message aborted: {event.reason}.")
            return None
        if isinstance(event, ProcessingUpdate):
            print_system_message("processing...")
            return None
        if isinstance(event, ProtocolRejected):
            raise WebsocketSessionRejected(event.code.value)
        if isinstance(event, ConversationEnded):
            if show_wait_for_new_conversation_message:
                print_system_message("Conversation ended; waiting for a new conversation.")
            return ConversationTerminated(event.reason)
        if isinstance(event, ConversationReady):
            return WaitState(starts_new_conversation=True)
        if isinstance(event, FollowUpRequested):
            print_system_message(f"Follow-up requested; timeout is {timeout_seconds:g}s.")
            return WaitState(
                starts_new_conversation=False,
                follow_up_requested=True,
                timeout_seconds=timeout_seconds,
            )
        raise RuntimeError(f"unsupported server event: {type(event).__name__}")

    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
        raise WebsocketDisconnected("websocket closed")
    if message.type == WSMsgType.ERROR:
        raise WebsocketDisconnected("websocket connection failed") from message.data
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
