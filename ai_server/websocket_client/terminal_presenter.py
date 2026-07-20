from __future__ import annotations

import asyncio
import os
import sys
from contextlib import suppress
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style

from ai_server.websocket_client.clock import SystemClock
from ai_server.websocket_client.interfaces import Clock
from ai_server.websocket_client.messages import InputKind, InputResult, InputResultKind

CLIENT_TEXT_STYLE = "\033[3;90m"
CLIENT_TEXT_RESET = "\033[0m"
CHAT_HISTORY_ENV_VAR = "PIOTR_CHAT_HISTORY"
CHAT_HISTORY_LENGTH = 1000


class TerminalPresenter:
    is_batch = False
    has_pending_messages = False

    def __init__(self, clock: Clock | None = None) -> None:
        self._session: PromptSession | None = None
        self._patch = None
        self._input_task: asyncio.Task | None = None
        self._prompt_started: asyncio.Event | None = None
        self._history_path: Path | None = None
        self._presented_at: float | None = None
        self._clock = clock or SystemClock()

    @staticmethod
    def validate_tty() -> bool:
        return sys.stdin.isatty() and sys.stdout.isatty()

    async def start(self) -> None:
        history_path = _chat_history_path()
        self._history_path = history_path
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            os.chmod(history_path.parent, 0o700)
        _trim_history(history_path)
        history_path.touch(exist_ok=True)
        with suppress(OSError):
            os.chmod(history_path, 0o600)
        self._session = PromptSession(
            history=FileHistory(str(history_path)),
            style=Style.from_dict({"prompt": "ansibrightblack"}),
        )
        self._patch = patch_stdout(raw=True)
        self._patch.__enter__()

    async def present_input(self, kind: InputKind) -> float:
        # The editor's first after_render callback is the public
        # prompt_toolkit boundary at which this offer has actually been drawn.
        if self._input_task is not None and not self._input_task.done():
            self._input_task.cancel()
        self._prompt_started = asyncio.Event()
        self._presented_at = None
        self._input_task = asyncio.create_task(self._read_prompt(kind, self._prompt_started))
        prompt_started_task = asyncio.create_task(self._prompt_started.wait())
        try:
            done, _ = await asyncio.wait({self._input_task, prompt_started_task}, return_when=asyncio.FIRST_COMPLETED)
            if self._input_task in done:
                self._input_task.result()
                return self._clock.monotonic()
            assert prompt_started_task in done
            assert self._presented_at is not None
            return self._presented_at
        finally:
            if not prompt_started_task.done():
                prompt_started_task.cancel()
            with suppress(asyncio.CancelledError):
                await prompt_started_task

    async def read_input(self, kind: InputKind) -> InputResult:
        assert self._input_task is not None
        return await self._input_task

    async def _read_prompt(self, kind: InputKind, prompt_started: asyncio.Event) -> InputResult:
        assert self._session is not None
        label = "waiting for new conversation> " if kind is InputKind.START else "waiting for next message> "
        try:
            def mark_prompt_visible(app) -> None:
                self._presented_at = self._clock.monotonic()
                prompt_started.set()
                self._session.app.after_render -= mark_prompt_visible

            self._session.app.after_render += mark_prompt_visible
            text = await self._session.prompt_async([("class:prompt", label)], handle_sigint=False)
        except EOFError:
            return InputResult(InputResultKind.EOF, None, self._clock.monotonic())
        finally:
            with suppress(ValueError):
                self._session.app.after_render -= mark_prompt_visible
        if self._history_path is not None:
            _trim_history(self._history_path)
        submitted_at = self._clock.monotonic()
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
        if self._input_task is not None and not self._input_task.done():
            self._input_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._input_task
        self._input_task = None

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
        try:
            if self._patch is not None:
                self._patch.__exit__(None, None, None)
                self._patch = None
            if self._history_path is not None:
                _trim_history(self._history_path)
                with suppress(OSError):
                    os.chmod(self._history_path, 0o600)
        finally:
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


def _trim_history(path: Path) -> None:
    """Keep complete prompt-toolkit FileHistory records bounded and private."""
    if not path.exists():
        return
    with suppress(OSError):
        records: list[list[str]] = []
        record: list[str] = []
        for line in path.read_text().splitlines():
            if line.startswith("+"):
                record.append(line[1:])
            elif record:
                if "\n".join(record).strip():
                    records.append(record)
                record = []
        if record and "\n".join(record).strip():
            records.append(record)
        records = records[-CHAT_HISTORY_LENGTH:]
        path.write_text("".join("\n# retained\n" + "".join(f"+{line}\n" for line in record) for record in records))
