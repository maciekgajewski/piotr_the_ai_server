from __future__ import annotations

import ipaddress
import math
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True)
class GatewaySettings:
    url: str
    expected_version: str
    handshake_timeout_seconds: float
    request_timeout_seconds: float
    idle_timeout_seconds: float
    close_timeout_seconds: float
    max_frame_bytes: int
    max_pending_requests: int
    event_queue_capacity: int

    def __post_init__(self) -> None:
        url = urlsplit(self.url)
        if url.scheme not in {"ws", "wss"} or not url.hostname:
            raise ValueError("gateway.url must be an absolute ws/wss URL")
        if url.username or url.password or url.fragment or url.query:
            raise ValueError("gateway.url must not contain credentials, a query, or a fragment")
        if url.scheme == "ws":
            try:
                loopback = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                loopback = url.hostname == "localhost"
            if not loopback:
                raise ValueError("unencrypted Gateway WebSocket requires a loopback host")
        if not isinstance(self.expected_version, str) or not self.expected_version.strip():
            raise ValueError("gateway.expected_version is required")
        for name in (
            "handshake_timeout_seconds", "request_timeout_seconds",
            "idle_timeout_seconds", "close_timeout_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"gateway.{name} must be positive and finite")
        for name in ("max_frame_bytes", "max_pending_requests", "event_queue_capacity"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"gateway.{name} must be a positive integer")


@dataclass(frozen=True)
class GatewayClientSettings:
    name: str
    role: str
    scopes: tuple[str, ...]
    platform: str
    device_family: str
    commands: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    token: str | None = field(default=None, repr=False)
    device_token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.role not in {"node", "operator"}:
            raise ValueError("Gateway role must be node or operator")
        for name in ("name", "platform", "device_family"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or "|" in value:
                raise ValueError(f"Gateway client {name} must be non-empty and contain no pipe")
        for name in ("scopes", "commands", "capabilities"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not value or "|" in value or "," in value
                for value in values
            ) or len(set(values)) != len(values):
                raise ValueError(f"Gateway client {name} must contain unique non-empty names")
        if self.role == "node" and self.scopes:
            raise ValueError("node connections must not request operator scopes")
        if self.role == "operator" and self.commands:
            raise ValueError("operator connections must not declare node commands")
        for token in (self.token, self.device_token):
            if token is not None and (not isinstance(token, str) or not token.strip() or token != token.strip() or "|" in token):
                raise ValueError("Gateway token must be non-empty, trimmed, and contain no pipe")


@dataclass(frozen=True)
class GatewayChatSettings:
    turn_timeout_seconds: float
    abort_timeout_seconds: float
    max_active_turns: int
    max_message_characters: int
    max_reply_characters: int

    def __post_init__(self) -> None:
        for name in ("turn_timeout_seconds", "abort_timeout_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"chat {name} must be positive and finite")
        for name in ("max_active_turns", "max_message_characters", "max_reply_characters"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"chat {name} must be a positive integer")


@dataclass(frozen=True)
class GatewayNotifySettings:
    max_params_bytes: int
    max_title_characters: int
    max_body_characters: int
    max_speech_characters: int
    minimum_remaining_ms: int
    presentation_cleanup_timeout_seconds: float
    result_receipt_margin_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "max_params_bytes", "max_title_characters", "max_body_characters",
            "max_speech_characters", "minimum_remaining_ms",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"notify {name} must be a positive integer")
        for name in ("presentation_cleanup_timeout_seconds", "result_receipt_margin_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"notify {name} must be positive and finite")
