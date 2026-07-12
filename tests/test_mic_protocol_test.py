from __future__ import annotations

import importlib.util
from pathlib import Path
import struct
import sys

import pytest

from ai_server.config import AgentConfig, Config, MicrophoneConfig, WebsocketConfig


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "lib" / "mic_protocol_test.py"
SPEC = importlib.util.spec_from_file_location("mic_protocol_test", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
mic_protocol_test = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mic_protocol_test
SPEC.loader.exec_module(mic_protocol_test)


def test_select_microphone_config_uses_only_configured_microphone() -> None:
    microphone = MicrophoneConfig(type="test", name="one", area=None, options={})
    assert mic_protocol_test.select_microphone_config(_config((microphone,)), None) is microphone


def test_select_microphone_config_requires_name_when_multiple() -> None:
    config = _config(
        (
            MicrophoneConfig(type="test", name="one", area=None, options={}),
            MicrophoneConfig(type="test", name="two", area=None, options={}),
        )
    )
    with pytest.raises(ValueError, match="use --mic"):
        mic_protocol_test.select_microphone_config(config, None)


def test_normalize_pcm16_audio() -> None:
    chunk = _pcm16_chunk(1000, -1000)
    assert mic_protocol_test.normalize_pcm16_chunks((chunk,), 2, 0.5) == (
        _pcm16_chunk(16383, -16383),
    )


def test_parse_args_rejects_invalid_volume() -> None:
    with pytest.raises(SystemExit, match="--volume must be between 0.0 and 1.0"):
        mic_protocol_test.parse_args(["--volume", "2"])


def test_parse_args_rejects_invalid_normalize_peak() -> None:
    with pytest.raises(SystemExit, match="--normalize-replay-peak must be between 0.0 and 1.0"):
        mic_protocol_test.parse_args(["--normalize-replay-peak", "0"])


def _pcm16_chunk(*samples: int) -> bytes:
    return struct.pack("<" + "h" * len(samples), *samples)


def _config(microphones: tuple[MicrophoneConfig, ...]) -> Config:
    return Config(
        agent=AgentConfig(type="interrogator", options={}),
        websocket=WebsocketConfig(port=8765),
        microphones=microphones,
    )
