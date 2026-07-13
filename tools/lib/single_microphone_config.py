from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import yaml


DEFAULT_OUTPUT_DIR = Path("/tmp")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]+")


def select_microphone(raw_config: dict[str, Any], microphone_name: str) -> dict[str, Any]:
    raw_microphones = raw_config.get("microphones")
    if not isinstance(raw_microphones, dict):
        raise ValueError("template microphones must be a mapping with a devices list")
    raw_devices = raw_microphones.get("devices")
    if not isinstance(raw_devices, list):
        raise ValueError("template microphones.devices must be a list")

    matches = [
        device
        for device in raw_devices
        if isinstance(device, dict) and device.get("name") == microphone_name
    ]
    if not matches:
        available_names = sorted(
            device["name"]
            for device in raw_devices
            if isinstance(device, dict) and isinstance(device.get("name"), str)
        )
        raise ValueError(
            f"microphone not found: {microphone_name}; available={available_names}"
        )
    if len(matches) != 1:
        raise ValueError(f"duplicate microphone name in template: {microphone_name}")

    raw_microphones["devices"] = [matches[0]]
    return raw_config


def load_template(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as template_file:
        raw_config = yaml.safe_load(template_file)
    if not isinstance(raw_config, dict):
        raise ValueError("template root must be a mapping")
    return raw_config


def default_output_path(microphone_name: str) -> Path:
    safe_name = SAFE_FILENAME_PATTERN.sub("-", microphone_name).strip("-.")
    if not safe_name:
        raise ValueError("microphone name does not contain a safe filename character")
    return DEFAULT_OUTPUT_DIR / f"ai-server-{safe_name}.yaml"


def write_private_config(raw_config: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            os.chmod(temporary_path, 0o600)
            yaml.safe_dump(
                raw_config,
                output_file,
                allow_unicode=True,
                sort_keys=False,
            )
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, output_path)
        os.chmod(output_path, 0o600)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def generate_config(template_path: Path, microphone_name: str, output_path: Path) -> None:
    raw_config = load_template(template_path)
    selected_config = select_microphone(raw_config, microphone_name)
    write_private_config(selected_config, output_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a private AI-server config containing exactly one configured microphone."
    )
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--microphone", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    output_path = args.output or default_output_path(args.microphone)
    try:
        generate_config(args.template, args.microphone, output_path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.error(str(error))
    print(output_path)


if __name__ == "__main__":
    main()
