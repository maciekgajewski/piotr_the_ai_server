from pathlib import Path

import pytest

from ai_server.config import ConversationConfig, ShutdownConfig, load_config_from_yaml


REQUIRED = """
websocket:
  port: 2137
  max_connections: 8
  capacity_retry_after_seconds: 3
  follow_up_idle_lease_seconds: 120
  max_frame_bytes: 65536
  ingress_queue_capacity: 16
  heartbeat_seconds: 30
  handshake_timeout_seconds: 10
conversation:
  agent_cancellation_deadline_seconds: 5
  fatal_notification_seconds: 1
shutdown:
  grace_period_seconds: 15
agent:
  type: echo
"""


def _load(tmp_path: Path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config_from_yaml(path)


def test_loads_required_protocol_bounds(tmp_path: Path) -> None:
    config = _load(tmp_path, REQUIRED)
    assert config.websocket.max_connections == 8
    assert config.websocket.capacity_retry_after_seconds == 3
    assert config.websocket.follow_up_idle_lease_seconds == 120
    assert config.websocket.max_frame_bytes == 65536
    assert config.websocket.ingress_queue_capacity == 16
    assert config.websocket.heartbeat_seconds == 30
    assert config.websocket.handshake_timeout_seconds == 10
    assert config.conversation == ConversationConfig(5.0, 1.0)
    assert config.shutdown == ShutdownConfig(15.0)


@pytest.mark.parametrize(
    "field",
    [
        "max_connections",
        "capacity_retry_after_seconds",
        "follow_up_idle_lease_seconds",
        "max_frame_bytes",
        "ingress_queue_capacity",
        "heartbeat_seconds",
        "handshake_timeout_seconds",
    ],
)
def test_websocket_bounds_are_required(tmp_path: Path, field: str) -> None:
    text = "\n".join(line for line in REQUIRED.splitlines() if not line.strip().startswith(f"{field}:"))
    with pytest.raises(ValueError, match=f"websocket.{field} is required"):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_connections", "false"),
        ("max_connections", "0"),
        ("max_connections", "1.5"),
        ("follow_up_idle_lease_seconds", ".inf"),
        ("heartbeat_seconds", "-1"),
        ("handshake_timeout_seconds", "0"),
    ],
)
def test_websocket_bounds_reject_invalid_values(tmp_path: Path, field: str, value: str) -> None:
    lines = []
    for line in REQUIRED.splitlines():
        if line.strip().startswith(f"{field}:"):
            lines.append(f"  {field}: {value}")
        else:
            lines.append(line)
    with pytest.raises(ValueError, match=f"websocket.{field}"):
        _load(tmp_path, "\n".join(lines))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *(
            (field, value)
            for field in (
                "max_connections",
                "capacity_retry_after_seconds",
                "max_frame_bytes",
                "ingress_queue_capacity",
            )
            for value in ("false", "0", "-1", "1.5", ".inf", ".nan")
        ),
        *(
            (field, value)
            for field in (
                "follow_up_idle_lease_seconds",
                "heartbeat_seconds",
                "handshake_timeout_seconds",
            )
            for value in ("false", "0", "-1", ".inf", ".nan")
        ),
    ],
)
def test_every_websocket_bound_rejects_every_invalid_value_class(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    lines = [
        f"  {field}: {value}" if line.strip().startswith(f"{field}:") else line
        for line in REQUIRED.splitlines()
    ]
    with pytest.raises(ValueError, match=f"websocket.{field}"):
        _load(tmp_path, "\n".join(lines))


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("conversation", "agent_cancellation_deadline_seconds"),
        ("conversation", "fatal_notification_seconds"),
        ("shutdown", "grace_period_seconds"),
    ],
)
def test_lifecycle_deadlines_are_required(tmp_path: Path, section: str, field: str) -> None:
    text = "\n".join(line for line in REQUIRED.splitlines() if not line.strip().startswith(f"{field}:"))
    with pytest.raises(ValueError, match=f"{section}.{field} is required"):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("conversation", "agent_cancellation_deadline_seconds"),
        ("conversation", "fatal_notification_seconds"),
        ("shutdown", "grace_period_seconds"),
    ],
)
@pytest.mark.parametrize("value", ["false", "0", "-1", ".inf", ".nan"])
def test_lifecycle_deadline_invalid_value_matrix(
    tmp_path: Path,
    section: str,
    field: str,
    value: str,
) -> None:
    lines = [
        f"  {field}: {value}" if line.strip().startswith(f"{field}:") else line
        for line in REQUIRED.splitlines()
    ]
    with pytest.raises(ValueError, match=f"{section}.{field}"):
        _load(tmp_path, "\n".join(lines))


def test_microphone_mapping_requires_explicit_policies(tmp_path: Path) -> None:
    text = REQUIRED + """
microphones:
  devices:
    - type: box3_esphome
      name: box
      area: office
      address: box.local
      api_key: secret
"""
    with pytest.raises(ValueError, match="microphones.follow_up_timeout_seconds is required"):
        _load(tmp_path, text)


def test_microphone_mapping_requires_explicit_text_buffer_bound(tmp_path: Path) -> None:
    text = REQUIRED + """
microphones:
  follow_up_timeout_seconds: 15
  devices:
    - type: box3_esphome
      name: box
      area: office
      address: box.local
      api_key: secret
"""
    with pytest.raises(ValueError, match="microphones.assistant_text_buffer_characters is required"):
        _load(tmp_path, text)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("follow_up_timeout_seconds", "false"),
        ("follow_up_timeout_seconds", "0"),
        ("follow_up_timeout_seconds", "-1"),
        ("follow_up_timeout_seconds", ".inf"),
        ("follow_up_timeout_seconds", ".nan"),
        ("assistant_text_buffer_characters", "false"),
        ("assistant_text_buffer_characters", "0"),
        ("assistant_text_buffer_characters", "-1"),
        ("assistant_text_buffer_characters", "1.5"),
    ],
)
def test_microphone_mapping_rejects_invalid_explicit_policy(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    text = REQUIRED + f"""
microphones:
  follow_up_timeout_seconds: {value if field == 'follow_up_timeout_seconds' else '15'}
  assistant_text_buffer_characters: {value if field == 'assistant_text_buffer_characters' else '1024'}
  devices:
    - type: box3_esphome
      name: box
      area: office
      address: box.local
      api_key: secret
"""
    with pytest.raises(ValueError, match=f"microphones.{field}"):
        _load(tmp_path, text)


def test_microphone_mapping_loads_explicit_policies(tmp_path: Path) -> None:
    config = _load(
        tmp_path,
        REQUIRED
        + """
microphones:
  follow_up_timeout_seconds: 15
  assistant_text_buffer_characters: 1024
  devices:
    - type: box3_esphome
      name: box
      area: office
      address: box.local
      api_key: secret
""",
    )
    assert config.microphones[0].follow_up_timeout_seconds == 15
    assert config.microphones[0].assistant_text_buffer_characters == 1024


def test_old_websocket_timeout_field_is_rejected(tmp_path: Path) -> None:
    text = REQUIRED.replace(
        "  max_connections: 8",
        "  max_connections: 8\n  follow_up_timeout_seconds: 60",
    )
    with pytest.raises(ValueError, match="websocket.follow_up_timeout_seconds has been removed"):
        _load(tmp_path, text)
