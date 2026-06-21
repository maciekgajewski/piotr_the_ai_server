from custom_components.ryszard.settings import empty_user_settings, set_user_settings, settings_for_user


def test_settings_for_user_returns_only_that_user_settings() -> None:
    data = {
        "users": {
            "ha-user-1": {"media": {"playlist_aliases": {"Work": "Post Rock Focus"}}},
            "ha-user-2": {"media": {"playlist_aliases": {"Dinner": "Dinner Jazz"}}},
        }
    }

    assert settings_for_user(data, "ha-user-1") == {
        "media": {"playlist_aliases": {"Work": "Post Rock Focus"}}
    }


def test_settings_for_user_returns_empty_settings_for_unknown_user() -> None:
    assert settings_for_user({"users": {}}, "missing") == empty_user_settings()


def test_set_user_settings_normalizes_playlist_aliases() -> None:
    data = {"users": {}}

    updated = set_user_settings(
        data,
        "ha-user-1",
        {
            "media": {
                "playlist_aliases": {
                    " Muzyka do pracy ": " Post Rock Focus ",
                    "": "ignored",
                    "empty target": " ",
                    "not string": 123,
                }
            }
        },
    )

    assert updated == {
        "users": {
            "ha-user-1": {
                "media": {"playlist_aliases": {"Muzyka do pracy": "Post Rock Focus"}}
            }
        }
    }
