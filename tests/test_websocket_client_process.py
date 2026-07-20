from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import time

import pytest


def test_interactive_wrapper_rejects_non_tty() -> None:
    completed = subprocess.run(
        ["tools/ai-server-chat.sh"],
        cwd=".",
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 2
    assert "tools/batch-ws-client.sh" in completed.stderr


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
def test_interactive_pty_signal_during_connect_ends_with_explicit_reset(termination_signal: int) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    master, slave = os.openpty()
    process = subprocess.Popen(
        ["tools/ai-server-chat.sh", f"ws://127.0.0.1:{port}/chat"],
        cwd=".",
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave)
    try:
        time.sleep(1.0)
        os.killpg(process.pid, termination_signal)
        assert process.wait(timeout=10) == 130
        output = bytearray()
        while select.select([master], [], [], 0)[0]:
            try:
                output.extend(os.read(master, 4096))
            except OSError:
                break
        assert bytes(output).rstrip().endswith(b"\x1b[0m")
    finally:
        listener.close()
        os.close(master)
        if process.poll() is None:
            process.kill()


@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
def test_batch_signal_during_connect_preserves_interrupted_exit(termination_signal: int) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    process = subprocess.Popen(
        ["tools/batch-ws-client.sh", f"ws://127.0.0.1:{port}/chat"], cwd=".",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    try:
        time.sleep(0.5)
        os.killpg(process.pid, termination_signal)
        assert process.wait(timeout=10) == 130
    finally:
        listener.close()
        if process.poll() is None:
            process.kill()


def test_terminal_presenter_pty_async_status_preserves_partial_edit(tmp_path) -> None:
    master, slave = os.openpty()
    environment = {**os.environ, "PIOTR_CHAT_HISTORY": str(tmp_path / "history")}
    process = subprocess.Popen(
        [".venv/bin/python", "-m", "tools.lib.terminal_presenter_pty_fixture"], cwd=".", stdin=slave, stdout=slave,
        stderr=slave, close_fds=True, env=environment,
    )
    os.close(slave)
    try:
        time.sleep(0.25)
        os.write(master, b"partial ")
        time.sleep(0.6)
        os.write(master, b"input\r")
        return_code = process.wait(timeout=10)
        output = bytearray()
        while select.select([master], [], [], 0)[0]:
            try:
                output.extend(os.read(master, 4096))
            except OSError:
                break
        assert return_code == 0, bytes(output)
        assert b"asynchronous status" in output
        assert b"RESULT:partial input" in output
        assert bytes(output).rstrip().endswith(b"\x1b[0m")
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
