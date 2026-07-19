import asyncio
import signal
import socket
import subprocess
import sys
from pathlib import Path

from aiohttp import ClientConnectorError, ClientSession, WSMsgType

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


def _write_config(tmp_path: Path, *, grace_period: float = 2.0) -> tuple[Path, int]:
    port = _unused_port()
    config = tmp_path / "server-lifecycle.yaml"
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
cache_dir: {tmp_path / 'cache'}
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
