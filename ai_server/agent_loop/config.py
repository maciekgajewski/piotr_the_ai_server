from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentLoopConfig:
    model: str
    ollama_url: str = "http://127.0.0.1:11434"
    options: dict[str, Any] = field(default_factory=dict)
    keep_alive: str | int | None = None
    think: bool | str | None = False
    request_timeout_seconds: float | None = 60.0
    max_tool_repair_attempts: int = 2
    max_tool_calls_per_message: int = 8

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must be a non-empty string")
        if not self.ollama_url:
            raise ValueError("ollama_url must be a non-empty string")
        if not isinstance(self.options, dict):
            raise ValueError("options must be a dict")
        if self.request_timeout_seconds is not None and self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.max_tool_repair_attempts < 0:
            raise ValueError("max_tool_repair_attempts must be zero or greater")
        if self.max_tool_calls_per_message < 1:
            raise ValueError("max_tool_calls_per_message must be positive")
