from pathlib import Path

import pytest

from ai_server.config import (
    AgentConfig,
    DEFAULT_LOG_LEVEL,
    DEFAULT_WEBSOCKET_HOST,
    DEFAULT_WEBSOCKET_PATH,
    Config,
    WebsocketConfig,
    load_config_from_yaml,
)


def write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def test_load_config_with_defaults(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
""",
    )

    assert load_config_from_yaml(config_path) == Config(
        agent=AgentConfig(type="echo", options={}),
        log_level=DEFAULT_LOG_LEVEL,
        websocket=WebsocketConfig(
            host=DEFAULT_WEBSOCKET_HOST,
            port=2137,
            path=DEFAULT_WEBSOCKET_PATH,
        )
    )


def test_load_config_with_explicit_websocket_values(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  host: 127.0.0.1
  port: 2137
  path: /chat
agent:
  type: echo
  temperature: 0
""",
    )

    assert load_config_from_yaml(config_path) == Config(
        agent=AgentConfig(type="echo", options={"temperature": 0}),
        log_level=DEFAULT_LOG_LEVEL,
        websocket=WebsocketConfig(host="127.0.0.1", port=2137, path="/chat")
    )


def test_load_config_with_explicit_log_level(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
log_level: debug
websocket:
  port: 2137
agent:
  type: echo
""",
    )

    assert load_config_from_yaml(config_path) == Config(
        agent=AgentConfig(type="echo", options={}),
        log_level="DEBUG",
        websocket=WebsocketConfig(
            host=DEFAULT_WEBSOCKET_HOST,
            port=2137,
            path=DEFAULT_WEBSOCKET_PATH,
        ),
    )


def test_load_config_requires_websocket_port(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  path: /chat
agent:
  type: echo
""",
    )

    with pytest.raises(ValueError, match="websocket.port is required"):
        load_config_from_yaml(config_path)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("", "config must contain a websocket mapping"),
        ("[]", "config must be a YAML mapping"),
        ("websocket: []", "config must contain a websocket mapping"),
        ("websocket:\n  port: 2137", "config must contain an agent mapping"),
        ("websocket:\n  port: 2137\nagent: []", "config must contain an agent mapping"),
        ("websocket:\n  port: 2137\nagent: {}", "agent.type must be a non-empty string"),
        ("websocket:\n  port: nope\nagent:\n  type: echo", "websocket.port must be an integer"),
        ("websocket:\n  port: 0\nagent:\n  type: echo", "websocket.port must be between 1 and 65535"),
        (
            "websocket:\n  port: 2137\n  path: chat\nagent:\n  type: echo",
            "websocket.path must be a string starting with '/'",
        ),
        ("log_level: noisy\nwebsocket:\n  port: 2137\nagent:\n  type: echo", "log_level must be one of"),
        ("log_level: 1\nwebsocket:\n  port: 2137\nagent:\n  type: echo", "log_level must be a string"),
    ],
)
def test_load_config_rejects_invalid_values(tmp_path: Path, content: str, error: str) -> None:
    config_path = write_config(tmp_path, content)

    with pytest.raises(ValueError, match=error):
        load_config_from_yaml(config_path)
