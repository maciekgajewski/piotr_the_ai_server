from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


class GatewayError(Exception):
    """A bounded failure at the external Gateway boundary."""


class GatewayProtocolError(GatewayError):
    pass


class GatewayDisconnected(GatewayError):
    pass


class GatewayCapacityExceeded(GatewayError):
    pass


class GatewayRequestUncertain(GatewayError):
    """A request may have reached the Gateway; callers must not replay it."""


class GatewayRequestCancelled(asyncio.CancelledError):
    """Cancellation after dispatch began; preserves cancellation and uncertainty."""

    def __init__(self, request_id: str) -> None:
        super().__init__("Gateway request cancelled after dispatch began; do not replay")
        self.request_id = request_id
        self.may_have_been_accepted = True


class GatewayRejected(GatewayError):
    def __init__(self, code: str, details: Any = None) -> None:
        # Remote messages/details may contain submitted text or credentials.
        super().__init__(f"Gateway rejected request ({code})")
        self.code = code
        self.details = details


@dataclass(frozen=True)
class GatewayEvent:
    name: str
    payload: Any = field(repr=False)
    sequence: int | None = None


@dataclass(frozen=True)
class GatewayHello:
    version: str
    connection_id: str
    methods: frozenset[str]
    events: frozenset[str]
    scopes: frozenset[str]
    device_token: str | None = field(default=None, repr=False)
