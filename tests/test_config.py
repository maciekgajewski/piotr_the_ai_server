from pathlib import Path

import pytest

from ai_server.config import (
    AgentConfig,
    ConversationConfig,
    DEFAULT_LOG_LEVEL,
    DEFAULT_WEBSOCKET_HOST,
    DEFAULT_WEBSOCKET_PATH,
    Config,
    MicrophoneConfig,
    SttConfig,
    TtsConfig,
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


def test_load_config_with_polite_reply_agent_model(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: polite_reply
  model: qwen3:4b
""",
    )

    assert load_config_from_yaml(config_path) == Config(
        agent=AgentConfig(type="polite_reply", options={"model": "qwen3:4b"}),
        log_level=DEFAULT_LOG_LEVEL,
        websocket=WebsocketConfig(
            host=DEFAULT_WEBSOCKET_HOST,
            port=2137,
            path=DEFAULT_WEBSOCKET_PATH,
        ),
    )


def test_load_config_adds_top_level_home_assistant_to_agent_options(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: assistant
  intent_router_model: llama3.2:3b
home_assistant:
  url: http://ha.local:8123
  token: secret-token
""",
    )

    assert load_config_from_yaml(config_path).agent == AgentConfig(
        type="assistant",
        options={
            "intent_router_model": "llama3.2:3b",
            "home_assistant": {
                "url": "http://ha.local:8123",
                "token": "secret-token",
            },
        },
    )


def test_load_config_with_voice_defaults_and_multiple_microphones(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
microphones:
  - type: box3_esphome
    name: box3-office
    address: piotr-box3-01-cbfaA8.local
    api_key: abc
    location: office
  - type: box3_esphome
    name: box3-roaming
    address: 192.168.1.42
    api_key: def
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.stt == SttConfig()
    assert config.tts == TtsConfig()
    assert config.microphones == (
        MicrophoneConfig(
            type="box3_esphome",
            name="box3-office",
            location="office",
            follow_up_timeout_seconds=None,
            options={"address": "piotr-box3-01-cbfaA8.local", "api_key": "abc"},
        ),
        MicrophoneConfig(
            type="box3_esphome",
            name="box3-roaming",
            location=None,
            follow_up_timeout_seconds=None,
            options={"address": "192.168.1.42", "api_key": "def"},
        ),
    )


def test_load_config_with_explicit_conversation_and_microphone_timeout(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
conversation:
  follow_up_timeout_seconds: 12.5
microphones:
  - type: box3_esphome
    name: box3-office
    address: box.local
    api_key: abc
    follow_up_timeout_seconds: 3
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.conversation == ConversationConfig(follow_up_timeout_seconds=12.5)
    assert config.microphones[0].follow_up_timeout_seconds == 3.0


def test_load_config_with_explicit_voice_values(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
stt:
  model: small
  language: pl
  device: cpu
  beam_size: 3
  capture_seconds: 4.5
tts:
  voice: pl_PL-darkman-medium
  volume: 0.7
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.stt == SttConfig(
        model="small",
        language="pl",
        device="cpu",
        beam_size=3,
        capture_seconds=4.5,
    )
    assert config.tts == TtsConfig(voice="pl_PL-darkman-medium", volume=0.7)


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
        (
            "websocket:\n  port: 2137\nagent:\n  type: polite_reply",
            "agent.model must be a non-empty string for polite_reply",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: polite_reply\n  model: ''",
            "agent.model must be a non-empty string for polite_reply",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: polite_reply\n  model: 123",
            "agent.model must be a non-empty string for polite_reply",
        ),
        ("websocket:\n  port: nope\nagent:\n  type: echo", "websocket.port must be an integer"),
        ("websocket:\n  port: 0\nagent:\n  type: echo", "websocket.port must be between 1 and 65535"),
        (
            "websocket:\n  port: 2137\n  path: chat\nagent:\n  type: echo",
            "websocket.path must be a string starting with '/'",
        ),
        ("log_level: noisy\nwebsocket:\n  port: 2137\nagent:\n  type: echo", "log_level must be one of"),
        ("log_level: 1\nwebsocket:\n  port: 2137\nagent:\n  type: echo", "log_level must be a string"),
        ("websocket:\n  port: 2137\nagent:\n  type: echo\nstt: []", "stt must be a mapping"),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nstt:\n  device: nope",
            "stt.device must be one of",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\ntts:\n  volume: 2",
            "tts.volume must be between",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones: {}",
            "microphones must be a list",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  - name: box",
            r"microphones\[0\].type must be a non-empty string",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  - type: box3_esphome",
            r"microphones\[0\].name must be a non-empty string",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  - type: box3_esphome\n    name: box",
            r"microphones\[0\].address must be a non-empty string for box3_esphome",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  - type: box3_esphome\n    name: box\n    address: host",
            r"microphones\[0\].api_key must be a non-empty string for box3_esphome",
        ),
    ],
)
def test_load_config_rejects_invalid_values(tmp_path: Path, content: str, error: str) -> None:
    config_path = write_config(tmp_path, content)

    with pytest.raises(ValueError, match=error):
        load_config_from_yaml(config_path)
