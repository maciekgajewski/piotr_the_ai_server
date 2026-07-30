import asyncio
from collections import deque
from typing import Any

import pytest

from ai_server.ollama_connection import (
    GPU_RESIDENCY_REQUEST_TIMEOUT_SECONDS,
    OllamaConnection,
    OllamaError,
    OllamaResidencyError,
)
from ai_server.ollama_residency import (
    MODEL_RESIDENCY_CHECK_INTERVAL_SECONDS,
    OllamaResidencySupervisor,
)


class FakeResponse:
    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def json(self) -> dict[str, Any]:
        return self._body


class FakeSession:
    def __init__(self, get_bodies: list[dict[str, Any]] | None = None) -> None:
        self._get_bodies = deque(get_bodies or [])
        self.requests: list[tuple[str, str, dict[str, Any] | None]] = []
        self.timeouts: list[Any] = []
        self.closed = False

    def get(self, url: str, timeout=None):
        self.requests.append(("GET", url, None))
        self.timeouts.append(timeout)
        return FakeResponse(self._get_bodies.popleft())

    def post(self, url: str, json: dict[str, Any], timeout=None):
        self.requests.append(("POST", url, json))
        self.timeouts.append(timeout)
        return FakeResponse({"done": True})

    async def close(self) -> None:
        self.closed = True


def _running_model(name: str, *, size: int = 100, size_vram: int = 100) -> dict[str, Any]:
    return {
        "name": name,
        "model": name,
        "size": size,
        "size_vram": size_vram,
    }


def test_protected_model_requests_are_pinned_without_mutating_caller_payload() -> None:
    async def scenario() -> None:
        session = FakeSession()
        connection = OllamaConnection(
            base_url="http://ollama:11434",
            session=session,
            protected_model="qwen3:14b",
        )
        payload = {
            "model": "qwen3:14b",
            "messages": [],
            "keep_alive": "5m",
        }

        await connection.chat(payload)

        assert payload["keep_alive"] == "5m"
        assert session.requests == [
            (
                "POST",
                "http://ollama:11434/api/chat",
                {
                    "model": "qwen3:14b",
                    "messages": [],
                    "keep_alive": -1,
                },
            )
        ]

    asyncio.run(scenario())


def test_unprotected_model_request_keeps_requested_keep_alive() -> None:
    async def scenario() -> None:
        session = FakeSession()
        connection = OllamaConnection(
            session=session,
            protected_model="qwen3:14b",
        )

        await connection.generate(
            {
                "model": "other:latest",
                "prompt": "",
                "keep_alive": 0,
            }
        )

        assert session.requests[0][2]["keep_alive"] == 0

    asyncio.run(scenario())


def test_healthy_gpu_residency_requires_no_corrective_requests() -> None:
    async def scenario() -> None:
        session = FakeSession(
            [{"models": [_running_model("qwen3:14b")]}]
        )
        connection = OllamaConnection(
            session=session,
            protected_model="qwen3:14b",
        )

        await connection.ensure_protected_model_gpu_residency()

        assert session.requests == [
            ("GET", "http://127.0.0.1:11434/api/ps", None)
        ]
        assert session.timeouts[0].total == GPU_RESIDENCY_REQUEST_TIMEOUT_SECONDS

    asyncio.run(scenario())


def test_partial_residency_evicts_competitors_then_reloads_and_rechecks() -> None:
    async def scenario() -> None:
        session = FakeSession(
            [
                {
                    "models": [
                        _running_model("other:latest", size=50, size_vram=50),
                        _running_model("qwen3:14b", size=100, size_vram=75),
                    ]
                },
                {"models": [_running_model("qwen3:14b")]},
            ]
        )
        connection = OllamaConnection(
            session=session,
            protected_model="qwen3:14b",
        )

        await connection.ensure_protected_model_gpu_residency()

        assert session.requests == [
            ("GET", "http://127.0.0.1:11434/api/ps", None),
            (
                "POST",
                "http://127.0.0.1:11434/api/generate",
                {
                    "model": "other:latest",
                    "stream": False,
                    "keep_alive": 0,
                },
            ),
            (
                "POST",
                "http://127.0.0.1:11434/api/generate",
                {
                    "model": "qwen3:14b",
                    "stream": False,
                    "keep_alive": 0,
                },
            ),
            (
                "POST",
                "http://127.0.0.1:11434/api/generate",
                {
                    "model": "qwen3:14b",
                    "stream": False,
                    "keep_alive": -1,
                },
            ),
            ("GET", "http://127.0.0.1:11434/api/ps", None),
        ]
        assert all(
            timeout.total == GPU_RESIDENCY_REQUEST_TIMEOUT_SECONDS
            for timeout in session.timeouts
        )

    asyncio.run(scenario())


