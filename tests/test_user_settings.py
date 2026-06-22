import asyncio

from ai_server.user_settings import ConfigUserSettingsProvider, HomeAssistantUserSettingsProvider


class FakeHomeAssistantConnection:
    def __init__(self) -> None:
        self.settings_by_ha_user_id = {}
        self.fail = False
        self.calls = []

    async def get_ryszard_user_settings(self, ha_user_id: str) -> dict:
        self.calls.append(ha_user_id)
        if self.fail:
            raise RuntimeError("HA unavailable")
        return self.settings_by_ha_user_id.get(ha_user_id, {})


def test_config_user_settings_provider_returns_deep_copy_case_insensitively() -> None:
    provider = ConfigUserSettingsProvider(
        {
            "Maciek": {
                "home_assistant_user_id": "ha-user-1",
                "media": {"playlist_aliases": {"Work": "Post Rock Focus"}},
            }
        }
    )

    settings = asyncio.run(provider.settings_for_user("maciek"))
    settings["media"]["playlist_aliases"]["Work"] = "changed"

    assert asyncio.run(provider.settings_for_user("Maciek")) == {
        "media": {"playlist_aliases": {"Work": "Post Rock Focus"}}
    }


def test_home_assistant_user_settings_provider_deep_merges_ha_settings_over_config() -> None:
    connection = FakeHomeAssistantConnection()
    connection.settings_by_ha_user_id["ha-user-1"] = {
        "media": {"playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"}}
    }
    provider = HomeAssistantUserSettingsProvider(
        connection=connection,
        fallback_settings={
            "Maciek": {
                "home_assistant_user_id": "ha-user-1",
                "media": {
                    "liked_songs_media_id": "library://playlist/7",
                    "playlist_aliases": {
                        "Muzyka do pracy": "stale target",
                        "Do gotowania": "Dinner Jazz",
                    },
                }
            }
        },
    )

    settings = asyncio.run(provider.settings_for_user("Maciek"))

    assert settings == {
        "media": {
            "liked_songs_media_id": "library://playlist/7",
            "playlist_aliases": {
                "Muzyka do pracy": "Post Rock Focus",
                "Do gotowania": "Dinner Jazz",
            },
        }
    }


def test_home_assistant_user_settings_provider_keeps_config_aliases_when_ha_aliases_are_empty() -> None:
    connection = FakeHomeAssistantConnection()
    connection.settings_by_ha_user_id["ha-user-1"] = {"media": {"playlist_aliases": {}}}
    provider = HomeAssistantUserSettingsProvider(
        connection=connection,
        fallback_settings={
            "Maciek": {
                "home_assistant_user_id": "ha-user-1",
                "media": {"playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"}},
            }
        },
    )

    assert asyncio.run(provider.settings_for_user("Maciek")) == {
        "media": {"playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"}}
    }


def test_home_assistant_user_settings_provider_uses_last_good_settings_after_fetch_failure() -> None:
    connection = FakeHomeAssistantConnection()
    connection.settings_by_ha_user_id["ha-user-1"] = {
        "media": {"playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"}}
    }
    provider = HomeAssistantUserSettingsProvider(
        connection=connection,
        fallback_settings={"Maciek": {"home_assistant_user_id": "ha-user-1"}},
    )

    assert asyncio.run(provider.settings_for_user("Maciek")) == {
        "media": {"playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"}}
    }
    connection.fail = True

    assert asyncio.run(provider.settings_for_user("Maciek")) == {
        "media": {"playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"}}
    }
    assert provider.status()["failed_users"] == ["Maciek"]


def test_home_assistant_user_settings_provider_continues_without_settings_for_unmapped_user() -> None:
    provider = HomeAssistantUserSettingsProvider(
        connection=FakeHomeAssistantConnection(),
        fallback_settings={"Maciek": {"home_assistant_user_id": "ha-user-1"}},
    )

    assert asyncio.run(provider.settings_for_user("Unknown")) == {}
    assert provider.status()["unmapped_users"] == ["Unknown"]
