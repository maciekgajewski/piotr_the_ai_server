from __future__ import annotations

import copy
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ai_server.home_assistant import HomeAssistantConnection

HOME_ASSISTANT_USER_ID_KEY = "home_assistant_user_id"


class UserSettingsProvider(ABC):
    @abstractmethod
    async def settings_for_user(self, user: str | None) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def user_exists(self, user: str) -> bool:
        raise NotImplementedError


@dataclass
class ConfigUserSettingsProvider(UserSettingsProvider):
    user_settings: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def settings_for_user(self, user: str | None) -> dict[str, Any]:
        return _strip_internal_settings(_user_settings_for(user, self.user_settings))

    async def user_exists(self, user: str) -> bool:
        return _user_exists(user, self.user_settings)


class HomeAssistantUserSettingsProvider(UserSettingsProvider):
    def __init__(
        self,
        *,
        connection: HomeAssistantConnection,
        fallback_settings: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._connection = connection
        self._fallback_settings = copy.deepcopy(fallback_settings or {})
        self._ha_user_ids_by_user = _ha_user_ids_by_user(self._fallback_settings)
        self._last_good_settings_by_user: dict[str, dict[str, Any]] = {}
        self._last_success_by_user: dict[str, datetime] = {}
        self._failed_users: set[str] = set()
        self._unmapped_users: set[str] = set()
        self._logger = logging.getLogger(f"{__name__}.HomeAssistantUserSettingsProvider")

    async def settings_for_user(self, user: str | None) -> dict[str, Any]:
        base_settings = _strip_internal_settings(_user_settings_for(user, self._fallback_settings))
        if not user:
            return base_settings

        ha_user_id = _mapped_ha_user_id(user, self._ha_user_ids_by_user)
        if ha_user_id is None:
            self._unmapped_users.add(user)
            return base_settings

        try:
            ha_settings = await self._connection.get_ryszard_user_settings(ha_user_id)
        except Exception:
            self._failed_users.add(user)
            self._logger.warning("could not fetch HA-owned settings for user=%r", user, exc_info=True)
            return _merge_settings(base_settings, self._last_good_settings_by_user.get(user, {}))

        self._failed_users.discard(user)
        self._unmapped_users.discard(user)
        self._last_good_settings_by_user[user] = copy.deepcopy(ha_settings)
        self._last_success_by_user[user] = datetime.now(UTC)
        return _merge_settings(base_settings, ha_settings)

    async def user_exists(self, user: str) -> bool:
        return _user_exists(user, self._fallback_settings)

    def status(self) -> dict[str, Any]:
        return {
            "mode": "home_assistant",
            "mapped_users": sorted(self._ha_user_ids_by_user),
            "last_success_by_user": {
                user: timestamp.isoformat()
                for user, timestamp in sorted(self._last_success_by_user.items())
            },
            "failed_users": sorted(self._failed_users),
            "unmapped_users": sorted(self._unmapped_users),
        }


def create_user_settings_provider(
    *,
    home_assistant_connection: HomeAssistantConnection | None,
    fallback_settings: dict[str, dict[str, Any]],
) -> UserSettingsProvider:
    if home_assistant_connection is not None and _ha_user_ids_by_user(fallback_settings):
        return HomeAssistantUserSettingsProvider(
            connection=home_assistant_connection,
            fallback_settings=fallback_settings,
        )
    return ConfigUserSettingsProvider(fallback_settings)


def _mapped_ha_user_id(user: str, mappings: dict[str, str]) -> str | None:
    ha_user_id = mappings.get(user)
    if ha_user_id is not None:
        return ha_user_id
    normalized_user = user.casefold()
    for candidate_user, candidate_ha_user_id in mappings.items():
        if candidate_user.casefold() == normalized_user:
            return candidate_ha_user_id
    return None


def _user_settings_for(user: str | None, user_settings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not user:
        return {}
    settings = user_settings.get(user)
    if settings is None:
        normalized_user = user.casefold()
        for candidate_user, candidate_settings in user_settings.items():
            if candidate_user.casefold() == normalized_user:
                settings = candidate_settings
                break
    return copy.deepcopy(settings) if isinstance(settings, dict) else {}


def _user_exists(user: str, user_settings: dict[str, dict[str, Any]]) -> bool:
    if user in user_settings:
        return True
    normalized_user = user.casefold()
    return any(candidate_user.casefold() == normalized_user for candidate_user in user_settings)


def _ha_user_ids_by_user(user_settings: dict[str, dict[str, Any]]) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for user, settings in user_settings.items():
        if not isinstance(settings, dict):
            continue
        home_assistant_user_id = settings.get(HOME_ASSISTANT_USER_ID_KEY)
        if isinstance(home_assistant_user_id, str) and home_assistant_user_id:
            mappings[user] = home_assistant_user_id
    return mappings


def _strip_internal_settings(settings: dict[str, Any]) -> dict[str, Any]:
    settings.pop(HOME_ASSISTANT_USER_ID_KEY, None)
    return settings


def _merge_settings(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_settings(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged
