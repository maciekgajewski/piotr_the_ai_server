from __future__ import annotations

import argparse
import atexit
import asyncio
import os
import readline
import signal
import sys
import termios
import threading
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Deque

from aiohttp import ClientError, ClientSession, WSMsgType

from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, NewConversation, SessionAttributes, TextMessage
from ai_server.messages import WaitForNewConversation, WaitForNewMessage
from ai_server.messages import endpoint_event_to_json, session_event_from_json, text_message_to_events

DEFAULT_WEBSOCKET_URL = "ws://127.0.0.1:2137/chat"
WAITING_FOR_NEW_CONVERSATION_PROMPT = "waiting for new conversation> "
WAITING_FOR_NEXT_MESSAGE_PROMPT = "waiting for next message> "
WAITING_FOR_SERVER_PROMPT = "waiting for server> "
DISCONNECTED_PROMPT = "disconnected; reconnecting> "
INTERRUPTED_EXIT_CODE = 130
RECONNECT_INITIAL_DELAY_SECONDS = 0.5
RECONNECT_MAX_DELAY_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 5.0
CLIENT_TEXT_STYLE = "\033[3;90m"
CLIENT_TEXT_RESET = "\033[0m"
CHAT_HISTORY_ENV_VAR = "PIOTR_CHAT_HISTORY"
CHAT_HISTORY_LENGTH = 1000
_readline_history_registered = False


class ChatInterrupted(Exception):
    """Raised when the chat client is interrupted by SIGINT or SIGTERM."""


class ChatExited(Exception):
    """Raised when the user exits the chat client."""


class WebsocketDisconnected(Exception):
    """Raised when the websocket connection is closed unexpectedly."""


class _ClientCommandResult:
    NOT_COMMAND = "not_command"
    HANDLED = "handled"
    EXIT = "exit"


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
    try:
        if options.messages:
            await _run_scripted_chat(options, stop_event)
        else:
            _configure_readline()
            input_session = _InteractiveInputSession(asyncio.get_running_loop())
            input_session.start()
            await _run_interactive_chat(options, input_session, stop_event)
    finally:
        terminal_state.restore()


async def _run_scripted_chat(options: ChatClientOptions, stop_event: asyncio.Event) -> None:
    async with ClientSession() as session:
        async with session.ws_connect(options.url) as websocket:
            await _send_session_attributes(websocket, options)
            scripted_messages = list(options.messages)
            sent_all_scripted_messages = not scripted_messages

            try:
                while True:
                    message = await _receive_websocket_message(websocket, stop_event)
                    wait_state = _handle_websocket_message(websocket, message)
                    if wait_state is None:
                        continue

                    if scripted_messages:
                        text = scripted_messages.pop(0)
                        sent_all_scripted_messages = not scripted_messages
                        _print_client_message(f"> {text}")
                        await _send_user_text(
                            websocket,
                            text,
                            starts_new_conversation=wait_state.starts_new_conversation,
                        )
                        continue

                    if sent_all_scripted_messages:
                        return
            except WebsocketDisconnected as exc:
                _print_client_message(f"Connection lost: {exc}.")


async def _run_interactive_chat(
    options: ChatClientOptions,
    input_session: "_InteractiveInputSession",
    stop_event: asyncio.Event,
) -> None:
    pending_lines: Deque[str] = deque()
    reconnect_delay = RECONNECT_INITIAL_DELAY_SECONDS

    async with ClientSession() as session:
        while True:
            input_session.set_prompt(DISCONNECTED_PROMPT)
            try:
                websocket = await _connect_interactive(
                    session,
                    options,
                    input_session,
                    pending_lines,
                    stop_event,
                    reconnect_delay,
                )
            except ChatExited:
                return
            reconnect_delay = RECONNECT_INITIAL_DELAY_SECONDS

            try:
                input_session.set_prompt(WAITING_FOR_SERVER_PROMPT)
                await _send_session_attributes(websocket, options)
                _print_client_message("Connected.")
                await _run_interactive_connection(websocket, input_session, pending_lines, stop_event)
                return
            except (WebsocketDisconnected, ClientError, OSError) as exc:
                input_session.set_prompt(DISCONNECTED_PROMPT)
                _print_client_message(f"Connection lost: {exc}.")
                reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY_SECONDS)
            finally:
                await websocket.close()


