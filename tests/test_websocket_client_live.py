from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import select
import signal
import subprocess

from aiohttp import WSMsgType, web
import pytest

from ai_server.websocket_messages import AssistantMessageCompleted, AssistantMessageStarted, AssistantTextChunk
from ai_server.websocket_messages import ConversationEnded, ConversationReady, ConversationStarted, FollowUpRequested, ProtocolRejected
from ai_server.websocket_messages import ProtocolRejectionCode, SessionAccepted, server_event_to_json


async def _receive_json(websocket) -> dict:
    message = await websocket.receive()
    assert message.type is WSMsgType.TEXT
    return json.loads(message.data)


def test_batch_wrapper_completes_against_deterministic_live_websocket_server() -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        submitted = await _receive_json(websocket)
        assert submitted == {"type": "start_conversation", "message": "hello"}
        await websocket.send_str(server_event_to_json(ConversationStarted("conversation-1")))
        await websocket.send_str(server_event_to_json(AssistantMessageStarted("message-1")))
        await websocket.send_str(server_event_to_json(AssistantTextChunk("message-1", "answer\x1b[31m")))
        await websocket.send_str(server_event_to_json(AssistantMessageCompleted("message-1")))
        await websocket.send_str(server_event_to_json(ConversationEnded("completed")))
        return websocket

    async def run() -> None:
        application = web.Application()
        application.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            process = await asyncio.create_subprocess_exec(
                "tools/batch-ws-client.sh", "--message", "hello", f"ws://127.0.0.1:{port}/chat",
                cwd=Path.cwd(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            assert process.returncode == 0
            assert stdout == b"answer\x1b[31m\n"
            assert stderr == b""
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_batch_wrapper_commits_follow_up_timeout_against_live_server() -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        assert (await _receive_json(websocket))["type"] == "start_conversation"
        await websocket.send_str(server_event_to_json(ConversationStarted("conversation-1")))
        await websocket.send_str(server_event_to_json(FollowUpRequested()))
        assert await _receive_json(websocket) == {"type": "follow_up_timed_out"}
        await websocket.send_str(server_event_to_json(ConversationEnded("follow_up_timeout")))
        return websocket

    async def run() -> None:
        application = web.Application()
        application.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            process = await asyncio.create_subprocess_exec(
                "tools/batch-ws-client.sh", "--message", "hello", "--follow-up-timeout-seconds", "0.001",
                f"ws://127.0.0.1:{port}/chat", cwd=Path.cwd(), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            assert process.returncode == 0
            assert stdout == b""
            assert stderr == b""
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_batch_wrapper_reports_rejection_and_disconnect_from_live_server() -> None:
    async def rejection_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(ProtocolRejected(ProtocolRejectionCode.INVALID_STATE, "no capacity")))
        return websocket

    async def disconnect_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.close()
        return websocket

    async def invoke(handler) -> tuple[int, bytes, bytes]:
        application = web.Application()
        application.router.add_get("/chat", handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            process = await asyncio.create_subprocess_exec(
                "tools/batch-ws-client.sh", f"ws://127.0.0.1:{port}/chat", cwd=Path.cwd(),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
            return process.returncode, stdout, stderr
        finally:
            await runner.cleanup()

    async def run() -> None:
        rejection_code, rejection_stdout, rejection_stderr = await invoke(rejection_handler)
        assert rejection_code == 1
        assert rejection_stdout == b""
        assert b"rejected: invalid_state: no capacity" in rejection_stderr
        disconnect_code, disconnect_stdout, disconnect_stderr = await invoke(disconnect_handler)
        assert disconnect_code == 1
        assert disconnect_stdout == b""
        assert b"connection lost: websocket closed" in disconnect_stderr

    asyncio.run(run())


def test_interactive_wrapper_shows_next_prompt_only_after_live_readiness() -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        assert await _receive_json(websocket) == {"type": "start_conversation", "message": "hello"}
        await websocket.send_str(server_event_to_json(ConversationStarted("conversation-1")))
        await asyncio.sleep(0.1)
        await websocket.send_str(server_event_to_json(AssistantMessageStarted("message-1")))
        await websocket.send_str(server_event_to_json(AssistantTextChunk("message-1", "delayed answer")))
        await websocket.send_str(server_event_to_json(AssistantMessageCompleted("message-1")))
        await websocket.send_str(server_event_to_json(ConversationEnded("completed")))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        await websocket.receive()
        return websocket

    async def run() -> None:
        application = web.Application()
        application.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        master, slave = os.openpty()
        process = subprocess.Popen(
            ["tools/ai-server-chat.sh", f"ws://127.0.0.1:{port}/chat"], cwd=Path.cwd(), stdin=slave, stdout=slave,
            stderr=slave, close_fds=True,
        )
        os.close(slave)
        try:
            await asyncio.sleep(0.5)
            os.write(master, b"hello\r")
            await asyncio.sleep(0.5)
            os.write(master, b"/exit\r")
            assert await asyncio.to_thread(process.wait, 10) == 0
            output = bytearray()
            while select.select([master], [], [], 0)[0]:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
            assert b"delayed answer" in output
            assert b"Conversation ended; waiting for a new conversation." in output
            assert output.count(b"waiting for new conversation> ") == 2
            assert b"waiting for next message> " not in output
            prompt_index = output.find(b"waiting for new conversation> ")
            answer_index = output.find(b"delayed answer")
            assert b"\x1b[3;90m" in output[:prompt_index + len(b"waiting for new conversation> ")]
            assert b"\x1b[0m" in output[max(0, answer_index - 80):answer_index]
            assert bytes(output).rstrip().endswith(b"\x1b[0m")
        finally:
            await runner.cleanup()
            os.close(master)
            if process.poll() is None:
                process.kill()

    asyncio.run(run())


def test_interactive_pty_eof_and_disconnect_restore_terminal() -> None:
    async def eof_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        await websocket.receive()
        return websocket

    async def disconnect_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        await asyncio.sleep(0.2)
        await websocket.close()
        return websocket

    async def invoke(handler, terminal_bytes: bytes | None, expected_code: int) -> bytes:
        application = web.Application()
        application.router.add_get("/chat", handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        master, slave = os.openpty()
        process = subprocess.Popen(
            ["tools/ai-server-chat.sh", f"ws://127.0.0.1:{port}/chat"], cwd=Path.cwd(), stdin=slave, stdout=slave,
            stderr=slave, close_fds=True,
        )
        os.close(slave)
        try:
            if terminal_bytes is not None:
                await asyncio.sleep(0.4)
                os.write(master, terminal_bytes)
            assert await asyncio.to_thread(process.wait, 10) == expected_code
            output = bytearray()
            while select.select([master], [], [], 0)[0]:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
            assert bytes(output).rstrip().endswith(b"\x1b[0m")
            return bytes(output)
        finally:
            await runner.cleanup()
            os.close(master)
            if process.poll() is None:
                process.kill()

    async def run() -> None:
        eof_output = await invoke(eof_handler, b"\x04", 0)
        assert b"waiting for new conversation> " in eof_output
        disconnect_output = await invoke(disconnect_handler, None, 1)
        assert b"connection lost: websocket closed" in disconnect_output

    asyncio.run(run())


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
def test_interactive_pty_signal_from_start_and_follow_up_prompts(termination_signal: int) -> None:
    async def start_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        await websocket.receive()
        return websocket

    async def follow_up_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        assert await _receive_json(websocket) == {"type": "start_conversation", "message": "hello"}
        await websocket.send_str(server_event_to_json(ConversationStarted("conversation-1")))
        await websocket.send_str(server_event_to_json(FollowUpRequested()))
        await websocket.receive()
        return websocket

    async def invoke(handler, before_signal: bytes | None) -> bytes:
        application = web.Application()
        application.router.add_get("/chat", handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        master, slave = os.openpty()
        process = subprocess.Popen(
            ["tools/ai-server-chat.sh", f"ws://127.0.0.1:{port}/chat"], cwd=Path.cwd(), stdin=slave, stdout=slave,
            stderr=slave, close_fds=True, start_new_session=True,
        )
        os.close(slave)
        try:
            await asyncio.sleep(0.5)
            if before_signal is not None:
                os.write(master, before_signal)
                await asyncio.sleep(0.3)
            os.killpg(process.pid, termination_signal)
            assert await asyncio.to_thread(process.wait, 10) == 130
            output = bytearray()
            while select.select([master], [], [], 0)[0]:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
            assert bytes(output).rstrip().endswith(b"\x1b[0m")
            return bytes(output)
        finally:
            await runner.cleanup()
            os.close(master)
            if process.poll() is None:
                process.kill()

    async def run() -> None:
        start_output = await invoke(start_handler, None)
        assert b"waiting for new conversation> " in start_output
        follow_up_output = await invoke(follow_up_handler, b"hello\r")
        assert b"waiting for next message> " in follow_up_output

    asyncio.run(run())


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
def test_interactive_pty_signal_while_server_owns_turn_restores_terminal(termination_signal: int) -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        assert await _receive_json(websocket) == {"type": "start_conversation", "message": "hello"}
        await websocket.send_str(server_event_to_json(ConversationStarted("conversation-1")))
        await websocket.receive()
        return websocket

    async def run() -> None:
        application = web.Application()
        application.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        master, slave = os.openpty()
        process = subprocess.Popen(
            ["tools/ai-server-chat.sh", f"ws://127.0.0.1:{port}/chat"], cwd=Path.cwd(), stdin=slave, stdout=slave,
            stderr=slave, close_fds=True, start_new_session=True,
        )
        os.close(slave)
        try:
            await asyncio.sleep(0.5)
            os.write(master, b"hello\r")
            await asyncio.sleep(0.3)
            os.killpg(process.pid, termination_signal)
            assert await asyncio.to_thread(process.wait, 10) == 130
            output = bytearray()
            while select.select([master], [], [], 0)[0]:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
            assert b"waiting for new conversation> " in output
            assert bytes(output).rstrip().endswith(b"\x1b[0m")
        finally:
            await runner.cleanup()
            os.close(master)
            if process.poll() is None:
                process.kill()

    asyncio.run(run())


def test_interactive_pty_rejection_restores_terminal() -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(ProtocolRejected(ProtocolRejectionCode.INVALID_STATE, "test rejection")))
        return websocket

    async def run() -> None:
        application = web.Application()
        application.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        master, slave = os.openpty()
        process = subprocess.Popen(
            ["tools/ai-server-chat.sh", f"ws://127.0.0.1:{port}/chat"], cwd=Path.cwd(), stdin=slave, stdout=slave,
            stderr=slave, close_fds=True,
        )
        os.close(slave)
        try:
            assert await asyncio.to_thread(process.wait, 10) == 1
            output = bytearray()
            while select.select([master], [], [], 0)[0]:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
            assert b"rejected: invalid_state: test rejection" in output
            assert bytes(output).rstrip().endswith(b"\x1b[0m")
        finally:
            await runner.cleanup()
            os.close(master)
            if process.poll() is None:
                process.kill()

    asyncio.run(run())


def test_interactive_pty_malformed_server_event_restores_terminal() -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str('{"type":"obsolete_event"}')
        return websocket

    async def run() -> None:
        application = web.Application()
        application.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        master, slave = os.openpty()
        process = subprocess.Popen(["tools/ai-server-chat.sh", f"ws://127.0.0.1:{port}/chat"], cwd=Path.cwd(), stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        try:
            assert await asyncio.to_thread(process.wait, 10) == 1
            output = bytearray()
            while select.select([master], [], [], 0)[0]:
                try:
                    output.extend(os.read(master, 4096))
                except OSError:
                    break
            assert b"protocol error:" in output
            assert bytes(output).rstrip().endswith(b"\x1b[0m")
        finally:
            await runner.cleanup()
            os.close(master)
            if process.poll() is None:
                process.kill()

    asyncio.run(run())


def test_interactive_pty_resize_redraw_preserves_partial_input() -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        await websocket.send_str(server_event_to_json(ConversationReady()))
        assert await _receive_json(websocket) == {"type": "start_conversation", "message": "partial input"}
        await websocket.send_str(server_event_to_json(ProtocolRejected(ProtocolRejectionCode.INVALID_STATE, "end test")))
        return websocket

    async def run() -> None:
        application = web.Application()
        application.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        master, slave = os.openpty()
        process = subprocess.Popen(["tools/ai-server-chat.sh", f"ws://127.0.0.1:{port}/chat"], cwd=Path.cwd(), stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True)
        os.close(slave)
        try:
            await asyncio.sleep(0.4)
            os.write(master, b"partial ")
            os.killpg(process.pid, signal.SIGWINCH)
            await asyncio.sleep(0.2)
            os.write(master, b"input\r")
            assert await asyncio.to_thread(process.wait, 10) == 1
        finally:
            await runner.cleanup()
            os.close(master)
            if process.poll() is None:
                process.kill()

    asyncio.run(run())


def test_interactive_pty_history_navigation_reuses_prior_submission(tmp_path: Path) -> None:
    async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse()
        await websocket.prepare(request)
        assert (await _receive_json(websocket))["type"] == "session_start"
        await websocket.send_str(server_event_to_json(SessionAccepted()))
        for number in (1, 2):
            await websocket.send_str(server_event_to_json(ConversationReady()))
            assert await _receive_json(websocket) == {"type": "start_conversation", "message": "history value"}
            await websocket.send_str(server_event_to_json(ConversationStarted(f"conversation-{number}")))
            await websocket.send_str(server_event_to_json(ConversationEnded("completed")))
        await websocket.send_str(server_event_to_json(ProtocolRejected(ProtocolRejectionCode.INVALID_STATE, "end test")))
        return websocket

    async def run() -> None:
        application = web.Application()
        application.router.add_get("/chat", websocket_handler)
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        master, slave = os.openpty()
        environment = {**os.environ, "PIOTR_CHAT_HISTORY": str(tmp_path / "history")}
        process = subprocess.Popen(
            ["tools/ai-server-chat.sh", f"ws://127.0.0.1:{port}/chat"], cwd=Path.cwd(), stdin=slave, stdout=slave,
            stderr=slave, close_fds=True, env=environment,
        )
        os.close(slave)
        try:
            await asyncio.sleep(0.4)
            os.write(master, b"history value\r")
            await asyncio.sleep(0.4)
            os.write(master, b"\x1b[A\r")
            assert await asyncio.to_thread(process.wait, 10) == 1
            assert (tmp_path / "history").exists()
        finally:
            await runner.cleanup()
            os.close(master)
            if process.poll() is None:
                process.kill()

    asyncio.run(run())
