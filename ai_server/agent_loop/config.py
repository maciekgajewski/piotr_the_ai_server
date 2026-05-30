from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


DEFAULT_FALLBACK_BACKOFF_SECONDS = 300.0


@dataclass(frozen=True)
class AgentLoopConfig:
    model: str
    ollama_url: str = "http://127.0.0.1:11434"
    fallback_model: str | None = None
    fallback_backoff_seconds: float = DEFAULT_FALLBACK_BACKOFF_SECONDS
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
        if self.fallback_model is not None and not self.fallback_model:
            raise ValueError("fallback_model must be a non-empty string when provided")
        if self.fallback_backoff_seconds <= 0:
            raise ValueError("fallback_backoff_seconds must be positive")
        if not isinstance(self.options, dict):
            raise ValueError("options must be a dict")
        if self.request_timeout_seconds is not None and self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.max_tool_repair_attempts < 0:
            raise ValueError("max_tool_repair_attempts must be zero or greater")
        if self.max_tool_calls_per_message < 1:
            raise ValueError("max_tool_calls_per_message must be positive")