async def _connect_interactive(
    session: ClientSession,
    options: ChatClientOptions,
    input_session: "_InteractiveInputSession",
    pending_lines: Deque[str],
    stop_event: asyncio.Event,
    reconnect_delay: float,
):
    while True:
        input_session.set_prompt(DISCONNECTED_PROMPT)
        _print_client_message(f"Connecting to {options.url} ...")
        connect_task = asyncio.create_task(asyncio.wait_for(session.ws_connect(options.url), CONNECT_TIMEOUT_SECONDS))
        line_task = asyncio.create_task(input_session.read_line())
        stop_task = asyncio.create_task(stop_event.wait())

        done, pending = await asyncio.wait(
            (connect_task, line_task, stop_task),
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done:
            await _cancel_tasks(pending)
            raise ChatInterrupted()

        if connect_task in done:
            await _cancel_tasks(pending)
            if line_task in done and _handle_offline_line(line_task.result(), pending_lines):
                with suppress(Exception):
                    websocket = connect_task.result()
                    await websocket.close()
                raise ChatExited()

            try:
                return connect_task.result()
            except (asyncio.TimeoutError, ClientError, OSError) as exc:
                _print_client_message(f"Connection failed: {exc}. Retrying in {reconnect_delay:.1f}s.")
                if await _sleep_or_handle_offline_input(input_session, pending_lines, stop_event, reconnect_delay):
                    raise ChatExited()
                reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY_SECONDS)
                continue

        await _cancel_tasks(pending)
        line = line_task.result()
        if _handle_offline_line(line, pending_lines):
            raise ChatExited()


async def _run_interactive_connection(
    websocket,
    input_session: "_InteractiveInputSession",
    pending_lines: Deque[str],
    stop_event: asyncio.Event,
) -> None:
    while True:
        message = await _receive_websocket_message(websocket, stop_event)
        wait_state = _handle_websocket_message(websocket, message)
        if wait_state is None:
            continue

        input_session.set_prompt(wait_state.prompt)
        while True:
            text, wait_state = await _read_next_interactive_text(
                websocket,
                input_session,
                pending_lines,
                stop_event,
                wait_state,
            )
            if text is None:
                return
            command_result = _handle_client_command(text)
            if command_result == _ClientCommandResult.EXIT:
                return
            if command_result == _ClientCommandResult.HANDLED:
                input_session.set_prompt(wait_state.prompt)
                continue

            input_session.set_prompt(WAITING_FOR_SERVER_PROMPT)
            await _send_user_text(websocket, text, starts_new_conversation=wait_state.starts_new_conversation)
            break


