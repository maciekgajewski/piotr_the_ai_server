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

from ai_server.ws_client_common import DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS, DEFAULT_WEBSOCKET_URL
from ai_server.ws_client_common import INTERRUPTED_EXIT_CODE, WebsocketDisconnected
from ai_server.ws_client_common import ConversationTerminated
from ai_server.ws_client_common import WEBSOCKET_HEARTBEAT_SECONDS
from ai_server.ws_client_common import WebsocketSessionRejected
from ai_server.ws_client_common import WaitState, WsClientInterrupted
from ai_server.ws_client_common import handle_websocket_message, receive_websocket_message, send_session_start
from ai_server.ws_client_common import send_follow_up_timed_out, send_user_text, validate_follow_up_timeout

WAITING_FOR_NEW_CONVERSATION_PROMPT = "waiting for new conversation> "
WAITING_FOR_NEXT_MESSAGE_PROMPT = "waiting for next message> "
WAITING_FOR_SERVER_PROMPT = "waiting for server> "
CONNECTING_PROMPT = "connecting> "
CONNECT_TIMEOUT_SECONDS = 5.0
WEBSOCKET_LIVENESS_CHECK_SECONDS = 2.0
WEBSOCKET_LISTENER_PROBE_TIMEOUT_SECONDS = 1.0
WEBSOCKET_LISTENER_CLOSE_TIMEOUT_SECONDS = 0.1
WEBSOCKET_CLOSE_TIMEOUT_SECONDS = 1.0
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


class ChatConnectionFailed(Exception):
    """Raised when the chat websocket connection cannot be established."""


class _ClientCommandResult:
    NOT_COMMAND = "not_command"
    HANDLED = "handled"
    EXIT = "exit"


@dataclass(frozen=True)
class ChatClientOptions:
    url: str
    user: str | None
    area: str | None
    follow_up_timeout_seconds: float


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with the AI server over websocket.")
    parser.add_argument("--user", help="Optional session user attribute sent in the websocket handshake.")
    parser.add_argument("--area", help="Optional Home Assistant area attribute sent in the websocket handshake.")
    parser.add_argument(
        "--follow-up-timeout-seconds",
        default=DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
        type=float,
        help=f"Follow-up timeout in seconds. Defaults to {DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS:g}.",
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
    async with ClientSession() as session:
        input_session.set_prompt(CONNECTING_PROMPT)
        try:
            websocket = await _connect_interactive(session, options, input_session, stop_event)
        except ChatExited:
            return
        except (asyncio.TimeoutError, ClientError, OSError) as exc:
            _print_client_message(f"Connection failed: {exc}.")
            raise ChatConnectionFailed() from exc

        try:
            input_session.set_prompt(WAITING_FOR_SERVER_PROMPT)
            await send_session_start(websocket, options.user, options.area)
            await _run_interactive_connection(
                websocket,
                input_session,
                stop_event,
                options.follow_up_timeout_seconds,
                options.url,
            )
            return
        except WebsocketSessionRejected as exc:
            _print_client_message(f"Connection rejected: {exc}.")
            return
        except (WebsocketDisconnected, ClientError, OSError) as exc:
            _print_client_message(f"Connection lost: {exc}.")
            raise ChatConnectionLost() from exc
        finally:
            await _close_websocket(websocket)


async def _connect_interactive(
    session: ClientSession,
    options: ChatClientOptions,
    input_session: "_InteractiveInputSession",
    stop_event: asyncio.Event,
):
    _print_client_message(f"Connecting to {options.url} ...")
    connect_task = asyncio.create_task(
        asyncio.wait_for(
            session.ws_connect(options.url, heartbeat=WEBSOCKET_HEARTBEAT_SECONDS),
            CONNECT_TIMEOUT_SECONDS,
        )
    )
    line_task = asyncio.create_task(input_session.read_line())
    stop_task = asyncio.create_task(stop_event.wait())

    try:
        while True:
            done, pending = await asyncio.wait(
                (connect_task, line_task, stop_task),
                return_when=asyncio.FIRST_COMPLETED,
            )

            if stop_task in done:
                raise ChatInterrupted()

            if connect_task in done:
                if line_task in done and _handle_offline_line(line_task.result()):
                    with suppress(Exception):
                        websocket = connect_task.result()
                        await _close_websocket(websocket)
                    raise ChatExited()
                return connect_task.result()

            assert line_task in done
            if _handle_offline_line(line_task.result()):
                raise ChatExited()
            line_task = asyncio.create_task(input_session.read_line())
    finally:
        await _cancel_tasks(tuple(task for task in (connect_task, line_task, stop_task) if not task.done()))


async def _run_interactive_connection(
    websocket,
    input_session: "_InteractiveInputSession",
    stop_event: asyncio.Event,
    follow_up_timeout_seconds: float,
    websocket_url: str = DEFAULT_WEBSOCKET_URL,
) -> None:
    show_wait_for_new_conversation_message = False
    while True:
        wait_state = await _read_next_wait_state(
            websocket,
            stop_event,
            follow_up_timeout_seconds,
            websocket_url,
            show_wait_for_new_conversation_message=show_wait_for_new_conversation_message,
        )
        show_wait_for_new_conversation_message = True
        input_session.set_prompt(_prompt_for_wait_state(wait_state))
        while True:
            text, wait_state, follow_up_timed_out = await _read_next_interactive_text(
                websocket,
                stop_event,
                input_session,
                wait_state,
                follow_up_timeout_seconds,
                websocket_url,
            )
            if follow_up_timed_out:
                input_session.set_prompt(WAITING_FOR_SERVER_PROMPT)
                break
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
    follow_up_timeout_seconds: float,
    websocket_url: str = DEFAULT_WEBSOCKET_URL,
    *,
    show_wait_for_new_conversation_message: bool = True,
) -> WaitState:
    while True:
        message = await _receive_with_liveness(websocket, stop_event, websocket_url)
        wait_state = handle_websocket_message(
            websocket,
            message,
            assistant_message_started_handler=_reset_terminal_style,
            system_message_printer=_print_client_message,
            show_wait_for_new_conversation_message=show_wait_for_new_conversation_message,
            follow_up_timeout_seconds=follow_up_timeout_seconds,
        )
        if isinstance(wait_state, WaitState):
            return wait_state


