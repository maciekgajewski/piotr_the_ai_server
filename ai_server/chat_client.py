from __future__ import annotations

import argparse
import atexit
import asyncio
import os
import readline
import signal
import sys
import termios
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import ClientError, ClientSession

from ai_server.ws_client_common import DEFAULT_WEBSOCKET_URL, INTERRUPTED_EXIT_CODE, WebsocketDisconnected
from ai_server.ws_client_common import WEBSOCKET_HEARTBEAT_SECONDS
from ai_server.ws_client_common import WebsocketSessionRejected
from ai_server.ws_client_common import WaitState
from ai_server.ws_client_common import handle_websocket_message, receive_websocket_message, send_session_attributes
from ai_server.ws_client_common import send_user_text

WAITING_FOR_NEW_CONVERSATION_PROMPT = "waiting for new conversation> "
WAITING_FOR_NEXT_MESSAGE_PROMPT = "waiting for next message> "
WAITING_FOR_SERVER_PROMPT = "waiting for server> "
CONNECTING_PROMPT = "connecting> "
DISCONNECTED_PROMPT = "disconnected; reconnecting> "
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


class ChatConnectionLost(Exception):
    """Raised when an established chat websocket connection is lost."""


class _ClientCommandResult:
    NOT_COMMAND = "not_command"
    HANDLED = "handled"
    EXIT = "exit"


@dataclass(frozen=True)
class ChatClientOptions:
    url: str
    user: str | None
    area: str | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with the AI server over websocket.")
    parser.add_argument("--user", help="Optional session user attribute sent in the websocket handshake.")
    parser.add_argument("--area", help="Optional Home Assistant area attribute sent in the websocket handshake.")
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
        _configure_readline()
        input_session = _InteractiveInputSession(asyncio.get_running_loop())
        input_session.start()
        await _run_interactive_chat(options, input_session, stop_event)
    finally:
        terminal_state.restore()


async def _run_interactive_chat(
    options: ChatClientOptions,
    input_session: "_InteractiveInputSession",
    stop_event: asyncio.Event,
) -> None:
    reconnect_delay = RECONNECT_INITIAL_DELAY_SECONDS
    connection_prompt = CONNECTING_PROMPT

    async with ClientSession() as session:
        while True:
            input_session.set_prompt(connection_prompt)
            try:
                websocket = await _connect_interactive(
                    session,
                    options,
                    input_session,
                    stop_event,
                    reconnect_delay,
                    connection_prompt,
                )
            except ChatExited:
                return
            reconnect_delay = RECONNECT_INITIAL_DELAY_SECONDS

            try:
                input_session.set_prompt(WAITING_FOR_SERVER_PROMPT)
                await send_session_attributes(websocket, options.user, options.area)
                await _run_interactive_connection(websocket, input_session, stop_event, options.url)
                return
            except WebsocketSessionRejected as exc:
                _print_client_message(f"Connection rejected: {exc}.")
                return
            except (WebsocketDisconnected, ClientError, OSError) as exc:
                _print_client_message(f"Connection lost: {exc}.")
                raise ChatConnectionLost() from exc
            finally:
                await websocket.close()


async def _connect_interactive(
    session: ClientSession,
    options: ChatClientOptions,
    input_session: "_InteractiveInputSession",
    stop_event: asyncio.Event,
    reconnect_delay: float,
    connection_prompt: str,
):
    while True:
        input_session.set_prompt(connection_prompt)
        _print_client_message(f"Connecting to {options.url} ...")
        connect_task = asyncio.create_task(
            asyncio.wait_for(
                session.ws_connect(options.url, heartbeat=WEBSOCKET_HEARTBEAT_SECONDS),
                CONNECT_TIMEOUT_SECONDS,
            )
        )
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
            if line_task in done and _handle_offline_line(line_task.result()):
                with suppress(Exception):
                    websocket = connect_task.result()
                    await websocket.close()
                raise ChatExited()

            try:
                return connect_task.result()
            except (asyncio.TimeoutError, ClientError, OSError) as exc:
                connection_prompt = DISCONNECTED_PROMPT
                input_session.set_prompt(connection_prompt)
                _print_client_message(f"Connection failed: {exc}. Retrying in {reconnect_delay:.1f}s.")
                if await _sleep_or_handle_offline_input(input_session, stop_event, reconnect_delay):
                    raise ChatExited()
                reconnect_delay = min(reconnect_delay * 2, RECONNECT_MAX_DELAY_SECONDS)
                continue

        await _cancel_tasks(pending)
        line = line_task.result()
        if _handle_offline_line(line):
            raise ChatExited()


