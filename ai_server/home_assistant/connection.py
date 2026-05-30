from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from aiohttp import ClientSession, WSMsgType

from ai_server.home_assistant.interfaces import (
    DEFAULT_CONTROLLABLE_DOMAINS,
    DEFAULT_INVENTORY_REFRESH_SECONDS,
    DEFAULT_INVENTORY_SUMMARY_SECONDS,
    DEVICE_TYPE_ALIASES,
    HOME_ASSISTANT_WEBSOCKET_PATH,
    PROPERTY_VALUE_ALIASES,
    HomeAssistantArea,
    HomeAssistantDevice,
    HomeAssistantEntity,
    HomeAssistantInventory,
    HomeAssistantOptions,
    HomeAssistantServiceCall,
    JsonScalar,
    ModifiableProperty,
)


INVENTORY_NOT_READY = {"error": "home_assistant_inventory_not_ready"}
CONNECTION_UNAVAILABLE = {"error": "home_assistant_connection_unavailable"}
RECONNECT_INITIAL_DELAY_SECONDS = 1.0
RECONNECT_MAX_DELAY_SECONDS = 30.0
GLOBAL_SCOPE_TERMS = (
    "all",
    "every",
    "whole house",
    "everywhere",
    "wszystkie",
    "wszyscy",
    "każde",
    "kazde",
    "każdy",
    "kazdy",
    "wszędzie",
    "wszedzie",
    "cały dom",
    "caly dom",
    "całym domu",
    "calym domu",
    "w całym domu",
    "w calym domu",
)


