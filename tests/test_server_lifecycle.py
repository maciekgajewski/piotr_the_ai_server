import asyncio
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import ClientConnectorError, ClientSession, WSMsgType

import ai_server.server as server
from ai_server.config import load_config_from_yaml
from ai_server.ollama_connection import OllamaResidencyError
from ai_server.websocket_messages import SessionAccepted, SessionStart, client_event_to_json
from ai_server.websocket_messages import server_event_from_json


ROOT = Path(__file__).resolve().parents[1]


def test_real_fatal_termination_controller_exits_process_nonzero() -> None:
    script = """
import asyncio
from ai_server.server import _ProcessFatalTerminationController

asyncio.run(_ProcessFatalTerminationController().terminate("test fatal containment"))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode != 0


def _unused_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_config(
    tmp_path: Path,
    *,
    grace_period: float = 2.0,
    local_model: str | None = None,
) -> tuple[Path, int]:
    port = _unused_port()
    config = tmp_path / "server-lifecycle.yaml"
    local_model_line = f"  local_model: {local_model}\n" if local_model is not None else ""
    config.write_text(
        f"""
websocket:
  host: 127.0.0.1
  port: {port}
  max_connections: 2
  capacity_retry_after_seconds: 1
  follow_up_idle_lease_seconds: 10
  max_frame_bytes: 4096
  ingress_queue_capacity: 4
  heartbeat_seconds: 1
  handshake_timeout_seconds: 1
conversation:
  agent_cancellation_deadline_seconds: 1
  fatal_notification_seconds: 0.1
shutdown:
  grace_period_seconds: {grace_period}
agent:
  type: echo
{local_model_line}cache_dir: {tmp_path / 'cache'}
data_dir: {tmp_path / 'data'}
""",
        encoding="utf-8",
    )
    return config, port


def _start_server(config: Path, *, hanging_close: bool = False) -> subprocess.Popen:
    if hanging_close:
        script = f"""
import asyncio
import ai_server.server as server
from ai_server.agent.echo import EchoAgent
from ai_server.config import load_config_from_yaml

class HangingAgent(EchoAgent):
    async def close(self):
        await asyncio.Event().wait()

async def create_agent(*args, **kwargs):
    return HangingAgent()