async def _run_interactive_connection(
    websocket,
    input_session: "_InteractiveInputSession",
    stop_event: asyncio.Event,
    websocket_url: str,
) -> None:
    show_wait_for_new_conversation_message = False
    while True:
        wait_state = await _read_next_wait_state(
            websocket,
            stop_event,
            websocket_url,
            show_wait_for_new_conversation_message=show_wait_for_new_conversation_message,
        )
        show_wait_for_new_conversation_message = True
        input_session.set_prompt(_prompt_for_wait_state(wait_state))
        while True:
            text, wait_state = await _read_next_interactive_text(
                websocket,
                stop_event,
                input_session,
                wait_state,
                websocket_url,
            )
            if text is None:
                return
            command_result = _handle_client_command(text)
            if command_result == _ClientCommandResult.EXIT:
                return
            if command_result == _ClientCommandResult.HANDLED:
                input_session.set_prompt(_prompt_for_wait_state(wait_state))
                continue

            input_session.set_prompt(WAITING_FOR_SERVER_PROMPT)
            await send_user_text(websocket, text, starts_new_conversation=wait_state.starts_new_conversation)
            break


async def _read_next_wait_state(
    websocket,
    stop_event: asyncio.Event,
    websocket_url: str = DEFAULT_WEBSOCKET_URL,
    *,
    show_wait_for_new_conversation_message: bool = True,
) -> WaitState:
    while True:
        message = await _receive_with_liveness(websocket, stop_event, websocket_url)
        wait_state = handle_websocket_message(
            websocket,
            message,
            system_message_printer=_print_client_message,
            show_wait_for_new_conversation_message=show_wait_for_new_conversation_message,
        )
        if wait_state is not None:
            return wait_state


async def _read_next_interactive_text(
    websocket,
    stop_event: asyncio.Event,
    input_session: "_InteractiveInputSession",
    wait_state: WaitState,
    websocket_url: str = DEFAULT_WEBSOCKET_URL,
) -> tuple[str | None, WaitState]:
    while True:
        receive_task = asyncio.create_task(receive_websocket_message(websocket, stop_event))
        line_task = asyncio.create_task(input_session.read_line())

        while True:
            done, pending = await asyncio.wait(
                (receive_task, line_task),
                timeout=WEBSOCKET_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if done:
                break
            await _probe_websocket(websocket, websocket_url)
        await _cancel_tasks(pending)

        if receive_task in done:
            next_wait_state = handle_websocket_message(
                websocket,
                receive_task.result(),
                system_message_printer=_print_client_message,
            )
            if next_wait_state is not None:
                wait_state = next_wait_state
                input_session.set_prompt(_prompt_for_wait_state(wait_state))
            if line_task in done:
                return line_task.result(), wait_state
            continue

        if line_task in done:
            return line_task.result(), wait_state


async def _receive_with_liveness(websocket, stop_event: asyncio.Event, websocket_url: str):
    while True:
        try:
            return await asyncio.wait_for(
                receive_websocket_message(websocket, stop_event),
                timeout=WEBSOCKET_HEARTBEAT_SECONDS,
            )
        except asyncio.TimeoutError:
            await _probe_websocket(websocket, websocket_url)


async def _probe_websocket(websocket, websocket_url: str) -> None:
    if websocket.closed:
        raise WebsocketDisconnected("websocket closed")
    try:
        await websocket.ping()
    except (ClientError, OSError, RuntimeError) as exc:
        raise WebsocketDisconnected("websocket heartbeat failed") from exc
    await _probe_websocket_listener(websocket_url)
    if websocket.closed:
        raise WebsocketDisconnected("websocket closed")


async def _probe_websocket_listener(websocket_url: str) -> None:
    parsed_url = urlparse(websocket_url)
    host = parsed_url.hostname
    if host is None:
        return
    port = parsed_url.port
    if port is None:
        port = 443 if parsed_url.scheme == "wss" else 80

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=parsed_url.scheme == "wss"),
            timeout=1.0,
        )
    except (asyncio.TimeoutError, OSError) as exc:
        raise WebsocketDisconnected("websocket server is unreachable") from exc

    writer.close()
    with suppress(asyncio.TimeoutError, OSError):
        await asyncio.wait_for(writer.wait_closed(), timeout=0.1)
    del reader


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