async def _read_next_interactive_text(
    websocket,
    input_session: "_InteractiveInputSession",
    pending_lines: Deque[str],
    stop_event: asyncio.Event,
    wait_state: "_WaitState",
) -> tuple[str | None, "_WaitState"]:
    if pending_lines:
        return pending_lines.popleft(), wait_state

    while True:
        receive_task = asyncio.create_task(_receive_websocket_message(websocket, stop_event))
        line_task = asyncio.create_task(input_session.read_line())

        done, pending = await asyncio.wait(
            (receive_task, line_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        await _cancel_tasks(pending)

        if line_task in done:
            return line_task.result(), wait_state

        next_wait_state = _handle_websocket_message(websocket, receive_task.result())
        if next_wait_state is not None:
            wait_state = next_wait_state
            input_session.set_prompt(wait_state.prompt)


@dataclass(frozen=True)
class _WaitState:
    prompt: str
    starts_new_conversation: bool


async def _send_session_attributes(websocket, options: ChatClientOptions) -> None:
    await websocket.send_str(endpoint_event_to_json(SessionAttributes(attributes=_session_attributes(options))))


def _handle_websocket_message(websocket, message) -> _WaitState | None:
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
            return _WaitState(WAITING_FOR_NEW_CONVERSATION_PROMPT, starts_new_conversation=True)
        if isinstance(event, WaitForNewMessage):
            return _WaitState(WAITING_FOR_NEXT_MESSAGE_PROMPT, starts_new_conversation=False)
        raise RuntimeError(f"unsupported server event: {type(event).__name__}")

    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
        raise WebsocketDisconnected("websocket closed")
    if message.type == WSMsgType.ERROR:
        raise WebsocketDisconnected("websocket connection failed") from websocket.exception()
    raise RuntimeError(f"unsupported websocket message type: {message.type}")


def _handle_client_command(text: str) -> str:
    command = text.strip()
    if not command.startswith("/"):
        return _ClientCommandResult.NOT_COMMAND

    if command == "/exit":
        return _ClientCommandResult.EXIT

    if command == "/help":
        _print_client_message("Commands:")
        _print_client_message("  /help  Show this help.")
        _print_client_message("  /exit  Exit the chat client.")
        return _ClientCommandResult.HANDLED

    _print_client_message(f"Unknown command: {command}. Type /help for available commands.")
    return _ClientCommandResult.HANDLED


def _handle_offline_line(line: str | None, pending_lines: Deque[str]) -> bool:
    if line is None:
        return True

    command_result = _handle_client_command(line)
    if command_result == _ClientCommandResult.EXIT:
        return True
    if command_result == _ClientCommandResult.HANDLED:
        return False

    if line == "":
        return False

    pending_lines.append(line)
    _print_client_message("Queued message; it will be sent after reconnect.")
    return False


async def _sleep_or_handle_offline_input(
    input_session: "_InteractiveInputSession",
    pending_lines: Deque[str],
    stop_event: asyncio.Event,
    delay_seconds: float,
) -> bool:
    sleep_task = asyncio.create_task(asyncio.sleep(delay_seconds))
    line_task = asyncio.create_task(input_session.read_line())
    stop_task = asyncio.create_task(stop_event.wait())

    done, pending = await asyncio.wait(
        (sleep_task, line_task, stop_task),
        return_when=asyncio.FIRST_COMPLETED,
    )
    await _cancel_tasks(pending)

    if stop_task in done:
        raise ChatInterrupted()
    if line_task in done:
        return _handle_offline_line(line_task.result(), pending_lines)
    return False


async def _cancel_tasks(tasks) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError, Exception):
            await task


def _configure_readline() -> None:
    readline.set_history_length(CHAT_HISTORY_LENGTH)
    with suppress(AttributeError):
        readline.set_auto_history(True)
    with suppress(Exception):
        readline.parse_and_bind("set enable-bracketed-paste on")

    history_path = _chat_history_path()
    with suppress(FileNotFoundError, OSError):
        readline.read_history_file(str(history_path))

    global _readline_history_registered
    if not _readline_history_registered:
        atexit.register(_write_readline_history, history_path)
        _readline_history_registered = True


def _chat_history_path() -> Path:
    configured_path = os.environ.get(CHAT_HISTORY_ENV_VAR)
    if configured_path:
        return Path(configured_path).expanduser()

    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).expanduser() / "piotr" / "chat_client_history"

    return Path.home() / ".local" / "state" / "piotr" / "chat_client_history"


def _write_readline_history(history_path: Path) -> None:
    with suppress(OSError):
        history_path.parent.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(str(history_path))


def _print_client_message(text: str) -> None:
    print(_style_client_text(text), flush=True)


def _style_client_text(text: str) -> str:
    return f"{CLIENT_TEXT_STYLE}{text}{CLIENT_TEXT_RESET}"


def _style_client_prompt(text: str) -> str:
    return f"{CLIENT_TEXT_STYLE}{text}"


class _InteractiveInputSession:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._prompt = DISCONNECTED_PROMPT
        self._prompt_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._read_loop, name="chat-client-readline", daemon=True)
        self._thread.start()

    def set_prompt(self, prompt: str) -> None:
        with self._prompt_lock:
            self._prompt = prompt
        with suppress(Exception):
            readline.redisplay()

    async def read_line(self) -> str | None:
        return await self._lines.get()

    def _read_loop(self) -> None:
        while True:
            prompt = self._current_prompt()
            try:
                line = input(_style_client_prompt(prompt))
                print(CLIENT_TEXT_RESET, end="", flush=True)
            except EOFError:
                print(CLIENT_TEXT_RESET, flush=True)
                self._put_line(None)
                return
            self._put_line(line)

    def _current_prompt(self) -> str:
        with self._prompt_lock:
            return self._prompt

    def _put_line(self, line: str | None) -> None:
        try:
            self._loop.call_soon_threadsafe(self._lines.put_nowait, line)
        except RuntimeError:
            return


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
