from __future__ import annotations

from collections import Counter
from pathlib import Path
import re

import pytest


FIRMWARE_PACKAGES = (
    Path("firmware/esphome/packages/home-assistant-voice-pe-piotr-standard.yaml"),
    Path("firmware/esphome/packages/esp32-s3-box-3-ryszardzie.yaml"),
)


@pytest.mark.parametrize("package_path", FIRMWARE_PACKAGES, ids=lambda path: path.stem)
def test_reconnect_retains_error_until_explicit_server_visual_command(
    package_path: Path,
) -> None:
    """MP-VISUAL-001/003/005: connectivity alone never selects a connected visual."""
    source = package_path.read_text(encoding="utf-8")

    assert source.count("id(server_visual_received) = false;") >= 2
    assert "if (!id(server_visual_received))" in source
    for state in ("idle", "listening", "processing"):
        script = re.search(
            rf"  - id: set_visual_{state}\n(?P<body>.*?)(?=\n  - id: )",
            source,
            re.DOTALL,
        )
        assert script is not None
        assert "id(server_visual_received) = true;" in script.group("body")


@pytest.mark.parametrize("package_path", FIRMWARE_PACKAGES, ids=lambda path: path.stem)
def test_only_connection_and_server_visual_handlers_assign_main_visual_phase(
    package_path: Path,
) -> None:
    """MP-VISUAL-004/006: callbacks and local indicators cannot infer a main phase."""
    source = package_path.read_text(encoding="utf-8")
    assigned_phases = Counter(
        re.findall(r"id\(voice_assistant_phase\) = \$\{([^}]+)\};", source)
    )

    if package_path.name.startswith("home-assistant"):
        assert assigned_phases == Counter(
            {
                "voice_assist_not_ready_phase_id": 2,
                "voice_assist_idle_phase_id": 1,
                "voice_assist_listening_for_command_phase_id": 1,
                "voice_assist_thinking_phase_id": 1,
            }
        )
    else:
        assert assigned_phases == Counter(
            {
                "voice_assist_not_ready_phase_id": 2,
                "voice_assist_idle_phase_id": 1,
                "voice_assist_listening_phase_id": 1,
                "voice_assist_thinking_phase_id": 1,
            }
        )
    assert "main visual remains server-controlled" in source


@pytest.mark.parametrize("package_path", FIRMWARE_PACKAGES, ids=lambda path: path.stem)
def test_explicit_stop_service_stops_device_pipeline(
    package_path: Path,
) -> None:
    """MP-STOP-001: the shared stop command reaches an explicit firmware stop."""
    source = package_path.read_text(encoding="utf-8")
    stop_script = re.search(
        r"  - id: stop_listening\n(?P<body>.*?)(?=\n  - id: )",
        source,
        re.DOTALL,
    )

    assert stop_script is not None
    assert "voice_assistant.stop:" in stop_script.group("body")


def test_shared_api_exposes_only_server_commandable_visual_states() -> None:
    """MP-VISUAL-001: firmware exposes no command that can select ERROR."""
    source = Path(
        "firmware/esphome/packages/piotr-voice-satellite-api-services.yaml"
    ).read_text(encoding="utf-8")
    visual_services = re.findall(r"    - service: set_visual_(\w+)", source)

    assert visual_services == ["idle", "listening", "processing"]