server.create_agent = create_agent
asyncio.run(server.run_server(load_config_from_yaml({str(config)!r}), "http://127.0.0.1:11434"))
"""
        command = [sys.executable, "-c", script]
    else:
        command = [sys.executable, "-m", "ai_server.server", "--config", str(config)]
    return subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _wait_until_ready(port: int) -> None:
    async with ClientSession() as client:
        for _ in range(200):
            try:
                async with client.get(f"http://127.0.0.1:{port}/api/status") as response:
                    if response.status == 200:
                        return
            except ClientConnectorError:
                pass
            await asyncio.sleep(0.01)
    raise AssertionError("server did not become ready")


def test_first_signal_closes_active_input_session_and_exits_zero(tmp_path: Path) -> None:
    async def scenario() -> None:
        config, port = _write_config(tmp_path)
        process = _start_server(config)
        try:
            await _wait_until_ready(port)
            async with ClientSession() as client:
                websocket = await client.ws_connect(f"ws://127.0.0.1:{port}/chat")
                await websocket.send_str(client_event_to_json(SessionStart()))
                accepted = await websocket.receive(timeout=2)
                assert server_event_from_json(accepted.data) == SessionAccepted()
                process.send_signal(signal.SIGTERM)
                while True:
                    message = await websocket.receive(timeout=2)
                    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
                        break
                assert websocket.close_code == 1001
                assert message.extra == "server shutting down"
            returncode = await asyncio.to_thread(process.wait, 5)
            assert returncode == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    asyncio.run(scenario())


def test_shutdown_deadline_hard_exits_nonzero(tmp_path: Path) -> None:
    async def scenario() -> None:
        config, port = _write_config(tmp_path, grace_period=0.05)
        process = _start_server(config, hanging_close=True)
        try:
            await _wait_until_ready(port)
            process.send_signal(signal.SIGTERM)
            returncode = await asyncio.to_thread(process.wait, 5)
            assert returncode != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    asyncio.run(scenario())


def test_second_signal_during_shutdown_hard_exits_nonzero(tmp_path: Path) -> None:
    async def scenario() -> None:
        config, port = _write_config(tmp_path, grace_period=5)
        process = _start_server(config, hanging_close=True)
        try:
            await _wait_until_ready(port)
            process.send_signal(signal.SIGTERM)
            await asyncio.sleep(0.05)
            assert process.poll() is None
            process.send_signal(signal.SIGINT)
            returncode = await asyncio.to_thread(process.wait, 5)
            assert returncode != 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    asyncio.run(scenario())


class _FakeOllamaConnection:
    def __init__(self, *, base_url: str, protected_model: str | None) -> None:
        self.base_url = base_url
        self.protected_model = protected_model
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeAgent:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeMicrophoneManager:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _FakeRunner:
    def __init__(self, app) -> None:
        self.app = app
        self.setup_called = False
        self.cleanup_called = False

    async def setup(self) -> None:
        self.setup_called = True

    async def cleanup(self) -> None:
        self.cleanup_called = True


class _FakeSite:
    def __init__(self, runner, host, port) -> None:
        self.runner = runner
        self.host = host
        self.port = port
        self.started = False

    async def start(self) -> None:
        self.started = True


def _patch_runtime_dependencies(monkeypatch):
    created: dict[str, object] = {}

    def create_ollama_connection(*, base_url, protected_model):
        connection = _FakeOllamaConnection(
            base_url=base_url,
            protected_model=protected_model,
        )
        created["ollama_connection"] = connection
        return connection

    async def create_agent(config, ollama_connection, **kwargs):
        assert ollama_connection is created["ollama_connection"]
        agent = _FakeAgent()
        created["agent"] = agent
        return agent

    async def init_mics(*args, **kwargs):
        microphone_manager = _FakeMicrophoneManager()
        created["microphone_manager"] = microphone_manager
        return microphone_manager

    def create_runner(app):
        runner = _FakeRunner(app)
        created["runner"] = runner
        return runner

    def create_site(runner, host, port):
        site = _FakeSite(runner, host, port)
        created["site"] = site
        return site

    monkeypatch.setattr(server, "OllamaConnection", create_ollama_connection)
    monkeypatch.setattr(server, "create_agent", create_agent)
    monkeypatch.setattr(server, "init_mics", init_mics)
    monkeypatch.setattr(server, "create_app", lambda *args, **kwargs: object())
    monkeypatch.setattr(server.web, "AppRunner", create_runner)
    monkeypatch.setattr(server.web, "TCPSite", create_site)
    return created


def test_startup_residency_failure_prevents_websocket_admission(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FailingStartupSupervisor:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def verify_startup(self) -> None:
            raise OllamaResidencyError("startup residency failed")

        async def run(self) -> None:
            raise AssertionError("periodic supervisor must not start")

    config_path, _port = _write_config(tmp_path, local_model="qwen3:14b")
    created = _patch_runtime_dependencies(monkeypatch)
    monkeypatch.setattr(server, "OllamaResidencySupervisor", FailingStartupSupervisor)

    with pytest.raises(OllamaResidencyError, match="startup residency failed"):
        asyncio.run(
            server.run_server(
                load_config_from_yaml(config_path),
                "http://ollama:11434",
            )
        )

    assert "runner" not in created
    assert "microphone_manager" not in created
    assert created["agent"].closed
    assert created["ollama_connection"].closed


def test_periodic_residency_failure_stops_server_and_closes_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    created = _patch_runtime_dependencies(monkeypatch)

    class FailingPeriodicSupervisor:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def verify_startup(self) -> None:
            pass

        async def run(self) -> None:
            while "site" not in created or not created["site"].started:
                await asyncio.sleep(0)
            raise OllamaResidencyError("periodic residency failed")

    config_path, _port = _write_config(tmp_path, local_model="qwen3:14b")
    monkeypatch.setattr(server, "OllamaResidencySupervisor", FailingPeriodicSupervisor)

    with pytest.raises(OllamaResidencyError, match="periodic residency failed"):
        asyncio.run(
            server.run_server(
                load_config_from_yaml(config_path),
                "http://ollama:11434",
            )
        )

    assert created["site"].started
    assert created["runner"].cleanup_called
    assert created["microphone_manager"].closed
    assert created["agent"].closed
    assert created["ollama_connection"].closed


def test_periodic_residency_failure_cancels_suspended_microphone_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    microphone_starting = asyncio.Event()
    microphone_cancelled = asyncio.Event()

    class FailingDuringMicrophoneStartupSupervisor:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def verify_startup(self) -> None:
            pass

        async def run(self) -> None:
            await microphone_starting.wait()
            raise OllamaResidencyError("residency failed during microphone startup")

    async def blocking_init_mics(*args, **kwargs):
        microphone_starting.set()
        try:
            await asyncio.Event().wait()
        finally:
            microphone_cancelled.set()

    config_path, _port = _write_config(tmp_path, local_model="qwen3:14b")
    created = _patch_runtime_dependencies(monkeypatch)
    monkeypatch.setattr(server, "init_mics", blocking_init_mics)
    monkeypatch.setattr(
        server,
        "OllamaResidencySupervisor",
        FailingDuringMicrophoneStartupSupervisor,
    )

    with pytest.raises(
        OllamaResidencyError,
        match="residency failed during microphone startup",
    ):
        asyncio.run(
            server.run_server(
                load_config_from_yaml(config_path),
                "http://ollama:11434",
            )
        )

    assert microphone_cancelled.is_set()
    assert "runner" not in created
    assert created["agent"].closed
    assert created["ollama_connection"].closed


def test_periodic_residency_failure_cancels_suspended_runner_setup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner_setup_starting = asyncio.Event()
    runner_setup_cancelled = asyncio.Event()

    class FailingDuringRunnerSetupSupervisor:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def verify_startup(self) -> None:
            pass

        async def run(self) -> None:
            await runner_setup_starting.wait()
            raise OllamaResidencyError("residency failed during runner setup")

    class BlockingRunner(_FakeRunner):
        async def setup(self) -> None:
            runner_setup_starting.set()
            try:
                await asyncio.Event().wait()
            finally:
                runner_setup_cancelled.set()

    config_path, _port = _write_config(tmp_path, local_model="qwen3:14b")
    created = _patch_runtime_dependencies(monkeypatch)

    def create_blocking_runner(app):
        runner = BlockingRunner(app)
        created["runner"] = runner
        return runner

    monkeypatch.setattr(server.web, "AppRunner", create_blocking_runner)
    monkeypatch.setattr(
        server,
        "OllamaResidencySupervisor",
        FailingDuringRunnerSetupSupervisor,
    )

    with pytest.raises(
        OllamaResidencyError,
        match="residency failed during runner setup",
    ):
        asyncio.run(
            server.run_server(
                load_config_from_yaml(config_path),
                "http://ollama:11434",
            )
        )

    assert runner_setup_cancelled.is_set()
    assert "site" not in created
    assert created["runner"].cleanup_called
    assert created["microphone_manager"].closed
    assert created["agent"].closed
    assert created["ollama_connection"].closed
