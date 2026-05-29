from __future__ import annotations

import logging
from typing import Annotated, Any

from ai_server.agent_loop import AgentCallableSet
from ai_server.home_assistant.connection import HomeAssistantConnection
from ai_server.home_assistant.interfaces import JsonScalar


class HomeAssistantToolSet(AgentCallableSet):
    def __init__(self, connection: HomeAssistantConnection, *, logger_name: str | None = None) -> None:
        self._connection = connection
        self._current_user_message = ""
        self._current_location: str | None = None
        self._logger = logging.getLogger(logger_name or f"{__name__}.{type(self).__name__}")

    def set_request_context(self, *, user_message: str, location: str | None) -> None:
        self._current_user_message = user_message
        self._current_location = location

    @AgentCallableSet.tool(
        description=(
            "Inspect only: list controllable Home Assistant devices in an area or room. "
            "This does not modify anything. For an action request, call modify_device after selecting the device."
        )
    )
    async def list_devices(
        self,
        area_name: Annotated[str, "Area id, room name, or any Home Assistant-provided room alias."],
    ) -> list[dict[str, Any]] | dict[str, Any]:
        self._logger.debug("list_devices called area_name=%r", area_name)
        return await self._connection.list_devices(area_name)

    @AgentCallableSet.tool(
        description="Find controllable Home Assistant devices by type, alias, area, or query. Omit area to search globally."
    )
    async def find_devices(
        self,
        query: Annotated[str, "Optional search text matched against device, entity, alias, type, and area names."] = "",
        device_type: Annotated[str, "Optional Home Assistant device type/domain such as climate, light, switch, fan, or cover."] = "",
        area_name: Annotated[str, "Optional area id, room name, or room alias."] = "",
    ) -> list[dict[str, Any]] | dict[str, Any]:
        self._logger.debug("find_devices called query=%r device_type=%r area_name=%r", query, device_type, area_name)
        return await self._connection.find_devices(query=query, device_type=device_type, area_name=area_name)

    @AgentCallableSet.tool(
        description=(
            "Inspect only: list modifiable properties for a Home Assistant device. "
            "This does not modify anything. If the requested property and value are known, the next step must be modify_device."
        )
    )
    async def list_modifiable_properties(
        self,
        device: Annotated[str, "Device id, entity id, device name, entity name, or any entity alias."],
    ) -> list[dict[str, Any]] | dict[str, Any]:
        device = await self._resolve_context_reference(device)
        self._logger.debug("list_modifiable_properties called device=%r", device)
        return await self._connection.list_modifiable_properties(device)

    @AgentCallableSet.tool(description="Inspect common modifiable properties shared by multiple devices.")
    async def list_common_modifiable_properties(
        self,
        devices: Annotated[list[str], "Device ids, entity ids, device names, entity names, or aliases."],
    ) -> dict[str, Any]:
        self._logger.debug("list_common_modifiable_properties called devices=%r", devices)
        return await self._connection.list_common_modifiable_properties(devices)

    @AgentCallableSet.tool(description="Modify one Home Assistant device property. This is the only tool that changes devices.")
    async def modify_device(
        self,
        device: Annotated[str, "Device id, entity id, device name, entity name, or any entity alias."],
        property_name: Annotated[str, "Property name returned by list_modifiable_properties."],
        value: Annotated[JsonScalar, "Desired property value. Common aliases are accepted."],
    ) -> dict[str, Any]:
        device = await self._resolve_context_reference(device)
        self._logger.debug("modify_device called device=%r property_name=%r value=%r", device, property_name, value)
        return await self._connection.modify_device(device, property_name, value)

    @AgentCallableSet.tool(
        description=(
            "Modify the same property on multiple Home Assistant devices. "
            "Use only when the user explicitly asks for all/every matching devices or all target devices are in the requested room."
        )
    )
    async def modify_devices(
        self,
        devices: Annotated[list[str], "Device ids, entity ids, device names, entity names, or aliases."],
        property_name: Annotated[str, "Property name shared by the target devices."],
        value: Annotated[JsonScalar, "Desired property value. Common aliases are accepted."],
    ) -> dict[str, Any]:
        devices = [await self._resolve_context_reference(device) for device in devices]
        self._logger.debug("modify_devices called devices=%r property_name=%r value=%r", devices, property_name, value)
        result = await self._connection.modify_devices(
            devices,
            property_name,
            value,
            user_message=self._current_user_message,
            current_location=self._current_location,
        )
        if result.get("status") == "rejected":
            raise ValueError(_batch_rejection_message(result, property_name, value))
        return result

    async def _resolve_context_reference(self, device: str) -> str:
        if device.count(".") != 1:
            return device
        device_type, area_name = device.split(".", 1)
        if not device_type or not area_name:
            return device
        matches = await self._connection.find_devices(query="", device_type=device_type, area_name=area_name)
        if not isinstance(matches, list) or len(matches) != 1:
            return device
        match = matches[0]
        name = match.get("name") if isinstance(match, dict) else None
        return name if isinstance(name, str) and name else device


def _batch_rejection_message(result: dict[str, Any], property_name: str, value: JsonScalar) -> str:
    base_message = result.get("message")
    if not isinstance(base_message, str) or not base_message:
        base_message = "Batch modification rejected."

    current_area = result.get("current_area")
    current_area_id = current_area.get("area_id") if isinstance(current_area, dict) else None
    rejected_devices = result.get("rejected_devices")
    if isinstance(current_area_id, str) and isinstance(rejected_devices, list):
        matching_devices = [
            device
            for device in rejected_devices
            if isinstance(device, dict) and device.get("area_id") == current_area_id and isinstance(device.get("name"), str)
        ]
        if len(matching_devices) == 1:
            device_name = matching_devices[0]["name"]
            return (
                f"{base_message} Your next response must be a tool call, not text: "
                f'call modify_device(device="{device_name}", property_name="{property_name}", value={value!r}).'
            )

    return f"{base_message} Your next response must be a narrower modify_device tool call or a clarification question, not a success message."
