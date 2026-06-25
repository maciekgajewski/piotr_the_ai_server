from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


JsonDict = dict[str, Any]

_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class JsonFileStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory.expanduser()

    def load(self, key: str) -> JsonDict:
        path = self._path_for_key(key)
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError(f"stored JSON data for key {key!r} must be an object")
        return data

    def store(self, key: str, data: JsonDict) -> None:
        if not isinstance(data, dict):
            raise ValueError("stored data must be a JSON object")
        path = self._path_for_key(key)
        serialized_data = json.dumps(
            _omit_none_dict_fields(data),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as stream:
            stream.write(serialized_data)
            stream.write("\n")
        temporary_path.replace(path)

    def _path_for_key(self, key: str) -> Path:
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError("JSON store key must contain only letters, digits, dots, underscores, and hyphens")
        return self._directory / f"{key}.json"


def _omit_none_dict_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _omit_none_dict_fields(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_omit_none_dict_fields(item) for item in value]
    return value
