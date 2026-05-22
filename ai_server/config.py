from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_WEBSOCKET_HOST = "0.0.0.0"
DEFAULT_WEBSOCKET_PATH = "/chat"
DEFAULT_LOG_LEVEL = "INFO"
LOG_LEVELS = frozenset(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))


@dataclass(frozen=True)
class WebsocketConfig:
    port: int
    host: str = DEFAULT_WEBSOCKET_HOST
    path: str = DEFAULT_WEBSOCKET_PATH


@dataclass(frozen=True)
class AgentConfig:
    type: str
    options: dict[str, Any]


@dataclass(frozen=True)
class Config:
    agent: AgentConfig
    websocket: WebsocketConfig
    log_level: str = DEFAULT_LOG_LEVEL


def load_config_from_yaml(path: str | Path) -> Config:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as config_file:
        raw_config = yaml.safe_load(config_file)

    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise ValueError("config must be a YAML mapping")

    websocket_config = raw_config.get("websocket")
    if not isinstance(websocket_config, dict):
        raise ValueError("config must contain a websocket mapping")

    agent_config = raw_config.get("agent")
    if not isinstance(agent_config, dict):
        raise ValueError("config must contain an agent mapping")

    return Config(
        agent=_parse_agent_config(agent_config),
        websocket=_parse_websocket_config(websocket_config),
        log_level=_parse_log_level(raw_config),
    )


def _parse_agent_config(raw_config: dict[str, Any]) -> AgentConfig:
    agent_type = raw_config.get("type")
    if not isinstance(agent_type, str) or not agent_type:
        raise ValueError("agent.type must be a non-empty string")

    options = {key: value for key, value in raw_config.items() if key != "type"}
    if agent_type == "polite_reply":
        model = options.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("agent.model must be a non-empty string for polite_reply")

    return AgentConfig(
        type=agent_type,
        options=options,
    )


def _parse_websocket_config(raw_config: dict[str, Any]) -> WebsocketConfig:
    if "port" not in raw_config:
        raise ValueError("websocket.port is required")

    port = raw_config["port"]
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValueError("websocket.port must be an integer")
    if port < 1 or port > 65535:
        raise ValueError("websocket.port must be between 1 and 65535")

    host = raw_config.get("host", DEFAULT_WEBSOCKET_HOST)
    if not isinstance(host, str) or not host:
        raise ValueError("websocket.host must be a non-empty string")

    path = raw_config.get("path", DEFAULT_WEBSOCKET_PATH)
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("websocket.path must be a string starting with '/'")

    return WebsocketConfig(port=port, host=host, path=path)


def _parse_log_level(raw_config: dict[str, Any]) -> str:
    log_level = raw_config.get("log_level", DEFAULT_LOG_LEVEL)
    if not isinstance(log_level, str):
        raise ValueError("log_level must be a string")

    normalized_log_level = log_level.upper()
    if normalized_log_level not in LOG_LEVELS:
        raise ValueError("log_level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")

    return normalized_log_level
