import asyncio

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
