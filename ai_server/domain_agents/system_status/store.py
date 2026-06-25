from __future__ import annotations

from typing import Any

from ai_server.utils import JsonFileStore


BASELINES_KEY = "system_status.baselines"
SNAPSHOT_KEY = "system_status.snapshot"


class SystemStatusStore:
    def __init__(self, store: JsonFileStore) -> None:
        self._store = store

    def load_baselines(self) -> dict[str, Any]:
        data = self._store.load(BASELINES_KEY)
        baselines = data.get("metrics", {})
        return baselines if isinstance(baselines, dict) else {}

    def store_baselines(self, baselines: dict[str, Any]) -> None:
        self._store.store(BASELINES_KEY, {"metrics": baselines})

    def load_snapshot(self) -> dict[str, Any]:
        return self._store.load(SNAPSHOT_KEY)

    def store_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._store.store(SNAPSHOT_KEY, snapshot)
