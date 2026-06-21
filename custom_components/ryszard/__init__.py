from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from .const import DOMAIN, PANEL_URL_PATH, STORAGE_KEY, STORAGE_VERSION
from .settings import set_user_settings, settings_for_user


async def async_setup(hass, config) -> bool:
    from homeassistant.components import panel_custom
    from homeassistant.helpers.storage import Store

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    hass.data[DOMAIN] = {"store": store}

    await _register_static_panel_asset(hass)
    register_result = panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name="ryszard-panel",
        module_url="/api/ryszard/panel.js",
        sidebar_title="Ryszard",
        sidebar_icon="mdi:account-voice",
        require_admin=False,
        config={},
    )
    if inspect.isawaitable(register_result):
        await register_result

    _register_websocket_commands(hass)
    return True


async def async_unload_entry(hass, entry) -> bool:
    from homeassistant.components import panel_custom

    remove_result = panel_custom.async_remove_panel(hass, PANEL_URL_PATH)
    if inspect.isawaitable(remove_result):
        await remove_result
    hass.data.pop(DOMAIN, None)
    return True


def _register_websocket_commands(hass) -> None:
    import voluptuous as vol

    from homeassistant.components import websocket_api

    @websocket_api.websocket_command({vol.Required("type"): "ryszard/settings/get"})
    @websocket_api.async_response
    async def websocket_get_settings(hass, connection, msg) -> None:
        data = await _load_data(hass)
        connection.send_result(msg["id"], {"settings": settings_for_user(data, connection.user.id)})

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "ryszard/settings/update",
            vol.Required("settings"): dict,
        }
    )
    @websocket_api.async_response
    async def websocket_update_settings(hass, connection, msg) -> None:
        data = await _load_data(hass)
        updated = set_user_settings(data, connection.user.id, msg["settings"])
        await _store(hass).async_save(updated)
        connection.send_result(msg["id"], {"settings": settings_for_user(updated, connection.user.id)})

    @websocket_api.websocket_command(
        {
            vol.Required("type"): "ryszard/settings/get_for_user",
            vol.Required("user_id"): str,
        }
    )
    @websocket_api.async_response
    async def websocket_get_settings_for_user(hass, connection, msg) -> None:
        if not getattr(connection.user, "is_admin", False):
            raise websocket_api.Unauthorized()
        data = await _load_data(hass)
        connection.send_result(msg["id"], {"settings": settings_for_user(data, msg["user_id"])})

    websocket_api.async_register_command(hass, websocket_get_settings)
    websocket_api.async_register_command(hass, websocket_update_settings)
    websocket_api.async_register_command(hass, websocket_get_settings_for_user)


async def _register_static_panel_asset(hass) -> None:
    panel_path = Path(__file__).with_name("www") / "panel.js"
    if hasattr(hass.http, "async_register_static_paths"):
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig("/api/ryszard/panel.js", str(panel_path), False)]
        )
        return
    hass.http.register_static_path("/api/ryszard/panel.js", str(panel_path), cache_headers=False)


def _store(hass):
    return hass.data[DOMAIN]["store"]


async def _load_data(hass) -> dict[str, Any]:
    data = await _store(hass).async_load()
    return data if isinstance(data, dict) else {"users": {}}
