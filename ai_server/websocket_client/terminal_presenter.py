from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import suppress
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from ai_server.websocket_client.messages import InputKind, InputResult, InputResultKind

CLIENT_TEXT_STYLE = "\033[3;90m"
CLIENT_TEXT_RESET = "\033[0m"
CHAT_HISTORY_ENV_VAR = "PIOTR_CHAT_HISTORY"
CHAT_HISTORY_LENGTH = 1000


class TerminalPresenter:
    is_batch = False

    def __init__(self) -> None:
        self._session: PromptSession | None = None
        self._patch = None
        self._input_task: asyncio.Task | None = None

    @staticmethod
    def validate_tty() -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    async def start(self) -> None:
        history_path = _chat_history_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(history_path.parent, 0o700)
        self._session = PromptSession(
            history=FileHistory(str(history_path)),
            style=Style.from_dict({"prompt": "ansibrightblack"}),
        )
        self._patch = patch_stdout(raw=True)
        self._patch.__enter__()

    async def present_input(self, kind: InputKind) -> float:
        return time.monotonic()

    async def read_input(self, kind: InputKind) -> InputResult:
        assert self._session is not None
        label = "waiting for new conversation> " if kind is InputKind.START else "waiting for next message> "
        try:
            text = await self._session.prompt_async([( "class:prompt", label)])
        except EOFError:
            return InputResult(InputResultKind.EOF, None, time.monotonic())
        submitted_at = time.monotonic()
        stripped = text.strip()
        if not stripped:
            return InputResult(InputResultKind.EMPTY, None, submitted_at)
        if stripped == "/help":
            return InputResult(InputResultKind.HELP, None, submitted_at)
        if stripped == "/exit":
            return InputResult(InputResultKind.EXIT, None, submitted_at)
        if stripped.startswith("/"):
            return InputResult(InputResultKind.UNKNOWN_COMMAND, stripped, submitted_at)
        return InputResult(InputResultKind.TEXT, text, submitted_at)

    async def cancel_input(self) -> None:
        return None

    def system(self, text: str) -> None:
        print(f"{CLIENT_TEXT_STYLE}{text}{CLIENT_TEXT_RESET}", flush=True)

    def assistant_started(self) -> None:
        return None

    def assistant_text(self, text: str) -> None:
        print(text, end="", flush=True)

    def assistant_finished(self, aborted_reason: str | None = None) -> None:
        print(flush=True)
        if aborted_reason is not None:
            self.system(f"Assistant message aborted: {aborted_reason}.")

    async def close(self) -> None:
        if self._patch is not None:
            self._patch.__exit__(None, None, None)
            self._patch = None
        # This is intentionally the last style-affecting output on every TTY path.
        real_stdout = getattr(sys, "__stdout__", sys.stdout)
        real_stdout.write(CLIENT_TEXT_RESET)
        real_stdout.flush()


def _chat_history_path() -> Path:
    configured_path = os.environ.get(CHAT_HISTORY_ENV_VAR)
    if configured_path:
        return Path(configured_path).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).expanduser() / "piotr" / "chat_client_history"
    return Path.home() / ".local" / "state" / "piotr" / "chat_client_history"
