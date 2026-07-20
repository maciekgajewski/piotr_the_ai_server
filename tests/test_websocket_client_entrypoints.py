from __future__ import annotations

import asyncio
from unittest.mock import patch

from ai_server import chat_client
from ai_server.websocket_client.messages import ExitKind, ExitResult


def test_interactive_terminal_startup_failure_is_presented_before_cleanup() -> None:
    presenters = []

    class _FailingPresenter:
        def __init__(self, clock) -> None:
            self.messages = []
            self.closed = False
            presenters.append(self)

        @staticmethod
        def validate_tty() -> bool:
            return True

        async def start(self) -> None:
            raise OSError("cannot initialize terminal")

        def system(self, text: str) -> None:
            self.messages.append(text)

        async def close(self) -> None:
            self.closed = True

    async def run() -> None:
        with patch.object(chat_client, "TerminalPresenter", _FailingPresenter):
            result = await chat_client.run_chat(chat_client.ChatClientOptions("ws://unused", None, None, 15))
        assert result.kind is ExitKind.LOCAL_FAILURE

    asyncio.run(run())
    assert presenters[0].messages == ["local failure: cannot initialize terminal"]
    assert presenters[0].closed


def test_interactive_startup_failure_remains_typed_when_close_fails() -> None:
    class _FailingPresenter:
        def __init__(self, clock) -> None:
            return None

        @staticmethod
        def validate_tty() -> bool:
            return True

        async def start(self) -> None:
            raise OSError("startup failed")

        def system(self, text: str) -> None:
            return None

        async def close(self) -> None:
            raise OSError("teardown failed")

    async def run() -> None:
        with patch.object(chat_client, "TerminalPresenter", _FailingPresenter):
            result = await chat_client.run_chat(chat_client.ChatClientOptions("ws://unused", None, None, 15))
        assert result.kind is ExitKind.LOCAL_FAILURE
        assert result.detail == "startup failed"

    asyncio.run(run())


def test_interactive_success_becomes_local_failure_when_close_fails() -> None:
    class _Presenter:
        def __init__(self, clock) -> None:
            return None

        @staticmethod
        def validate_tty() -> bool:
            return True

        async def start(self) -> None:
            return None

        def system(self, text: str) -> None:
            return None

        async def close(self) -> None:
            raise OSError("teardown failed")

    class _Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class _Engine:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def run(self) -> ExitResult:
            return ExitResult(ExitKind.COMPLETED)

    async def connect(*args, **kwargs):
        return _Context()

    async def run() -> None:
        with patch.object(chat_client, "TerminalPresenter", _Presenter), \
             patch.object(chat_client, "ClientSession", _Context), \
             patch.object(chat_client, "connect", connect), \
             patch.object(chat_client, "ClientEngine", _Engine):
            result = await chat_client.run_chat(chat_client.ChatClientOptions("ws://unused", None, None, 15))
        assert result.kind is ExitKind.LOCAL_FAILURE
        assert result.detail == "teardown failed"

    asyncio.run(run())


def test_interactive_engine_diagnostic_output_failure_is_typed_local_failure() -> None:
    class _Presenter:
        def __init__(self, clock) -> None:
            return None

        @staticmethod
        def validate_tty() -> bool:
            return True

        async def start(self) -> None:
            return None

        def system(self, text: str) -> None:
            if text.startswith("rejected:"):
                raise OSError("diagnostic output failed")

        async def close(self) -> None:
            return None

    class _Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

    class _Engine:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def run(self) -> ExitResult:
            return ExitResult(ExitKind.REJECTED, "server denied session")

    async def connect(*args, **kwargs):
        return _Context()

    async def run() -> None:
        with patch.object(chat_client, "TerminalPresenter", _Presenter), \
             patch.object(chat_client, "ClientSession", _Context), \
             patch.object(chat_client, "connect", connect), \
             patch.object(chat_client, "ClientEngine", _Engine):
            result = await chat_client.run_chat(chat_client.ChatClientOptions("ws://unused", None, None, 15))
        assert result.kind is ExitKind.LOCAL_FAILURE
        assert result.detail == "diagnostic output failed"

    asyncio.run(run())
