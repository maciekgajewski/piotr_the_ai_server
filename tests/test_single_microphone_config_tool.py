from __future__ import annotations

import copy
from pathlib import Path
import stat

import pytest
import yaml

from tools.lib import single_microphone_config


def _template() -> dict:
    return {
        "log_level": "DEBUG",
        "stt": {"log_transcripts": True},
        "microphones": {
            "open_mic_wake_phrase": "Ryszardzie",
            "devices": [
                {
                    "type": "box3_esphome",
                    "name": "box3-office",
                    "address": "box.local",
                    "api_key": "box-secret",
                },
                {
                    "type": "box3_esphome",
                    "name": "voice-pe-02",
                    "address": "voice-pe.local",
                    "api_key": "voice-pe-secret",
                },
            ],
        },
    }


def test_select_microphone_preserves_config_and_keeps_exactly_one_device() -> None:
    selected = single_microphone_config.select_microphone(
        copy.deepcopy(_template()), "box3-office"
    )

    assert selected["log_level"] == "DEBUG"
    assert selected["stt"] == {"log_transcripts": True}
    assert selected["microphones"]["open_mic_wake_phrase"] == "Ryszardzie"
    assert selected["microphones"]["devices"] == [
        {
            "type": "box3_esphome",
            "name": "box3-office",
            "address": "box.local",
            "api_key": "box-secret",
        }
    ]


def test_select_microphone_rejects_unknown_and_duplicate_names() -> None:
    with pytest.raises(ValueError, match="microphone not found: missing"):
        single_microphone_config.select_microphone(copy.deepcopy(_template()), "missing")

    duplicate = _template()
    duplicate["microphones"]["devices"].append(
        copy.deepcopy(duplicate["microphones"]["devices"][0])
    )
    with pytest.raises(ValueError, match="duplicate microphone name"):
        single_microphone_config.select_microphone(duplicate, "box3-office")


@pytest.mark.parametrize(
    "raw_config,error",
    [
        ({}, "microphones must be a mapping"),
        ({"microphones": {}}, "microphones.devices must be a list"),
    ],
)
def test_select_microphone_rejects_invalid_template_shape(raw_config: dict, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        single_microphone_config.select_microphone(raw_config, "box3-office")


def test_generate_config_writes_private_atomic_output(tmp_path: Path) -> None:
    template_path = tmp_path / "template.yaml"
    output_path = tmp_path / "generated.yaml"
    template_path.write_text(
        yaml.safe_dump(_template(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    output_path.write_text("old", encoding="utf-8")
    output_path.chmod(0o644)

    single_microphone_config.generate_config(
        template_path, "voice-pe-02", output_path
    )

    generated = yaml.safe_load(output_path.read_text(encoding="utf-8"))
    assert [
        device["name"] for device in generated["microphones"]["devices"]
    ] == ["voice-pe-02"]
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".generated.yaml.*.tmp")) == []


def test_default_output_path_sanitizes_microphone_name() -> None:
    assert single_microphone_config.default_output_path("box3 office/one") == Path(
        "/tmp/ai-server-box3-office-one.yaml"
    )