async def _read_next_interactive_text(
    websocket,
    stop_event: asyncio.Event,
    input_session: "_InteractiveInputSession",
    wait_state: WaitState,
    follow_up_timeout_seconds: float,
    websocket_url: str = DEFAULT_WEBSOCKET_URL,
) -> tuple[str | None, WaitState, bool]:
    receive_task = asyncio.create_task(receive_websocket_message(websocket, stop_event))
    line_task = asyncio.create_task(input_session.read_line())
    timeout_task = (
        asyncio.create_task(asyncio.sleep(wait_state.timeout_seconds))
        if wait_state.follow_up_requested and wait_state.timeout_seconds is not None
        else None
    )
    try:
        while True:
            tasks = (receive_task, line_task) + ((timeout_task,) if timeout_task is not None else ())
            done, pending = await asyncio.wait(
                tasks,
                timeout=WEBSOCKET_LIVENESS_CHECK_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await _probe_websocket(websocket, websocket_url)
                continue

            if receive_task in done:
                next_wait_state = handle_websocket_message(
                    websocket,
                    _receive_task_result(receive_task),
                    assistant_message_started_handler=_reset_terminal_style,
                    system_message_printer=_print_client_message,
                    follow_up_timeout_seconds=follow_up_timeout_seconds,
                )
                if isinstance(next_wait_state, ConversationTerminated):
                    return None, wait_state, True
                if isinstance(next_wait_state, WaitState):
                    wait_state = next_wait_state
                    input_session.set_prompt(_prompt_for_wait_state(wait_state))
                    if timeout_task is not None:
                        timeout_task.cancel()
                    timeout_task = (
                        asyncio.create_task(asyncio.sleep(wait_state.timeout_seconds))
                        if wait_state.follow_up_requested and wait_state.timeout_seconds is not None
                        else None
                    )
                if line_task in done:
                    return line_task.result(), wait_state, False
                if timeout_task is not None and timeout_task in done:
                    await send_follow_up_timed_out(websocket)
                    return None, wait_state, True
                receive_task = asyncio.create_task(receive_websocket_message(websocket, stop_event))
                continue

            if timeout_task is not None and timeout_task in done and line_task not in done:
                await send_follow_up_timed_out(websocket)
                return None, wait_state, True

            assert line_task in done
            return line_task.result(), wait_state, False
    finally:
        await _cancel_tasks(tuple(task for task in (receive_task, line_task, timeout_task) if task is not None and not task.done()))


async def _receive_with_liveness(websocket, stop_event: asyncio.Event, websocket_url: str):
    receive_task = asyncio.create_task(receive_websocket_message(websocket, stop_event))
    try:
        while True:
            done, _pending = await asyncio.wait(
                (receive_task,),
                timeout=WEBSOCKET_LIVENESS_CHECK_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receive_task in done:
                return _receive_task_result(receive_task)
            await _probe_websocket(websocket, websocket_url)
    finally:
        await _cancel_tasks((receive_task,) if not receive_task.done() else ())


def _receive_task_result(receive_task: asyncio.Task):
    try:
        return receive_task.result()
    except WsClientInterrupted as exc:
        raise ChatInterrupted() from exc


async def _probe_websocket(websocket, websocket_url: str) -> None:
    if websocket.closed:
        raise WebsocketDisconnected("websocket closed")
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
            timeout=WEBSOCKET_LISTENER_PROBE_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, OSError) as exc:
        raise WebsocketDisconnected("websocket server is unreachable") from exc

    writer.close()
    with suppress(asyncio.TimeoutError, OSError):
        await asyncio.wait_for(writer.wait_closed(), timeout=WEBSOCKET_LISTENER_CLOSE_TIMEOUT_SECONDS)
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


async def _cancel_tasks(tasks) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    for task in tasks:
        with suppress(asyncio.CancelledError, Exception):
            await task


async def _close_websocket(websocket) -> None:
    if websocket.closed:
        return
    with suppress(asyncio.TimeoutError, Exception):
        await asyncio.wait_for(websocket.close(), timeout=WEBSOCKET_CLOSE_TIMEOUT_SECONDS)


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


def _reset_terminal_style() -> None:
    print(CLIENT_TEXT_RESET, end="", flush=True)


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
        follow_up_timeout_seconds=validate_follow_up_timeout(args.follow_up_timeout_seconds),
    )
    try:
        asyncio.run(run_chat(options))
    except (ChatInterrupted, KeyboardInterrupt):
        return INTERRUPTED_EXIT_CODE
    except (ChatConnectionFailed, ChatConnectionLost):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
