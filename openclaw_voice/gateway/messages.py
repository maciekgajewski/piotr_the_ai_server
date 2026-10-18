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


class GatewayChatBusy(GatewayError):
    pass


class GatewayChatFailed(GatewayError):
    pass


class GatewayChatUncertain(GatewayError):
    """A remote run may still be active; its room route must stay excluded."""


class GatewayNotifyRejected(GatewayError):
    """A notification was rejected before any playback began."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class GatewayNotifyUncertain(GatewayError):
    """Playback may have begun; the node must not claim it was unheard."""


class GatewayNotifyReceiptUncertain(GatewayError):
    """The native caller did not confirm the node's result; never replay it."""


@dataclass(frozen=True)
class GatewayNotification:
    invocation_id: str
    node_id: str
    idempotency_key: str
    title: str
    body: str
    sound: str | None
    priority: str | None
    delivery: str | None
    remaining_ms: int


@dataclass(frozen=True)
class GatewayChatReply:
    run_id: str
    session_key: str
    text: str


@dataclass(frozen=True)
class GatewayEvent:
    name: str
    payload: Any = field(repr=False)
    sequence: int | None = None


@dataclass(frozen=True)
class GatewayHello:
    version: str
    connection_id: str
    role: str
    methods: frozenset[str]
    events: frozenset[str]
    scopes: frozenset[str]
    device_token: str | None = field(default=None, repr=False)
