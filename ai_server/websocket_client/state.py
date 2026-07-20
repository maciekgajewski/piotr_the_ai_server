from __future__ import annotations

from enum import Enum


class ClientState(Enum):
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    WAITING_FOR_READY = "waiting_for_ready"
    PRESENTING_START_INPUT = "presenting_start_input"
    AWAITING_START_INPUT = "awaiting_start_input"
    WAITING_FOR_CONVERSATION_START = "waiting_for_conversation_start"
    WAITING_FOR_AGENT = "waiting_for_agent"
    PRESENTING_FOLLOW_UP_INPUT = "presenting_follow_up_input"
    AWAITING_FOLLOW_UP_INPUT = "awaiting_follow_up_input"
    WAITING_FOR_CONVERSATION_END = "waiting_for_conversation_end"
    CLOSING = "closing"
    CLOSED = "closed"


class StreamState(Enum):
    BEFORE_STREAM = "before_stream"
    OPEN = "open"
    FINISHED = "finished"
