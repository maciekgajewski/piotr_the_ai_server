import json

import pytest

from ai_server.utils import JsonFileStore


def test_json_file_store_loads_missing_key_as_empty_dict(tmp_path):
    store = JsonFileStore(tmp_path)

    assert store.load("system_status") == {}


def test_json_file_store_stores_one_file_per_key(tmp_path):
    store = JsonFileStore(tmp_path)

    store.store("system_status.baseline", {"cpu": {"ema": 0.42, "missing": None}, "user": "Krzysztof"})

    assert json.loads((tmp_path / "system_status.baseline.json").read_text(encoding="utf-8")) == {
        "cpu": {"ema": 0.42},
        "user": "Krzysztof",
    }
    assert store.load("system_status.baseline") == {"cpu": {"ema": 0.42}, "user": "Krzysztof"}


@pytest.mark.parametrize("key", ["", "../escape", "system/status", "status json", ".hidden"])
def test_json_file_store_rejects_unsafe_keys(tmp_path, key):
    store = JsonFileStore(tmp_path)

    with pytest.raises(ValueError, match="JSON store key"):
        store.load(key)


def test_json_file_store_rejects_non_object_stored_data(tmp_path):
    store = JsonFileStore(tmp_path)

    with pytest.raises(ValueError, match="stored data must be a JSON object"):
        store.store("bad", ["not", "a", "dict"])


def test_json_file_store_rejects_non_object_file_content(tmp_path):
    (tmp_path / "bad.json").write_text("[]", encoding="utf-8")
    store = JsonFileStore(tmp_path)

    with pytest.raises(ValueError, match="must be an object"):
        store.load("bad")


def test_json_file_store_rejects_non_json_values(tmp_path):
    store = JsonFileStore(tmp_path)

    with pytest.raises(ValueError, match="Out of range float values"):
        store.store("bad", {"value": float("nan")})
