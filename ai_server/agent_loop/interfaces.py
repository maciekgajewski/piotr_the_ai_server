from __future__ import annotations

from typing import Any, Protocol


class HttpResponse(Protocol):
    status: int

    async def __aenter__(self) -> "HttpResponse":
        raise NotImplementedError

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        raise NotImplementedError

    async def json(self) -> dict[str, Any]:
        raise NotImplementedError


class HttpSession(Protocol):
    def post(self, url: str, json: dict[str, Any]) -> HttpResponse:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
