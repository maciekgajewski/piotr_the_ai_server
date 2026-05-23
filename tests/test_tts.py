import asyncio
import socket

import aioesphomeapi.host_resolver

from ai_server.microphones import tts


class FakeAddrInfo:
    def __init__(self, sockaddr) -> None:
        self.sockaddr = sockaddr


def test_resolve_connect_host_uses_aioesphomeapi_resolver(monkeypatch) -> None:
    async def fake_resolve_host(hosts, port, timeout):
        assert hosts == ["box.local"]
        assert port == tts.API_PORT
        assert timeout == 10.0
        return [FakeAddrInfo(("192.168.1.50", 6053))]

    monkeypatch.setattr(aioesphomeapi.host_resolver, "async_resolve_host", fake_resolve_host)

    assert asyncio.run(tts._resolve_connect_host("box.local")) == "192.168.1.50"


def test_wav_header_is_streamable_pcm_header() -> None:
    header = tts.wav_header(rate=22050, width=2, channels=1)

    assert len(header) == tts.WAV_HEADER_BYTES
    assert header[:4] == b"RIFF"
    assert header[8:12] == b"WAVE"
    assert header[12:16] == b"fmt "
    assert header[36:40] == b"data"
    assert int.from_bytes(header[24:28], "little") == 22050
    assert int.from_bytes(header[34:36], "little") == 16


def test_wait_for_tcp_port_returns_when_port_is_listening() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listening_socket:
        listening_socket.bind(("127.0.0.1", 0))
        listening_socket.listen()
        port = listening_socket.getsockname()[1]

        tts._wait_for_tcp_port("127.0.0.1", port, timeout=1.0)
