from __future__ import annotations

import copy
from typing import Any


def empty_user_settings() -> dict[str, Any]:
    return {"media": {"playlist_aliases": {}}}


def settings_for_user(data: dict[str, Any], user_id: str) -> dict[str, Any]:
    users = data.get("users")
    if not isinstance(users, dict):
        return empty_user_settings()
    settings = users.get(user_id)
    if not isinstance(settings, dict):
        return empty_user_settings()
    return normalize_user_settings(settings)


def set_user_settings(data: dict[str, Any], user_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(data)
    users = updated.setdefault("users", {})
    if not isinstance(users, dict):
        users = {}
        updated["users"] = users
    users[user_id] = normalize_user_settings(settings)
    return updated


def normalize_user_settings(settings: dict[str, Any]) -> dict[str, Any]:
    media = settings.get("media")
    if not isinstance(media, dict):
        media = {}

    aliases = media.get("playlist_aliases")
    if not isinstance(aliases, dict):
        aliases = {}

    normalized_aliases: dict[str, str] = {}
    for alias, target in aliases.items():
        if not isinstance(alias, str) or not isinstance(target, str):
            continue
        clean_alias = alias.strip()
        clean_target = target.strip()
        if clean_alias and clean_target:
            normalized_aliases[clean_alias] = clean_target

    return {"media": {"playlist_aliases": normalized_aliases}}
