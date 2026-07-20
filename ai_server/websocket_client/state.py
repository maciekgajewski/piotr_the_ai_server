from __future__ import annotations

from enum import Enum


class ClientState(Enum):
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    WAITING_FOR_READY = "waiting_for_ready"
    AWAITING_START_INPUT = "awaiting_start_input"
    WAITING_FOR_CONVERSATION_START = "waiting_for_conversation_start"
    WAITING_FOR_AGENT = "waiting_for_agent"
    AWAITING_FOLLOW_UP_INPUT = "awaiting_follow_up_input"
    WAITING_FOR_CONVERSATION_END = "waiting_for_conversation_end"
    CLOSED = "closed"


class StreamState(Enum):
    BEFORE_STREAM = "before_stream"
    OPEN = "open"
    FINISHED = "finished"
