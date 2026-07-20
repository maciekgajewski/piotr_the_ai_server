from __future__ import annotations

import math

DEFAULT_WEBSOCKET_URL = "ws://127.0.0.1:2137/chat"
DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS = 15.0
WEBSOCKET_HEARTBEAT_SECONDS = 2.0


def positive_finite_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError("must be a positive finite number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("must be a positive finite number")
    return parsed
