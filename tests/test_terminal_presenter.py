from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from prompt_toolkit.utils import Event
from prompt_toolkit.history import FileHistory

from ai_server.websocket_client import terminal_presenter
from ai_server.websocket_client.messages import InputKind
from ai_server.websocket_client.terminal_presenter import CHAT_HISTORY_LENGTH, TerminalPresenter, _chat_history_path, _trim_history


class _PromptSession:
    def __init__(self) -> None:
        self.prompt_rendered = asyncio.Event()
        self.release_input = asyncio.Event()
        self.app = type("_Application", (), {"after_render": Event(self)})()

    async def prompt_async(self, message, **kwargs):
        self.app.after_render()
        self.prompt_rendered.set()
        await self.release_input.wait()
        return "hello"


def test_present_input_commits_only_after_prompt_toolkit_renders() -> None:
    async def run() -> None:
        presenter = TerminalPresenter()
        session = _PromptSession()
        presenter._session = session
        presented_at = await presenter.present_input(InputKind.START)
        assert presented_at > 0
        assert session.prompt_rendered.is_set()
        assert presenter._input_task is not None and not presenter._input_task.done()
        await presenter.cancel_input()

    asyncio.run(run())


def test_terminal_presenter_uses_its_injected_clock_for_presentation_and_submission() -> None:
    class _Clock:
        now = 10.0

        def monotonic(self) -> float:
            return self.now

        async def sleep_until(self, deadline: float) -> None:
            await asyncio.Future()

    async def run() -> None:
        clock = _Clock()
        presenter = TerminalPresenter(clock)
        session = _PromptSession()
        presenter._session = session
        assert await presenter.present_input(InputKind.START) == 10.0
        clock.now = 12.0
        session.release_input.set()
        result = await presenter.read_input(InputKind.START)
        assert result.submitted_at == 12.0

    asyncio.run(run())


def test_present_input_propagates_prompt_initialization_failure() -> None:
    class _FailingSession:
        app = type("_Application", (), {"after_render": Event(object())})()

        async def prompt_async(self, message, **kwargs):
            raise OSError("terminal unavailable")

    async def run() -> None:
        presenter = TerminalPresenter()
        presenter._session = _FailingSession()
        with pytest.raises(OSError, match="terminal unavailable"):
            await presenter.present_input(InputKind.START)

    asyncio.run(run())


def test_cancelling_presentation_joins_its_render_waiter() -> None:
    class _NoRenderSession:
        def __init__(self) -> None:
            self.prompt_started = asyncio.Event()
            self.app = type("_Application", (), {"after_render": Event(self)})()

        async def prompt_async(self, message, **kwargs):
            self.prompt_started.set()
            await asyncio.Future()

    async def run() -> None:
        presenter = TerminalPresenter()
        session = _NoRenderSession()
        presenter._session = session
        presentation_task = asyncio.create_task(presenter.present_input(InputKind.START))
        await session.prompt_started.wait()
        presentation_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await presentation_task
        await presenter.cancel_input()
        await asyncio.sleep(0)
        assert not [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done() and task.get_coro().__qualname__ == "Event.wait"
        ]

    asyncio.run(run())


def test_trim_history_excludes_blank_entries_and_keeps_last_thousand(tmp_path: Path) -> None:
    path = tmp_path / "history"
    records = ["\n# entry\n+   \n"] + [f"\n# entry\n+message-{index}\n" for index in range(CHAT_HISTORY_LENGTH + 2)]
    path.write_text("".join(records))
    _trim_history(path)
    kept = list(FileHistory(str(path)).load_history_strings())
    assert len(kept) == CHAT_HISTORY_LENGTH
    assert kept[0] == f"message-{CHAT_HISTORY_LENGTH + 1}"
    assert kept[-1] == "message-2"


def test_history_path_honors_explicit_and_xdg_locations(monkeypatch, tmp_path: Path) -> None:
    explicit = tmp_path / "private-history"
    monkeypatch.setenv("PIOTR_CHAT_HISTORY", str(explicit))
    assert _chat_history_path() == explicit
    monkeypatch.delenv("PIOTR_CHAT_HISTORY")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert _chat_history_path() == tmp_path / "state" / "piotr" / "chat_client_history"


def test_start_creates_a_private_configured_history_file(monkeypatch, tmp_path: Path) -> None:
    class _Patch:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

    history_path = tmp_path / "history" / "chat"
    monkeypatch.setenv("PIOTR_CHAT_HISTORY", str(history_path))
    monkeypatch.setattr(terminal_presenter, "patch_stdout", lambda raw: _Patch())

    async def run() -> None:
        presenter = TerminalPresenter()
        await presenter.start()
        await presenter.close()

    asyncio.run(run())
    assert history_path.exists()
    assert history_path.stat().st_mode & 0o077 == 0


def test_system_and_assistant_text_have_distinct_semantic_output(capsys) -> None:
    presenter = TerminalPresenter()
    presenter.system("status")
    presenter.assistant_text("assistant")
    output = capsys.readouterr().out
    assert output.startswith("\033[3;90mstatus\033[0m")
    assert output.endswith("assistant")


def test_close_emits_reset_when_patch_teardown_fails(monkeypatch) -> None:
    class _BrokenPatch:
        def __exit__(self, exc_type, exc, traceback):
            raise OSError("teardown failed")

    class _Output:
        def __init__(self) -> None:
            self.written = ""
            self.flushed = False

        def write(self, text: str) -> None:
            self.written += text

        def flush(self) -> None:
            self.flushed = True

    output = _Output()
    monkeypatch.setattr(terminal_presenter.sys, "__stdout__", output)

    async def run() -> None:
        presenter = TerminalPresenter()
        presenter._patch = _BrokenPatch()
        with pytest.raises(OSError, match="teardown failed"):
            await presenter.close()

    asyncio.run(run())
    assert output.written == "\033[0m"
    assert output.flushed
