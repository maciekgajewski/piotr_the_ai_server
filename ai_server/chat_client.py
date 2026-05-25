from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import termios
from contextlib import suppress
from dataclasses import dataclass

try:
    import readline  # noqa: F401
except ImportError:
    pass

from aiohttp import ClientSession, WSMsgType

from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, NewConversation, SessionAttributes, TextMessage
from ai_server.messages import WaitForNewConversation, WaitForNewMessage
from ai_server.messages import endpoint_event_to_json, session_event_from_json, text_message_to_events

DEFAULT_WEBSOCKET_URL = "ws://127.0.0.1:2137/chat"
WAITING_FOR_NEW_CONVERSATION_PROMPT = "waiting for new conversation> "
WAITING_FOR_NEXT_MESSAGE_PROMPT = "waiting for next message> "
INTERRUPTED_EXIT_CODE = 130


class ChatInterrupted(Exception):
    """Raised when the chat client is interrupted by SIGINT or SIGTERM."""


@dataclass(frozen=True)
class ChatClientOptions:
    url: str
    user: str | None
    location: str | None
    messages: tuple[str, ...]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with the AI server over websocket.")
    parser.add_argument("--user", help="Optional session user attribute sent in the websocket handshake.")
    parser.add_argument("--location", help="Optional session location attribute sent in the websocket handshake.")
    parser.add_argument(
        "--message",
        action="append",
        default=[],
        help="Scripted message to send. Can be repeated. Scripted mode exits on the next wait-state event.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_WEBSOCKET_URL,
        help=f"Websocket URL. Defaults to {DEFAULT_WEBSOCKET_URL}.",
    )
    return parser.parse_args(argv)


async def run_chat(options: ChatClientOptions) -> None:
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    terminal_state = _TerminalState()
    stdin_reader = await _open_stdin_reader() if not options.messages else None

    async with ClientSession() as session:
        try:
            async with session.ws_connect(options.url) as websocket:
                await websocket.send_str(
                    endpoint_event_to_json(SessionAttributes(attributes=_session_attributes(options)))
                )
                scripted_messages = list(options.messages)
                sent_all_scripted_messages = not scripted_messages
                pending_prompt: str | None = None

                while True:
                    message = await _receive_websocket_message(websocket, stop_event)
                    if message.type == WSMsgType.TEXT:
                        event = session_event_from_json(message.data)
                        if isinstance(event, MessageBegin):
                            continue
                        if isinstance(event, MessageFragment):
                            print(event.text, end="", flush=True)
                            continue
                        if isinstance(event, MessageEnd):
                            print(flush=True)
                            continue
                        if isinstance(event, WaitForNewConversation):
                            pending_prompt = WAITING_FOR_NEW_CONVERSATION_PROMPT
                        elif isinstance(event, WaitForNewMessage):
                            pending_prompt = WAITING_FOR_NEXT_MESSAGE_PROMPT
                        else:
                            raise RuntimeError(f"unsupported server event: {type(event).__name__}")

                        if scripted_messages:
                            text = scripted_messages.pop(0)
                            sent_all_scripted_messages = not scripted_messages
                            await _send_user_text(
                                websocket,
                                text,
                                starts_new_conversation=isinstance(event, WaitForNewConversation),
                            )
                            pending_prompt = None
                            continue

                        if options.messages and sent_all_scripted_messages:
                            return

                        assert stdin_reader is not None
                        text = await _read_interactive_line(stdin_reader, pending_prompt, stop_event)
                        if text is None:
                            return
                        await _send_user_text(
                            websocket,
                            text,
                            starts_new_conversation=isinstance(event, WaitForNewConversation),
                        )
                        pending_prompt = None
                        continue

                    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                        return
                    if message.type == WSMsgType.ERROR:
                        raise RuntimeError("websocket connection failed") from websocket.exception()
        finally:
            terminal_state.restore()


async def _send_user_text(websocket, text: str, starts_new_conversation: bool) -> None:
    if starts_new_conversation:
        await websocket.send_str(endpoint_event_to_json(NewConversation(attributes={})))
    for event in text_message_to_events(TextMessage(text=text)):
        await websocket.send_str(endpoint_event_to_json(event))


async def _receive_websocket_message(websocket, stop_event: asyncio.Event):
    receive_task = asyncio.create_task(websocket.receive())
    stop_task = asyncio.create_task(stop_event.wait())
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
        raise ChatInterrupted()

    return receive_task.result()


async def _open_stdin_reader() -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    return reader


async def _read_interactive_line(
    reader: asyncio.StreamReader,
    prompt: str,
    stop_event: asyncio.Event,
) -> str | None:
    print(prompt, end="", flush=True)
    line_task = asyncio.create_task(reader.readline())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        (line_task, stop_task),
        return_when=asyncio.FIRST_COMPLETED,
    )

    for task in pending:
        task.cancel()
    for task in pending:
        with suppress(asyncio.CancelledError):
            await task

    if stop_task in done:
        print(flush=True)
        raise ChatInterrupted()

    line = line_task.result()
    if line == b"":
        return None
    return line.decode().rstrip("\n")


def _session_attributes(options: ChatClientOptions) -> dict[str, str]:
    attributes = {}
    if options.user:
        attributes["user"] = options.user
    if options.location:
        attributes["location"] = options.location
    return attributes


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signame), stop_event.set)
        except NotImplementedError:
            pass


class _TerminalState:
    def __init__(self) -> None:
        try:
            self._fd = sys.stdin.fileno()
        except OSError:
            self._fd = None
            self._state = None
            return

        self._state = termios.tcgetattr(self._fd) if sys.stdin.isatty() else None

    def restore(self) -> None:
        if self._fd is None or self._state is None:
            return
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._state)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    options = ChatClientOptions(
        url=args.url,
        user=args.user,
        location=args.location,
        messages=tuple(args.message),
    )
    try:
        asyncio.run(run_chat(options))
    except (ChatInterrupted, KeyboardInterrupt):
        return INTERRUPTED_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