class HomeAssistantConnection:
    def __init__(self, options: HomeAssistantOptions) -> None:
        self._options = options
        self._logger = logging.getLogger(f"{__name__}.HomeAssistantConnection[{options.url}]")
        self._inventory: HomeAssistantInventory | None = None
        self._raw_areas: list[dict[str, Any]] = []
        self._raw_devices: list[dict[str, Any]] = []
        self._raw_entity_details: list[dict[str, Any]] = []
        self._states_by_entity_id: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._registry_refresh_task: asyncio.Task[None] | None = None
        self._state_subscription_task: asyncio.Task[None] | None = None
        self._last_inventory_summary_at: float | None = None
        self._closed = False

    @property
    def inventory(self) -> HomeAssistantInventory | None:
        return self._inventory

    async def start(self) -> None:
        self._closed = False
        if self._registry_refresh_task is None or self._registry_refresh_task.done():
            self._logger.debug(
                "starting Home Assistant registry refresh task interval_seconds=%s",
                self._options.inventory_refresh_seconds,
            )
            self._registry_refresh_task = asyncio.create_task(self._registry_refresh_loop())
        if self._state_subscription_task is None or self._state_subscription_task.done():
            self._logger.debug("starting Home Assistant state subscription task")
            self._state_subscription_task = asyncio.create_task(self._state_subscription_loop())

    async def close(self) -> None:
        self._closed = True
        tasks = [task for task in (self._registry_refresh_task, self._state_subscription_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._registry_refresh_task = None
        self._state_subscription_task = None
        self._logger.debug("Home Assistant connection stopped")

    async def list_devices(self, area_name: str) -> list[dict[str, Any]] | dict[str, Any]:
        inventory = self._inventory
        if inventory is None:
            return dict(INVENTORY_NOT_READY)

        area = _resolve_area(inventory, area_name)
        if isinstance(area, dict):
            return area

        return [
            _device_to_mapping(device, inventory)
            for device in inventory.devices_by_area.get(area.area_id, ())
        ]

    async def find_devices(
        self,
        query: str = "",
        device_type: str = "",
        area_name: str = "",
    ) -> list[dict[str, Any]] | dict[str, Any]:
        inventory = self._inventory
        if inventory is None:
            return dict(INVENTORY_NOT_READY)

        area: HomeAssistantArea | None = None
        if area_name:
            resolved_area = _resolve_area(inventory, area_name)
            if isinstance(resolved_area, dict):
                return resolved_area
            area = resolved_area

        return [
            _device_to_mapping(device, inventory)
            for device in _find_devices(inventory, query=query, device_type=device_type, area=area)
        ]

    async def list_modifiable_properties(self, device: str) -> list[dict[str, Any]] | dict[str, Any]:
        inventory = self._inventory
        if inventory is None:
            return dict(INVENTORY_NOT_READY)

        resolved_device = _resolve_device(inventory, device)
        if isinstance(resolved_device, dict):
            return resolved_device

        return [_property_to_mapping(property_info) for property_info in resolved_device.properties]

    async def list_common_modifiable_properties(self, devices: list[str]) -> dict[str, Any]:
        inventory = self._inventory
        if inventory is None:
            return dict(INVENTORY_NOT_READY)

        resolved_devices, errors = _resolve_devices_for_batch(inventory, devices)
        return {
            "devices": [_device_to_mapping(device, inventory) for device in resolved_devices],
            "common_properties": _common_property_mappings(resolved_devices),
            "errors": errors,
        }

    async def modify_device(self, device: str, property_name: str, value: JsonScalar) -> dict[str, Any]:
        inventory = self._inventory
        if inventory is None:
            return dict(INVENTORY_NOT_READY)

        resolved_device = _resolve_device(inventory, device)
        if isinstance(resolved_device, dict):
            return resolved_device

        result = await self._modify_resolved_device(resolved_device, property_name, value)
        if result.get("status") in {"skipped", "failed"}:
            result = {"device_id": resolved_device.device_id, **result}
        return result

    async def modify_devices(
        self,
        devices: list[str],
        property_name: str,
        value: JsonScalar,
        *,
        user_message: str = "",
        current_area: str | None = None,
    ) -> dict[str, Any]:
        inventory = self._inventory
        if inventory is None:
            return dict(INVENTORY_NOT_READY)

        resolved_devices, errors = _resolve_devices_for_batch(inventory, devices)
        scope_rejection = _batch_scope_rejection(
            inventory,
            resolved_devices,
            user_message=user_message,
            current_area=current_area,
        )
        if scope_rejection is not None:
            return scope_rejection

        results = []
        for resolved_device in resolved_devices:
            result = await self._modify_resolved_device(resolved_device, property_name, value)
            result["device_id"] = resolved_device.device_id
            result["device_name"] = resolved_device.name
            results.append(result)

        return {
            "status": "ok" if results and all(result.get("status") == "ok" for result in results) and not errors else "partial",
            "results": results,
            "errors": errors,
        }

    def system_prompt_context(self, *, user: str | None, area: str | None) -> str:
        inventory = self._inventory
        area_text = area or "unknown"
        user_text = user or "unknown"
        area_lines = []
        if inventory is None:
            area_lines.append("- Inventory is not ready yet.")
        else:
            for area in sorted(inventory.areas_by_id.values(), key=lambda item: item.name.casefold()):
                alias_text = ", ".join(area.aliases) if area.aliases else "none"
                area_lines.append(f"- area_id={area.area_id}; name={area.name}; aliases={alias_text}")

        return "\n".join(
            [
                "You are a Home Assistant control agent.",
                "Reply in Polish. Use tools to inspect devices and perform modifications.",
                "If the user does not name an area, prefer the current area when it is known.",
                "Critical action rules:",
                "- list_devices and list_modifiable_properties only inspect Home Assistant; they never change devices.",
                "- For user requests to turn on, turn off, set, change, open, close, dim, brighten, heat, cool, or adjust a device, you must call modify_device.",
                "- Never claim that a device was changed unless modify_device or modify_devices returned status=ok for that exact requested change.",
                "- If you inspected devices but did not call modify_device, say that you found the device but have not changed it yet.",
                "- If the current area has exactly one device matching the requested type or alias, that device is specific enough; call modify_device without asking for confirmation.",
                "- Do not ask for confirmation for ordinary reversible smart-home changes unless multiple matching devices or values remain ambiguous.",
                "- If you cannot choose the device, property, or value after inspecting available devices/properties, ask a clarification question instead of claiming success.",
                "- For explicit global or all-device requests, use find_devices. If multiple devices match, inspect common properties with list_common_modifiable_properties, then call modify_devices.",
                "- If modify_devices returns status=rejected, obey the message in the tool result and make the narrower modify_device call instead of claiming success.",
                "- For requests about klimatyzacja, klima, air conditioning, or AC, prefer devices with type=climate.",
                "- To turn climate or air conditioning off, prefer modify_device with property_name=hvac_mode and value=off when hvac_mode is available.",
                "- To turn multiple climate or air conditioning devices off, prefer modify_devices with property_name=hvac_mode and value=off when hvac_mode is available.",
                "- After list_modifiable_properties returns the needed property for an action request, your next assistant response must be a modify_device tool call, not text.",
                "- Final confirmation must be exactly one short Polish sentence. Do not use bullet lists, markdown, or explanations.",
                '- Example: user says "wyłącz klimatyzację" in Office, list_devices shows one climate device "Study air conditioner", and list_modifiable_properties shows hvac_mode with off; then call modify_device(device="Study air conditioner", property_name="hvac_mode", value="off").',
                '- Example: user says "wyłącz wszystkie klimatyzatory"; call find_devices(query="klimatyzator klima klimatyzacja", device_type="climate"), then list_common_modifiable_properties, then modify_devices for every matching device.',
                """
Scope rules:
- If the user names an area or room, restrict the action to that area.
- If the user does not name an area, prefer the current area only for singular or local requests.
- If the user says "all", "every", "wszystkie", "każde", "wszędzie", "w całym domu", or similar global wording, do not restrict to the current area. Search all areas.
- If the user does not use global wording, never modify devices outside the named area or current area.
- For requests about all devices of a type, find every matching device before modifying any of them.
- For multiple matching devices, modify every matching device unless the request is ambiguous or unsafe.
- Do not report success for "all" unless every matching device was attempted. Still reply in one sentence.
""",
                f"Current user: {user_text}",
                f"Current area: {area_text}",
                "Known areas and aliases:",
                *area_lines,
                "Device type vocabulary:",
                _format_device_type_vocabulary(),
                "Value vocabulary:",
                _format_value_vocabulary(),
            ]
        )

    async def _modify_resolved_device(
        self,
        resolved_device: HomeAssistantDevice,
        property_name: str,
        value: JsonScalar,
    ) -> dict[str, Any]:
        property_info = resolved_device.property_by_name.get(property_name)
        if property_info is None:
            return {
                "status": "skipped",
                "error": "unsupported_property",
                "property_name": property_name,
                "known_properties": sorted(resolved_device.property_by_name),
            }

        try:
            service_call = _build_service_call(property_info, value)
        except ValueError as exc:
            return {
                "status": "skipped",
                "error": "invalid_property_value",
                "property_name": property_name,
                "message": str(exc),
            }

        try:
            await _call_home_assistant_service(self._options, service_call, self._logger)
        except Exception as exc:
            self._logger.exception(
                "Home Assistant service call failed device_id=%s property=%s",
                resolved_device.device_id,
                property_info.property_name,
            )
            return {
                "status": "failed",
                "error": "service_call_failed",
                "property_name": property_info.property_name,
                "message": str(exc),
                "service": f"{service_call.domain}.{service_call.service}",
                "entity_id": property_info.entity_id,
            }
        return {
            "status": "ok",
            "service": f"{service_call.domain}.{service_call.service}",
            "entity_id": property_info.entity_id,
        }

    async def _registry_refresh_loop(self) -> None:
        while not self._closed:
            try:
                await self.refresh_inventory()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("failed to refresh Home Assistant inventory")
            await asyncio.sleep(self._options.inventory_refresh_seconds)

    async def refresh_inventory(self) -> None:
        async with _HomeAssistantWebSocket(self._options, self._logger, log_traffic=False) as client:
            raw_areas = await client.command({"type": "config/area_registry/list"})
            raw_devices = await client.command({"type": "config/device_registry/list"})
            entity_registry = await client.command({"type": "config/entity_registry/list"})
            raw_states = await client.command({"type": "get_states"})

            raw_entity_details = []
            for entity in entity_registry:
                entity_id = entity.get("entity_id")
                if not isinstance(entity_id, str):
                    continue
                if entity_id.split(".", 1)[0] not in self._options.controllable_domains:
                    continue
                if entity.get("disabled_by") is not None or entity.get("hidden_by") is not None:
                    continue
                raw_entity_details.append(await client.command({"type": "config/entity_registry/get", "entity_id": entity_id}))

        async with self._lock:
            self._raw_areas = raw_areas
            self._raw_devices = raw_devices
            self._raw_entity_details = raw_entity_details
            self._states_by_entity_id = {
                state["entity_id"]: state
                for state in raw_states
                if isinstance(state, dict) and isinstance(state.get("entity_id"), str)
            }
            self._rebuild_inventory_locked()
            assert self._inventory is not None
            self._log_inventory_summary_if_due(self._inventory)

    async def _state_subscription_loop(self) -> None:
        delay = RECONNECT_INITIAL_DELAY_SECONDS
        while not self._closed:
            try:
                await self._run_state_subscription()
                delay = RECONNECT_INITIAL_DELAY_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Home Assistant state subscription failed; reconnecting in %ss", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY_SECONDS)

    async def _run_state_subscription(self) -> None:
        async with _HomeAssistantWebSocket(self._options, self._logger, log_traffic=False) as client:
            await client.command({"type": "subscribe_events", "event_type": "state_changed"})
            self._logger.info("subscribed to Home Assistant state_changed events")
            while not self._closed:
                event = await client.receive_event()
                await self._handle_state_changed(event)

    async def _handle_state_changed(self, event: dict[str, Any]) -> None:
        if event.get("event_type") != "state_changed":
            return
        data = event.get("data")
        if not isinstance(data, dict):
            return
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if not isinstance(entity_id, str) or not isinstance(new_state, dict):
            return
        async with self._lock:
            self._states_by_entity_id[entity_id] = new_state
            if self._raw_entity_details:
                self._rebuild_inventory_locked()
        self._logger.debug("updated Home Assistant cached state entity_id=%s state=%s", entity_id, new_state.get("state"))

    def _rebuild_inventory_locked(self) -> None:
        self._inventory = _build_inventory(
            raw_areas=self._raw_areas,
            raw_devices=self._raw_devices,
            raw_entity_details=self._raw_entity_details,
            raw_states=list(self._states_by_entity_id.values()),
            controllable_domains=self._options.controllable_domains,
        )

    def _log_inventory_summary_if_due(self, inventory: HomeAssistantInventory) -> None:
        now = asyncio.get_running_loop().time()
        if self._last_inventory_summary_at is None:
            self._logger.debug("refreshed Home Assistant inventory initial_summary=%s", _inventory_debug_summary(inventory))
            self._last_inventory_summary_at = now
            return
        if now - self._last_inventory_summary_at >= self._options.inventory_summary_seconds:
            self._logger.debug(
                "refreshed Home Assistant inventory heartbeat areas=%s devices=%s",
                len(inventory.areas_by_id),
                len(inventory.devices_by_id),
            )
            self._last_inventory_summary_at = now


def parse_home_assistant_options(options: dict[str, Any]) -> HomeAssistantOptions:
    raw_options = options.get("home_assistant")
    if not isinstance(raw_options, dict):
        raise ValueError("agent.home_assistant must be a mapping")

    url = raw_options.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("agent.home_assistant.url must be a non-empty string")

    token = raw_options.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("agent.home_assistant.token must be a non-empty string")

    raw_domains = raw_options.get("controllable_domains", DEFAULT_CONTROLLABLE_DOMAINS)
    if not isinstance(raw_domains, list | tuple) or not raw_domains:
        raise ValueError("agent.home_assistant.controllable_domains must be a non-empty list")
    domains = []
    for domain in raw_domains:
        if not isinstance(domain, str) or not domain:
            raise ValueError("agent.home_assistant.controllable_domains values must be non-empty strings")
        domains.append(domain.lower())

    inventory_refresh_seconds = raw_options.get("inventory_refresh_seconds", DEFAULT_INVENTORY_REFRESH_SECONDS)
    if (
        not isinstance(inventory_refresh_seconds, (int, float))
        or isinstance(inventory_refresh_seconds, bool)
        or inventory_refresh_seconds <= 0
    ):
        raise ValueError("agent.home_assistant.inventory_refresh_seconds must be a positive number")

    inventory_summary_seconds = raw_options.get("inventory_summary_seconds", DEFAULT_INVENTORY_SUMMARY_SECONDS)
    if (
        not isinstance(inventory_summary_seconds, (int, float))
        or isinstance(inventory_summary_seconds, bool)
        or inventory_summary_seconds <= 0
    ):
        raise ValueError("agent.home_assistant.inventory_summary_seconds must be a positive number")

    return HomeAssistantOptions(
        url=url.rstrip("/"),
        token=token,
        controllable_domains=tuple(dict.fromkeys(domains)),
        inventory_refresh_seconds=float(inventory_refresh_seconds),
        inventory_summary_seconds=float(inventory_summary_seconds),
    )


async def _call_home_assistant_service(
    options: HomeAssistantOptions,
    service_call: HomeAssistantServiceCall,
    logger: logging.Logger,
) -> None:
    payload: dict[str, Any] = {
        "type": "call_service",
        "domain": service_call.domain,
        "service": service_call.service,
        "target": {"entity_id": service_call.entity_id},
    }
    if service_call.service_data:
        payload["service_data"] = service_call.service_data

    logger.debug(
        "calling Home Assistant service domain=%s service=%s entity_id=%s service_data=%s",
        service_call.domain,
        service_call.service,
        service_call.entity_id,
        service_call.service_data,
    )
    async with _HomeAssistantWebSocket(options, logger, log_traffic=True) as client:
        await client.command(payload)
    logger.debug(
        "Home Assistant service call completed domain=%s service=%s entity_id=%s",
        service_call.domain,
        service_call.service,
        service_call.entity_id,
    )


class _HomeAssistantWebSocket:
    def __init__(self, options: HomeAssistantOptions, logger: logging.Logger, *, log_traffic: bool) -> None:
        self._options = options
        self._logger = logger
        self._log_traffic = log_traffic
        self._session: ClientSession | None = None
        self._websocket = None
        self._next_id = 1

    async def __aenter__(self) -> "_HomeAssistantWebSocket":
        self._session = ClientSession()
        try:
            websocket_url = _websocket_url(self._options.url)
            if self._log_traffic:
                self._logger.debug("Home Assistant WS connecting url=%s", websocket_url)
            self._websocket = await self._session.ws_connect(websocket_url, heartbeat=30)

            auth_required = await self._websocket.receive_json(timeout=15)
            if auth_required.get("type") != "auth_required":
                raise ValueError("Home Assistant WebSocket did not request auth")

            await self._websocket.send_json({"type": "auth", "access_token": self._options.token})
            auth_result = await self._websocket.receive_json(timeout=15)
            if auth_result.get("type") != "auth_ok":
                raise ValueError("Home Assistant WebSocket auth failed")

            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._websocket is not None:
            await self._websocket.close()
        if self._session is not None:
            await self._session.close()

    async def command(self, payload: dict[str, Any]) -> Any:
        if self._websocket is None:
            raise RuntimeError("Home Assistant WebSocket is not connected")

        command_id = self._next_id
        self._next_id += 1
        command_payload = {"id": command_id, **payload}
        if self._log_traffic:
            self._logger.debug("Home Assistant WS request type=%s id=%s payload=%s", payload.get("type"), command_id, payload)
        await self._websocket.send_json(command_payload)

        while True:
            message = await self._websocket.receive(timeout=15)
            body = _parse_ws_text_message(message)
            if body.get("id") != command_id:
                continue
            if not body.get("success"):
                raise ValueError(f"Home Assistant command failed type={payload.get('type')} error={body.get('error')}")
            result = body.get("result")
            if self._log_traffic:
                self._logger.debug(
                    "Home Assistant WS response type=%s id=%s result_summary=%s",
                    payload.get("type"),
                    command_id,
                    _summarize_ws_result(result),
                )
            return result

    async def receive_event(self) -> dict[str, Any]:
        if self._websocket is None:
            raise RuntimeError("Home Assistant WebSocket is not connected")

        while True:
            body = _parse_ws_text_message(await self._websocket.receive(timeout=60))
            if body.get("type") == "event":
                event = body.get("event")
                if isinstance(event, dict):
                    return event


def _parse_ws_text_message(message) -> dict[str, Any]:
    if message.type != WSMsgType.TEXT:
        raise ValueError(f"unexpected Home Assistant WebSocket message type: {message.type}")
    body = json.loads(message.data)
    if not isinstance(body, dict):
        raise ValueError("Home Assistant WebSocket message must be an object")
    return body


def _build_inventory(
    raw_areas: list[dict[str, Any]],
    raw_devices: list[dict[str, Any]],
    raw_entity_details: list[dict[str, Any]],
    raw_states: list[dict[str, Any]],
    controllable_domains: tuple[str, ...],
) -> HomeAssistantInventory:
    states_by_entity_id = {
        state["entity_id"]: state
        for state in raw_states
        if isinstance(state, dict) and isinstance(state.get("entity_id"), str)
    }
    raw_devices_by_id = {
        device["id"]: device
        for device in raw_devices
        if isinstance(device, dict) and isinstance(device.get("id"), str)
    }

    areas_by_id = {
        area["area_id"]: HomeAssistantArea(
            area_id=area["area_id"],
            name=_first_string(area.get("name"), area["area_id"]),
            aliases=_clean_aliases(area.get("aliases")),
        )
        for area in raw_areas
        if isinstance(area, dict) and isinstance(area.get("area_id"), str)
    }

    entities_by_device_id: dict[str, list[HomeAssistantEntity]] = {}
    for detail in raw_entity_details:
        entity_id = detail.get("entity_id")
        device_id = detail.get("device_id")
        if not isinstance(entity_id, str) or not isinstance(device_id, str):
            continue
        domain = entity_id.split(".", 1)[0]
        if domain not in controllable_domains:
            continue

        state = states_by_entity_id.get(entity_id)
        device = raw_devices_by_id.get(device_id)
        effective_area_id = detail.get("area_id") or (device or {}).get("area_id")
        if not isinstance(effective_area_id, str) or effective_area_id not in areas_by_id:
            continue
        if state is None:
            continue

        entity = HomeAssistantEntity(
            entity_id=entity_id,
            device_id=device_id,
            area_id=effective_area_id,
            domain=domain,
            name=_entity_name(detail, state, entity_id),
            aliases=_clean_aliases(detail.get("aliases")),
            state=_first_string(state.get("state"), ""),
            attributes=dict(state.get("attributes") if isinstance(state.get("attributes"), dict) else {}),
        )
        entities_by_device_id.setdefault(device_id, []).append(entity)

    devices_by_id = {}
    for device_id, entities in entities_by_device_id.items():
        raw_device = raw_devices_by_id.get(device_id, {})
        area_id = entities[0].area_id
        domains = sorted({entity.domain for entity in entities})
        properties = tuple(
            property_info
            for entity in entities
            for property_info in _properties_for_entity(entity)
        )
        if not properties:
            continue

        device = HomeAssistantDevice(
            device_id=device_id,
            area_id=area_id,
            name=_device_name(raw_device, entities[0]),
            aliases=_merge_aliases(entity.aliases for entity in entities),
            device_type=domains[0] if len(domains) == 1 else "multi_domain",
            entities=tuple(sorted(entities, key=lambda entity: entity.entity_id)),
            properties=properties,
        )
        devices_by_id[device_id] = device

    devices_by_area: dict[str, tuple[HomeAssistantDevice, ...]] = {}
    for area_id in areas_by_id:
        devices_by_area[area_id] = tuple(
            sorted(
                (device for device in devices_by_id.values() if device.area_id == area_id),
                key=lambda device: device.name.casefold(),
            )
        )

    return HomeAssistantInventory(
        areas_by_id=areas_by_id,
        devices_by_id=devices_by_id,
        devices_by_area=devices_by_area,
        area_lookup=_build_area_lookup(areas_by_id),
        device_lookup=_build_device_lookup(devices_by_id),
    )


def _resolve_area(inventory: HomeAssistantInventory, area_name: str) -> HomeAssistantArea | dict[str, Any]:
    matches = inventory.area_lookup.get(_normalize_lookup(area_name), ())
    if len(matches) == 1:
        return inventory.areas_by_id[matches[0]]
    if not matches:
        return {
            "error": "unknown_area",
            "area": area_name,
            "known_areas": [_area_to_mapping(area) for area in inventory.areas_by_id.values()],
        }
    return {
        "error": "ambiguous_area",
        "area": area_name,
        "candidates": [_area_to_mapping(inventory.areas_by_id[area_id]) for area_id in matches],
    }


def _resolve_device(inventory: HomeAssistantInventory, device: str) -> HomeAssistantDevice | dict[str, Any]:
    matches = inventory.device_lookup.get(_normalize_lookup(device), ())
    if len(matches) == 1:
        return inventory.devices_by_id[matches[0]]
    if not matches:
        return {
            "error": "unknown_device",
            "device": device,
            "known_devices": [_device_to_mapping(device_info, inventory) for device_info in inventory.devices_by_id.values()],
        }
    return {
        "error": "ambiguous_device",
        "device": device,
        "candidates": [_device_to_mapping(inventory.devices_by_id[device_id], inventory) for device_id in matches],
    }


def _summarize_ws_result(result: Any) -> Any:
    if isinstance(result, list):
        return {"type": "list", "count": len(result), "sample": result[:2]}
    if isinstance(result, dict):
        return {"type": "object", "keys": sorted(result)[:20], "value": result if len(result) <= 10 else None}
    return result


def _inventory_debug_summary(inventory: HomeAssistantInventory) -> dict[str, Any]:
    return {
        "areas": [
            {
                "area_id": area.area_id,
                "name": area.name,
                "aliases": list(area.aliases),
                "device_count": len(inventory.devices_by_area.get(area.area_id, ())),
            }
            for area in sorted(inventory.areas_by_id.values(), key=lambda item: item.name.casefold())
        ],
        "devices": [
            {
                "device_id": device.device_id,
                "name": device.name,
                "type": device.device_type,
                "area": inventory.areas_by_id[device.area_id].name,
                "aliases": list(device.aliases),
                "properties": [property_info.property_name for property_info in device.properties],
            }
            for device in sorted(inventory.devices_by_id.values(), key=lambda item: item.name.casefold())
        ],
    }


def _properties_for_entity(entity: HomeAssistantEntity) -> tuple[ModifiableProperty, ...]:
    properties = [
        ModifiableProperty(
            property_name="on",
            entity_id=entity.entity_id,
            domain=entity.domain,
            value_type="boolean",
            description="Turn the device on or off.",
            allowed_values=(True, False),
        )
    ]

    if entity.domain == "climate":
        properties.append(
            ModifiableProperty(
                property_name="target_temperature",
                entity_id=entity.entity_id,
                domain=entity.domain,
                value_type="number",
                description="Set target temperature in Celsius.",
                min_value=_optional_float(entity.attributes.get("min_temp")),
                max_value=_optional_float(entity.attributes.get("max_temp")),
                step=_optional_float(entity.attributes.get("target_temp_step")),
            )
        )
        hvac_modes = _string_tuple(entity.attributes.get("hvac_modes"))
        if hvac_modes:
            properties.append(
                ModifiableProperty(
                    property_name="hvac_mode",
                    entity_id=entity.entity_id,
                    domain=entity.domain,
                    value_type="string",
                    description="Set climate HVAC mode.",
                    allowed_values=hvac_modes,
                )
            )
        fan_modes = _string_tuple(entity.attributes.get("fan_modes"))
        if fan_modes:
            properties.append(
                ModifiableProperty(
                    property_name="fan_mode",
                    entity_id=entity.entity_id,
                    domain=entity.domain,
                    value_type="string",
                    description="Set climate fan mode.",
                    allowed_values=fan_modes,
                )
            )

    if entity.domain == "light" and _light_supports_brightness(entity):
        properties.append(
            ModifiableProperty(
                property_name="brightness_percent",
                entity_id=entity.entity_id,
                domain=entity.domain,
                value_type="number",
                description="Set light brightness percentage.",
                min_value=0,
                max_value=100,
                step=1,
            )
        )

    if entity.domain == "fan" and "percentage" in entity.attributes:
        properties.append(
            ModifiableProperty(
                property_name="percentage",
                entity_id=entity.entity_id,
                domain=entity.domain,
                value_type="number",
                description="Set fan speed percentage.",
                min_value=0,
                max_value=100,
                step=1,
            )
        )

    if entity.domain == "cover" and "current_position" in entity.attributes:
        properties.append(
            ModifiableProperty(
                property_name="position",
                entity_id=entity.entity_id,
                domain=entity.domain,
                value_type="number",
                description="Set cover position percentage.",
                min_value=0,
                max_value=100,
                step=1,
            )
        )

    return tuple(properties)


def _build_service_call(property_info: ModifiableProperty, value: JsonScalar) -> HomeAssistantServiceCall:
    normalized_value = _normalize_property_value(property_info, value)
    if property_info.property_name == "on":
        return HomeAssistantServiceCall(
            domain=property_info.domain,
            service="turn_on" if normalized_value is True else "turn_off",
            entity_id=property_info.entity_id,
            service_data={},
        )
    if property_info.property_name == "target_temperature":
        return HomeAssistantServiceCall("climate", "set_temperature", property_info.entity_id, {"temperature": normalized_value})
    if property_info.property_name == "hvac_mode":
        return HomeAssistantServiceCall("climate", "set_hvac_mode", property_info.entity_id, {"hvac_mode": normalized_value})
    if property_info.property_name == "fan_mode":
        return HomeAssistantServiceCall("climate", "set_fan_mode", property_info.entity_id, {"fan_mode": normalized_value})
    if property_info.property_name == "brightness_percent":
        return HomeAssistantServiceCall("light", "turn_on", property_info.entity_id, {"brightness_pct": normalized_value})
    if property_info.property_name == "percentage":
        return HomeAssistantServiceCall("fan", "set_percentage", property_info.entity_id, {"percentage": normalized_value})
    if property_info.property_name == "position":
        return HomeAssistantServiceCall("cover", "set_cover_position", property_info.entity_id, {"position": normalized_value})
    raise ValueError(f"unsupported property: {property_info.property_name}")


def _normalize_property_value(property_info: ModifiableProperty, value: JsonScalar) -> JsonScalar:
    if property_info.value_type == "boolean":
        normalized_value = _normalize_alias(property_info.property_name, value)
        if isinstance(normalized_value, bool):
            return normalized_value
        if isinstance(value, bool):
            return value
        raise ValueError(f"{property_info.property_name} must be true or false")

    if property_info.value_type == "number":
        number = _parse_number(value, property_info.property_name)
        if property_info.min_value is not None and number < property_info.min_value:
            raise ValueError(f"{property_info.property_name} must be at least {property_info.min_value}")
        if property_info.max_value is not None and number > property_info.max_value:
            raise ValueError(f"{property_info.property_name} must be at most {property_info.max_value}")
        return int(number) if number.is_integer() else number

    if property_info.value_type == "string":
        normalized_value = _normalize_alias(property_info.property_name, value)
        if isinstance(normalized_value, str):
            value_text = normalized_value
        elif isinstance(value, str):
            value_text = value
        else:
            raise ValueError(f"{property_info.property_name} must be a string")

        allowed_by_normalized = {_normalize_lookup(str(allowed)): allowed for allowed in property_info.allowed_values}
        matched_value = allowed_by_normalized.get(_normalize_lookup(value_text))
        if matched_value is not None:
            return matched_value
        raise ValueError(
            f"{property_info.property_name} must be one of: {', '.join(str(value) for value in property_info.allowed_values)}"
        )

    raise ValueError(f"unsupported value type: {property_info.value_type}")


def _normalize_alias(property_name: str, value: JsonScalar) -> JsonScalar | None:
    alias_map = PROPERTY_VALUE_ALIASES.get(property_name, {})
    if isinstance(value, str):
        normalized_value = _normalize_lookup(value)
        for canonical_value, aliases in alias_map.items():
            if normalized_value == _normalize_lookup(str(canonical_value)):
                return canonical_value
            if normalized_value in {_normalize_lookup(alias) for alias in aliases}:
                return canonical_value
    return None


def _format_device_type_vocabulary() -> str:
    return "\n".join(f"- {device_type}: {', '.join(aliases)}" for device_type, aliases in DEVICE_TYPE_ALIASES.items())


def _format_value_vocabulary() -> str:
    lines = []
    for property_name, value_aliases in PROPERTY_VALUE_ALIASES.items():
        for canonical_value, aliases in value_aliases.items():
            lines.append(f"- {property_name}: {canonical_value} aliases: {', '.join(aliases)}")
    return "\n".join(lines)


def _find_devices(
    inventory: HomeAssistantInventory,
    *,
    query: str,
    device_type: str,
    area: HomeAssistantArea | None,
) -> list[HomeAssistantDevice]:
    normalized_device_type = _resolve_device_type(device_type) or _infer_device_type(query)
    query_terms = _query_terms_without_device_type_aliases(query, normalized_device_type)
    matches = []
    for device in sorted(inventory.devices_by_id.values(), key=lambda item: item.name.casefold()):
        if area is not None and device.area_id != area.area_id:
            continue
        if normalized_device_type and device.device_type != normalized_device_type:
            continue
        if query_terms and not _device_matches_query(inventory, device, query_terms):
            continue
        matches.append(device)
    return matches


def _resolve_device_type(device_type: str) -> str:
    normalized_device_type = _normalize_lookup(device_type)
    if not normalized_device_type:
        return ""
    for canonical_type, aliases in DEVICE_TYPE_ALIASES.items():
        if normalized_device_type == _normalize_lookup(canonical_type):
            return canonical_type
        if normalized_device_type in {_normalize_lookup(alias) for alias in aliases}:
            return canonical_type
    return normalized_device_type


def _infer_device_type(query: str) -> str:
    normalized_query = _normalize_lookup(query)
    if not normalized_query:
        return ""
    for canonical_type, aliases in DEVICE_TYPE_ALIASES.items():
        for value in (canonical_type, *aliases):
            if _normalized_phrase_in_query(value, normalized_query):
                return canonical_type
    return ""


def _query_terms_without_device_type_aliases(query: str, device_type: str) -> tuple[str, ...]:
    normalized_alias_terms = set()
    if device_type:
        aliases = DEVICE_TYPE_ALIASES.get(device_type, ())
        for value in (device_type, *aliases):
            normalized_alias_terms.update(_query_terms(value))
    return tuple(term for term in _query_terms(query) if term not in normalized_alias_terms)


def _query_terms(query: str) -> tuple[str, ...]:
    return tuple(term for term in (_normalize_lookup(part) for part in query.split()) if term)


def _normalized_phrase_in_query(value: str, normalized_query: str) -> bool:
    normalized_value = _normalize_lookup(value)
    if not normalized_value:
        return False
    return re.search(rf"(^|\s){re.escape(normalized_value)}($|\s)", normalized_query) is not None


def _device_matches_query(
    inventory: HomeAssistantInventory,
    device: HomeAssistantDevice,
    query_terms: tuple[str, ...],
) -> bool:
    haystack = " ".join(_device_search_values(inventory, device))
    normalized_haystack = _normalize_lookup(haystack)
    return any(term in normalized_haystack for term in query_terms)


def _device_search_values(inventory: HomeAssistantInventory, device: HomeAssistantDevice) -> tuple[str, ...]:
    area = inventory.areas_by_id[device.area_id]
    values = [
        device.device_id,
        device.name,
        device.device_type,
        area.area_id,
        area.name,
        *area.aliases,
        *device.aliases,
    ]
    for entity in device.entities:
        values.extend((entity.entity_id, entity.name, *entity.aliases))
    return tuple(values)


def _resolve_devices_for_batch(
    inventory: HomeAssistantInventory,
    devices: list[str],
) -> tuple[list[HomeAssistantDevice], list[dict[str, Any]]]:
    resolved_devices = []
    errors = []
    seen_device_ids = set()
    for requested_device in devices:
        resolved_device = _resolve_device(inventory, requested_device)
        if isinstance(resolved_device, dict):
            errors.append({"device": requested_device, **resolved_device})
            continue
        if resolved_device.device_id in seen_device_ids:
            continue
        seen_device_ids.add(resolved_device.device_id)
        resolved_devices.append(resolved_device)
    return resolved_devices, errors


def _batch_scope_rejection(
    inventory: HomeAssistantInventory,
    devices: list[HomeAssistantDevice],
    *,
    user_message: str,
    current_area: str | None,
) -> dict[str, Any] | None:
    if len(devices) <= 1:
        return None
    if not user_message:
        return None
    if _has_global_scope(user_message):
        return None

    resolved_current_area = _resolve_current_area(inventory, current_area)
    if resolved_current_area is not None and all(device.area_id == resolved_current_area.area_id for device in devices):
        return None

    rejected_devices = [_device_to_mapping(device, inventory) for device in devices]
    target_area_text = resolved_current_area.name if resolved_current_area is not None else "the named/current area"
    return {
        "status": "rejected",
        "error": "batch_scope_not_allowed",
        "message": (
            "The user did not ask for all matching devices. Do not claim success. "
            f"Use modify_device for the single matching device in {target_area_text}, "
            "or ask a clarification question if no single device is clear."
        ),
        "current_area_name": current_area or "",
        "current_area": _area_to_mapping(resolved_current_area) if resolved_current_area is not None else None,
        "rejected_devices": rejected_devices,
    }


def _resolve_current_area(inventory: HomeAssistantInventory, current_area: str | None) -> HomeAssistantArea | None:
    if not current_area:
        return None
    area = _resolve_area(inventory, current_area)
    if isinstance(area, dict):
        return None
    return area


def _has_global_scope(user_message: str) -> bool:
    normalized_message = _normalize_lookup(user_message)
    return any(_normalized_phrase_in_query(term, normalized_message) for term in GLOBAL_SCOPE_TERMS)


def _common_property_mappings(devices: list[HomeAssistantDevice]) -> list[dict[str, Any]]:
    if not devices:
        return []
    common_property_names = set(devices[0].property_by_name)
    for device in devices[1:]:
        common_property_names &= set(device.property_by_name)

    properties = []
    for property_name in sorted(common_property_names):
        property_infos = [device.property_by_name[property_name] for device in devices]
        common_property = _merge_common_property(property_infos)
        if common_property is not None:
            properties.append(_property_to_mapping(common_property))
    return properties


def _merge_common_property(property_infos: list[ModifiableProperty]) -> ModifiableProperty | None:
    first = property_infos[0]
    value_type = first.value_type
    if any(property_info.value_type != value_type for property_info in property_infos):
        return None

    allowed_values: tuple[JsonScalar, ...] = ()
    if any(property_info.allowed_values for property_info in property_infos):
        allowed_values = tuple(
            value
            for value in first.allowed_values
            if all(value in property_info.allowed_values for property_info in property_infos[1:])
        )
        if not allowed_values:
            return None

    min_values = [property_info.min_value for property_info in property_infos if property_info.min_value is not None]
    max_values = [property_info.max_value for property_info in property_infos if property_info.max_value is not None]
    min_value = max(min_values) if min_values else None
    max_value = min(max_values) if max_values else None
    if min_value is not None and max_value is not None and min_value > max_value:
        return None

    steps = [property_info.step for property_info in property_infos if property_info.step is not None]
    return ModifiableProperty(
        property_name=first.property_name,
        entity_id="",
        domain=first.domain if all(property_info.domain == first.domain for property_info in property_infos) else "mixed",
        value_type=value_type,
        description=first.description,
        min_value=min_value,
        max_value=max_value,
        step=max(steps) if steps else None,
        allowed_values=allowed_values,
    )


def _property_to_mapping(property_info: ModifiableProperty) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "property_name": property_info.property_name,
        "domain": property_info.domain,
        "value_type": property_info.value_type,
        "description": property_info.description,
    }
    if property_info.entity_id:
        mapping["entity_id"] = property_info.entity_id
    if property_info.allowed_values:
        mapping["allowed_values"] = list(property_info.allowed_values)
    if property_info.min_value is not None:
        mapping["min"] = property_info.min_value
    if property_info.max_value is not None:
        mapping["max"] = property_info.max_value
    if property_info.step is not None:
        mapping["step"] = property_info.step
    aliases = PROPERTY_VALUE_ALIASES.get(property_info.property_name)
    if aliases:
        mapping["value_aliases"] = {str(value): list(value_aliases) for value, value_aliases in aliases.items()}
    return mapping


def _area_to_mapping(area: HomeAssistantArea) -> dict[str, Any]:
    return {"area_id": area.area_id, "name": area.name, "aliases": list(area.aliases)}


def _device_to_mapping(device: HomeAssistantDevice, inventory: HomeAssistantInventory) -> dict[str, Any]:
    return {
        "device_id": device.device_id,
        "name": device.name,
        "type": device.device_type,
        "aliases": list(device.aliases),
        "area_id": device.area_id,
        "area_name": inventory.areas_by_id[device.area_id].name,
    }


def _build_area_lookup(areas_by_id: dict[str, HomeAssistantArea]) -> dict[str, tuple[str, ...]]:
    lookup: dict[str, list[str]] = {}
    for area in areas_by_id.values():
        for value in (area.area_id, area.name, *area.aliases):
            _add_lookup(lookup, value, area.area_id)
    return {key: tuple(values) for key, values in lookup.items()}


def _build_device_lookup(devices_by_id: dict[str, HomeAssistantDevice]) -> dict[str, tuple[str, ...]]:
    lookup: dict[str, list[str]] = {}
    for device in devices_by_id.values():
        for value in (device.device_id, device.name, *device.aliases):
            _add_lookup(lookup, value, device.device_id)
        for entity in device.entities:
            for value in (entity.entity_id, entity.name, *entity.aliases):
                _add_lookup(lookup, value, device.device_id)
    return {key: tuple(values) for key, values in lookup.items()}


def _add_lookup(lookup: dict[str, list[str]], value: str, identifier: str) -> None:
    key = _normalize_lookup(value)
    if not key:
        return
    lookup.setdefault(key, [])
    if identifier not in lookup[key]:
        lookup[key].append(identifier)


def _normalize_lookup(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().strip())


def _websocket_url(url: str) -> str:
    if url.startswith("https://"):
        return f"wss://{url.removeprefix('https://')}{HOME_ASSISTANT_WEBSOCKET_PATH}"
    if url.startswith("http://"):
        return f"ws://{url.removeprefix('http://')}{HOME_ASSISTANT_WEBSOCKET_PATH}"
    raise ValueError("Home Assistant url must start with http:// or https://")


def _entity_name(detail: dict[str, Any], state: dict[str, Any], entity_id: str) -> str:
    attributes = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    return _first_string(detail.get("name"), detail.get("original_name"), attributes.get("friendly_name"), entity_id)


def _device_name(raw_device: dict[str, Any], fallback_entity: HomeAssistantEntity) -> str:
    return _first_string(raw_device.get("name_by_user"), raw_device.get("name"), fallback_entity.name)


def _first_string(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    raise ValueError("expected at least one non-empty string")


def _clean_aliases(raw_aliases: Any) -> tuple[str, ...]:
    if not isinstance(raw_aliases, list):
        return ()
    return tuple(alias for alias in raw_aliases if isinstance(alias, str) and alias)


def _merge_aliases(alias_groups) -> tuple[str, ...]:
    merged = []
    for aliases in alias_groups:
        for alias in aliases:
            if alias not in merged:
                merged.append(alias)
    return tuple(merged)


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _light_supports_brightness(entity: HomeAssistantEntity) -> bool:
    color_modes = entity.attributes.get("supported_color_modes")
    if not isinstance(color_modes, list):
        return "brightness" in entity.attributes
    return any(mode in {"brightness", "color_temp", "hs", "rgb", "rgbw", "rgbww", "xy"} for mode in color_modes)


def _parse_number(value: JsonScalar, property_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{property_name} must be a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{property_name} must be a number") from exc
    raise ValueError(f"{property_name} must be a number")