def test_failed_repair_raises_residency_error() -> None:
    async def scenario() -> None:
        session = FakeSession(
            [
                {"models": []},
                {
                    "models": [
                        _running_model("qwen3:14b", size=100, size_vram=90)
                    ]
                },
            ]
        )
        connection = OllamaConnection(
            session=session,
            protected_model="qwen3:14b",
        )

        with pytest.raises(
            OllamaResidencyError,
            match=r"qwen3:14b.*size=100 size_vram=90",
        ):
            await connection.ensure_protected_model_gpu_residency()

    asyncio.run(scenario())


class BlockingResponse(FakeResponse):
    def __init__(
        self,
        body: dict[str, Any],
        entered: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(body)
        self._entered = entered
        self._release = release

    async def __aenter__(self):
        self._entered.set()
        await self._release.wait()
        return self


class GateTestSession(FakeSession):
    def __init__(self) -> None:
        super().__init__([{"models": [_running_model("qwen3:14b")]}])
        self.first_request_entered = asyncio.Event()
        self.release_first_request = asyncio.Event()
        self._post_count = 0

    def post(self, url: str, json: dict[str, Any], timeout=None):
        self.requests.append(("POST", url, json))
        self.timeouts.append(timeout)
        self._post_count += 1
        if self._post_count == 1:
            return BlockingResponse(
                {"done": True},
                self.first_request_entered,
                self.release_first_request,
            )
        return FakeResponse({"done": True})


def test_residency_repair_waits_for_active_requests_and_blocks_new_requests() -> None:
    async def scenario() -> None:
        session = GateTestSession()
        connection = OllamaConnection(
            session=session,
            protected_model="qwen3:14b",
        )
        first_request = asyncio.create_task(
            connection.chat({"model": "qwen3:14b", "messages": []})
        )
        await session.first_request_entered.wait()

        residency_check = asyncio.create_task(
            connection.ensure_protected_model_gpu_residency()
        )
        await asyncio.sleep(0)
        second_request = asyncio.create_task(
            connection.chat({"model": "qwen3:14b", "messages": []})
        )
        await asyncio.sleep(0)

        assert [request[0] for request in session.requests] == ["POST"]

        session.release_first_request.set()
        await asyncio.gather(first_request, residency_check, second_request)

        assert [request[0] for request in session.requests] == [
            "POST",
            "GET",
            "POST",
        ]

    asyncio.run(scenario())


def test_close_is_terminal_for_later_requests() -> None:
    async def scenario() -> None:
        session = FakeSession()
        connection = OllamaConnection()
        connection._session = session

        await connection.close()

        assert session.closed
        assert connection._session is None
        with pytest.raises(OllamaError, match="Ollama connection is closed"):
            await connection.chat({"model": "qwen3:14b", "messages": []})
        assert connection._session is None
        assert session.requests == []

    asyncio.run(scenario())


def test_close_wins_over_request_queued_behind_active_request() -> None:
    async def scenario() -> None:
        session = GateTestSession()
        connection = OllamaConnection(session=session)
        first_request = asyncio.create_task(
            connection.chat({"model": "qwen3:14b", "messages": []})
        )
        await session.first_request_entered.wait()
        close_task = asyncio.create_task(connection.close())
        await asyncio.sleep(0)
        queued_request = asyncio.create_task(
            connection.chat({"model": "qwen3:14b", "messages": []})
        )
        await asyncio.sleep(0)

        session.release_first_request.set()
        await asyncio.gather(first_request, close_task)

        with pytest.raises(OllamaError, match="Ollama connection is closed"):
            await queued_request
        assert [request[0] for request in session.requests] == ["POST"]
        assert connection._request_gate._active_requests == 0

    asyncio.run(scenario())


def test_residency_supervisor_checks_at_startup_and_every_sixty_seconds() -> None:
    class StopSupervisor(Exception):
        pass

    class FakeConnection:
        protected_model = "qwen3:14b"

        def __init__(self) -> None:
            self.check_count = 0

        async def ensure_protected_model_gpu_residency(self) -> None:
            self.check_count += 1

    async def scenario() -> None:
        connection = FakeConnection()
        sleeps: list[float] = []

        async def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise StopSupervisor

        supervisor = OllamaResidencySupervisor(connection, sleep=sleep)
        await supervisor.verify_startup()
        with pytest.raises(StopSupervisor):
            await supervisor.run()

        assert connection.check_count == 2
        assert sleeps == [
            MODEL_RESIDENCY_CHECK_INTERVAL_SECONDS,
            MODEL_RESIDENCY_CHECK_INTERVAL_SECONDS,
        ]
        assert MODEL_RESIDENCY_CHECK_INTERVAL_SECONDS == 60.0

    asyncio.run(scenario())