def _handle_offline_line(line: str | None) -> bool:
    if line is None:
        return True

    command_result = _handle_client_command(line)
    if command_result == _ClientCommandResult.EXIT:
        return True
    if command_result == _ClientCommandResult.HANDLED:
        return False

    if line == "":
        return False

    _print_client_message("Disconnected; message not sent.")
    return False


async def _sleep_or_handle_offline_input(
    input_session: "_InteractiveInputSession",
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
        return _handle_offline_line(line_task.result())
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
        self._prompt = CONNECTING_PROMPT
        self._buffer = b""
        self._fd: int | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._fd = sys.stdin.fileno()
        except OSError:
            self._lines.put_nowait(None)
            return
        os.set_blocking(self._fd, False)
        self._loop.add_reader(self._fd, self._read_ready)

    def set_prompt(self, prompt: str) -> None:
        if prompt == self._prompt:
            return
        self._prompt = prompt
        print(_style_client_prompt(prompt), end="", flush=True)

    async def read_line(self) -> str | None:
        return await self._lines.get()

    def _read_ready(self) -> None:
        assert self._fd is not None
        try:
            data = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            self._close_input()
            return

        if data == b"":
            self._close_input()
            return

        self._buffer += data
        while b"\n" in self._buffer:
            raw_line, self._buffer = self._buffer.split(b"\n", 1)
            line = raw_line.rstrip(b"\r").decode(errors="replace")
            print(CLIENT_TEXT_RESET, end="", flush=True)
            self._lines.put_nowait(line)

    def _current_prompt(self) -> str:
        return self._prompt

    def _close_input(self) -> None:
        if self._fd is not None:
            with suppress(ValueError, RuntimeError):
                self._loop.remove_reader(self._fd)
            self._fd = None
        print(CLIENT_TEXT_RESET, flush=True)
        self._lines.put_nowait(None)


def _prompt_for_wait_state(wait_state: WaitState) -> str:
    if wait_state.starts_new_conversation:
        return WAITING_FOR_NEW_CONVERSATION_PROMPT
    return WAITING_FOR_NEXT_MESSAGE_PROMPT


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
            self._blocking = None
            return

        self._state = termios.tcgetattr(self._fd) if sys.stdin.isatty() else None
        self._blocking = os.get_blocking(self._fd)

    def restore(self) -> None:
        if self._fd is None:
            return
        if self._blocking is not None:
            os.set_blocking(self._fd, self._blocking)
        if self._state is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._state)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    options = ChatClientOptions(
        url=args.url,
        user=args.user,
        area=args.area,
    )
    try:
        asyncio.run(run_chat(options))
    except (ChatInterrupted, KeyboardInterrupt):
        return INTERRUPTED_EXIT_CODE
    except ChatConnectionLost:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
