from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentReply:
    reply_text: str
    end_conversation: bool
