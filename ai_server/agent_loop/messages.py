from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentReply:
    reply_text: str
    end_conversation: bool
    prompt_eval_count: int | None = None
    eval_count: int | None = None
    duration_ms: int | None = None
