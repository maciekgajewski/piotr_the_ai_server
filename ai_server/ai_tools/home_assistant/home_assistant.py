from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Annotated, Any, TypeAlias

from aiohttp import ClientSession, WSMsgType

from ai_server.agent_loop import AgentCallableSet, AgentLoop, AgentLoopConfig
from ai_server.ai_tools.interfaces import BaseTool
from ai_server.config import AgentConfig
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import TextMessage


HOME_ASSISTANT_FAILURE_REPLY = "Przepraszam, nie udało mi się połączyć z Home Assistant."
DEFAULT_CONTROLLABLE_DOMAINS = ("climate", "light", "switch", "fan", "cover")
DEFAULT_INVENTORY_REFRESH_SECONDS = 30.0
DEFAULT_INVENTORY_SUMMARY_SECONDS = 300.0
HOME_ASSISTANT_WEBSOCKET_PATH = "/api/websocket"

JsonScalar: TypeAlias = str | int | float | bool

PROPERTY_VALUE_ALIASES: dict[str, dict[JsonScalar, tuple[str, ...]]] = {
    "hvac_mode": {
        "fan_only": ("wentylacja", "tryb wentylacji", "nawiew", "wiatrak", "fan"),
        "cool": ("chłodzenie", "klimatyzacja", "klima", "zimno"),
        "heat": ("grzanie", "ogrzewanie", "ciepło"),
        "off": ("wyłącz", "wyłączone", "off"),
    },
    "on": {
        True: ("włącz", "włączone", "on", "tak"),
        False: ("wyłącz", "wyłączone", "off", "nie"),
    },
}


class HomeAssistantTool(BaseTool, AgentCallableSet):
    name = "home_assistant"
    description = (
        "A tool for controlling smart home devices. Use this for any queries related to smart home control, "
        "air conditioning, lighting, etc."
    )

    def __init__(self, config: AgentConfig) -> None:
        super().__init__(config)
        self._home_assistant = _parse_home_assistant_options(self._config.options)
        self._inventory: HomeAssistantInventory | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._last_inventory_summary_at: float | None = None
        self._logger.debug(
            "configured HomeAssistantTool url=%s controllable_domains=%s inventory_refresh_seconds=%s inventory_summary_seconds=%s agent_loop_model=%s ollama_url=%s",
            self._home_assistant.url,
            self._home_assistant.controllable_domains,
            self._home_assistant.inventory_refresh_seconds,
            self._home_assistant.inventory_summary_seconds,
            self._home_assistant.agent_loop_model,
            self._home_assistant.ollama_url,
        )
        self._start_background_refresh()

    async def run(self, conversation: Conversation, endpoint: ConversationEndpoint, request: TextMessage) -> None:
        self._start_background_refresh()
        system_prompt = _build_system_prompt(self._inventory, conversation)
        self._logger.debug(
            "starting Home Assistant conversation conversation_id=%s user=%s location=%s initial_request=%r inventory_ready=%s system_prompt=%r",
            conversation.conversation_id,
            conversation.user,
            conversation.location,
            request.text,
            self._inventory is not None,
            system_prompt,
        )
        loop_config = AgentLoopConfig(
            model=self._home_assistant.agent_loop_model,
            ollama_url=self._home_assistant.ollama_url,
        )

        async with AgentLoop(config=loop_config, system_prompt=system_prompt, tools=self) as loop:
            reply = await loop.send_user_message(request.text)
            self._logger.debug(
                "sending Home Assistant reply conversation_id=%s reply=%r end_conversation=%s loop_eval_count=%s",
                conversation.conversation_id,
                reply.reply_text,
                reply.end_conversation,
                loop.eval_count,
            )
            await endpoint.send_message(TextMessage(text=reply.reply_text))
            if reply.end_conversation:
                self._logger.debug("ending Home Assistant conversation after initial reply conversation_id=%s", conversation.conversation_id)
                return

            async for follow_up in endpoint.messages():
                self._logger.debug(
                    "received Home Assistant follow-up conversation_id=%s message=%r",
                    conversation.conversation_id,
                    follow_up.text,
                )
                reply = await loop.send_user_message(follow_up.text)
                self._logger.debug(
                    "sending Home Assistant follow-up reply conversation_id=%s reply=%r end_conversation=%s loop_eval_count=%s",
                    conversation.conversation_id,
                    reply.reply_text,
                    reply.end_conversation,
                    loop.eval_count,
                )
                await endpoint.send_message(TextMessage(text=reply.reply_text))
                if reply.end_conversation:
                    self._logger.debug("ending Home Assistant conversation after follow-up conversation_id=%s", conversation.conversation_id)
                    return
        self._logger.debug("Home Assistant conversation input stream ended conversation_id=%s", conversation.conversation_id)

    async def close(self) -> None:
        if self._refresh_task is None:
            self._logger.debug("close requested; no Home Assistant inventory refresh task exists")
            return
        self._logger.debug("cancelling Home Assistant inventory refresh task")
        self._refresh_task.cancel()
        try:
            await self._refresh_task
        except asyncio.CancelledError:
            pass
        self._logger.debug("Home Assistant inventory refresh task stopped")
        self._refresh_task = None

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
        self._logger.debug("list_devices called area_name=%r inventory_ready=%s", area_name, self._inventory is not None)
        inventory = self._inventory
        if inventory is None:
            self._logger.debug("list_devices returning inventory-not-ready")
            return _inventory_not_ready()

        area = inventory.resolve_area(area_name)
        if isinstance(area, dict):
            self._logger.debug("list_devices area resolution failed area_name=%r result=%s", area_name, area)
            return area

        result = [
            {
                "device_id": device.device_id,
                "name": device.name,
                "type": device.device_type,
                "aliases": list(device.aliases),
                "area_id": device.area_id,
                "area_name": inventory.areas_by_id[device.area_id].name,
            }
            for device in inventory.devices_by_area.get(area.area_id, ())
        ]
        self._logger.debug("list_devices resolved area_id=%s area_name=%s devices=%s", area.area_id, area.name, result)
        return result

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
        self._logger.debug(
            "list_modifiable_properties called device=%r inventory_ready=%s",
            device,
            self._inventory is not None,
        )
        inventory = self._inventory
        if inventory is None:
            self._logger.debug("list_modifiable_properties returning inventory-not-ready")
            return _inventory_not_ready()

        resolved_device = inventory.resolve_device(device)
        if isinstance(resolved_device, dict):
            self._logger.debug("list_modifiable_properties device resolution failed device=%r result=%s", device, resolved_device)
            return resolved_device

        result = [_property_to_mapping(property_info) for property_info in resolved_device.properties]
        self._logger.debug(
            "list_modifiable_properties resolved device_id=%s device_name=%s properties=%s",
            resolved_device.device_id,
            resolved_device.name,
            result,
        )
        return result

    @AgentCallableSet.tool(description="Modify one Home Assistant device property. This is the only tool that changes devices.")
    async def modify_device(
        self,
        device: Annotated[str, "Device id, entity id, device name, entity name, or any entity alias."],
        property_name: Annotated[str, "Property name returned by list_modifiable_properties."],
        value: Annotated[JsonScalar, "Desired property value. Common aliases are accepted."],
    ) -> dict[str, Any]:
        self._logger.debug(
            "modify_device called device=%r property_name=%r value=%r inventory_ready=%s",
            device,
            property_name,
            value,
            self._inventory is not None,
        )
        inventory = self._inventory
        if inventory is None:
            self._logger.debug("modify_device returning inventory-not-ready")
            return _inventory_not_ready()

        resolved_device = inventory.resolve_device(device)
        if isinstance(resolved_device, dict):
            self._logger.debug("modify_device device resolution failed device=%r result=%s", device, resolved_device)
            return resolved_device

        property_info = resolved_device.property_by_name.get(property_name)
        if property_info is None:
            result = {
                "error": "unknown_property",
                "device_id": resolved_device.device_id,
                "property_name": property_name,
                "known_properties": sorted(resolved_device.property_by_name),
            }
            self._logger.debug("modify_device property resolution failed result=%s", result)
            return result

        try:
            service_call = _build_service_call(property_info, value)
        except ValueError as exc:
            result = {
                "error": "invalid_property_value",
                "device_id": resolved_device.device_id,
                "property_name": property_name,
                "message": str(exc),
            }
            self._logger.debug("modify_device value validation failed result=%s", result)
            return result

        self._logger.debug(
            "modify_device calling Home Assistant service device_id=%s property=%s service=%s.%s entity_id=%s service_data=%s",
            resolved_device.device_id,
            property_info.property_name,
            service_call.domain,
            service_call.service,
            property_info.entity_id,
            service_call.service_data,
        )
        await _call_home_assistant_service(self._home_assistant, service_call, self._logger)
        result = {
            "status": "ok",
            "service": f"{service_call.domain}.{service_call.service}",
            "entity_id": property_info.entity_id,
        }
        self._logger.debug("modify_device completed result=%s", result)
        return result

    def _start_background_refresh(self) -> None:
        if self._refresh_task is not None and not self._refresh_task.done():
            self._logger.debug("Home Assistant inventory refresh task already running")
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._logger.debug("not starting Home Assistant inventory refresh task; no running event loop")
            return
        self._logger.debug("starting Home Assistant inventory refresh task interval_seconds=%s", self._home_assistant.inventory_refresh_seconds)
        self._refresh_task = asyncio.create_task(self._refresh_inventory_loop())

    async def _refresh_inventory_loop(self) -> None:
        while True:
            try:
                self._inventory = await _fetch_inventory(self._home_assistant, self._logger)
                self._log_inventory_summary_if_due(self._inventory)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("failed to refresh Home Assistant inventory")
            await asyncio.sleep(self._home_assistant.inventory_refresh_seconds)

    def _log_inventory_summary_if_due(self, inventory: "HomeAssistantInventory") -> None:
        now = asyncio.get_running_loop().time()
        if self._last_inventory_summary_at is None:
            self._logger.debug(
                "refreshed Home Assistant inventory initial_summary=%s",
                _inventory_debug_summary(inventory),
            )
            self._last_inventory_summary_at = now
            return
        if now - self._last_inventory_summary_at >= self._home_assistant.inventory_summary_seconds:
            self._logger.debug(
                "refreshed Home Assistant inventory heartbeat areas=%s devices=%s",
                len(inventory.areas_by_id),
                len(inventory.devices_by_id),
            )
            self._last_inventory_summary_at = now


@dataclass(frozen=True)
class HomeAssistantOptions:
    url: str
    token: str
    controllable_domains: tuple[str, ...]
    inventory_refresh_seconds: float
    inventory_summary_seconds: float
    agent_loop_model: str
    ollama_url: str


@dataclass(frozen=True)
class HomeAssistantArea:
    area_id: str
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ModifiableProperty:
    property_name: str
    entity_id: str
    domain: str
    value_type: str
    description: str
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    allowed_values: tuple[JsonScalar, ...] = ()


@dataclass(frozen=True)
class HomeAssistantDevice:
    device_id: str
    area_id: str
    name: str
    aliases: tuple[str, ...]
    device_type: str
    entities: tuple["HomeAssistantEntity", ...]
    properties: tuple[ModifiableProperty, ...]

    @property
    def property_by_name(self) -> dict[str, ModifiableProperty]:
        return {property_info.property_name: property_info for property_info in self.properties}


@dataclass(frozen=True)
class HomeAssistantEntity:
    entity_id: str
    device_id: str
    area_id: str
    domain: str
    name: str
    aliases: tuple[str, ...]
    state: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class HomeAssistantInventory:
    areas_by_id: dict[str, HomeAssistantArea]
    devices_by_id: dict[str, HomeAssistantDevice]
    devices_by_area: dict[str, tuple[HomeAssistantDevice, ...]]
    area_lookup: dict[str, tuple[str, ...]]
    device_lookup: dict[str, tuple[str, ...]]

    def resolve_area(self, area_name: str) -> HomeAssistantArea | dict[str, Any]:
        matches = self.area_lookup.get(_normalize_lookup(area_name), ())
        if len(matches) == 1:
            return self.areas_by_id[matches[0]]
        if not matches:
            return {
                "error": "unknown_area",
                "area": area_name,
                "known_areas": [_area_to_mapping(area) for area in self.areas_by_id.values()],
            }
        return {
            "error": "ambiguous_area",
            "area": area_name,
            "candidates": [_area_to_mapping(self.areas_by_id[area_id]) for area_id in matches],
        }

    def resolve_device(self, device: str) -> HomeAssistantDevice | dict[str, Any]:
        matches = self.device_lookup.get(_normalize_lookup(device), ())
        if len(matches) == 1:
            return self.devices_by_id[matches[0]]
        if not matches:
            return {
                "error": "unknown_device",
                "device": device,
                "known_devices": [_device_to_mapping(device_info, self) for device_info in self.devices_by_id.values()],
            }
        return {
            "error": "ambiguous_device",
            "device": device,
            "candidates": [_device_to_mapping(self.devices_by_id[device_id], self) for device_id in matches],
        }


@dataclass(frozen=True)
class HomeAssistantServiceCall:
    domain: str
    service: str
    entity_id: str
    service_data: dict[str, Any]


def _parse_home_assistant_options(options: dict[str, Any]) -> HomeAssistantOptions:
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

    agent_loop_model = options.get("model")
    if not isinstance(agent_loop_model, str) or not agent_loop_model:
        raise ValueError("agent.model must be a non-empty string for HomeAssistantTool")

    ollama_url = options.get("ollama_url")
    if not isinstance(ollama_url, str) or not ollama_url:
        raise ValueError("agent.ollama_url must be a non-empty string for HomeAssistantTool")

    return HomeAssistantOptions(
        url=url.rstrip("/"),
        token=token,
        controllable_domains=tuple(dict.fromkeys(domains)),
        inventory_refresh_seconds=float(inventory_refresh_seconds),
        inventory_summary_seconds=float(inventory_summary_seconds),
        agent_loop_model=agent_loop_model,
        ollama_url=ollama_url,
    )


async def _fetch_inventory(options: HomeAssistantOptions, logger: logging.Logger) -> HomeAssistantInventory:
    async with _HomeAssistantWebSocket(options, logger, log_traffic=False) as client:
        areas = await client.command({"type": "config/area_registry/list"})
        devices = await client.command({"type": "config/device_registry/list"})
        entity_registry = await client.command({"type": "config/entity_registry/list"})
        states = await client.command({"type": "get_states"})

        entity_details = []
        for entity in entity_registry:
            entity_id = entity.get("entity_id")
            if not isinstance(entity_id, str):
                continue
            if entity_id.split(".", 1)[0] not in options.controllable_domains:
                continue
            if entity.get("disabled_by") is not None or entity.get("hidden_by") is not None:
                continue
            entity_details.append(await client.command({"type": "config/entity_registry/get", "entity_id": entity_id}))

    inventory = _build_inventory(
        raw_areas=areas,
        raw_devices=devices,
        raw_entity_details=entity_details,
        raw_states=states,
        controllable_domains=options.controllable_domains,
    )
    return inventory


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
            if message.type != WSMsgType.TEXT:
                raise ValueError(f"unexpected Home Assistant WebSocket message type: {message.type}")
            body = json.loads(message.data)
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


def _summarize_ws_result(result: Any) -> Any:
    if isinstance(result, list):
        return {
            "type": "list",
            "count": len(result),
            "sample": result[:2],
        }
    if isinstance(result, dict):
        return {
            "type": "object",
            "keys": sorted(result)[:20],
            "value": result if len(result) <= 10 else None,
        }
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
        return HomeAssistantServiceCall(
            domain="climate",
            service="set_temperature",
            entity_id=property_info.entity_id,
            service_data={"temperature": normalized_value},
        )
    if property_info.property_name == "hvac_mode":
        return HomeAssistantServiceCall(
            domain="climate",
            service="set_hvac_mode",
            entity_id=property_info.entity_id,
            service_data={"hvac_mode": normalized_value},
        )
    if property_info.property_name == "fan_mode":
        return HomeAssistantServiceCall(
            domain="climate",
            service="set_fan_mode",
            entity_id=property_info.entity_id,
            service_data={"fan_mode": normalized_value},
        )
    if property_info.property_name == "brightness_percent":
        return HomeAssistantServiceCall(
            domain="light",
            service="turn_on",
            entity_id=property_info.entity_id,
            service_data={"brightness_pct": normalized_value},
        )
    if property_info.property_name == "percentage":
        return HomeAssistantServiceCall(
            domain="fan",
            service="set_percentage",
            entity_id=property_info.entity_id,
            service_data={"percentage": normalized_value},
        )
    if property_info.property_name == "position":
        return HomeAssistantServiceCall(
            domain="cover",
            service="set_cover_position",
            entity_id=property_info.entity_id,
            service_data={"position": normalized_value},
        )
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


def _build_system_prompt(inventory: HomeAssistantInventory | None, conversation: Conversation) -> str:
    location = conversation.location or "unknown"
    user = conversation.user or "unknown"
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
            "If the user does not name a room, prefer the current location when it is known.",
            "Critical action rules:",
            "- list_devices and list_modifiable_properties only inspect Home Assistant; they never change devices.",
            "- For user requests to turn on, turn off, set, change, open, close, dim, brighten, heat, cool, or adjust a device, you must call modify_device.",
            "- Never claim that a device was changed unless modify_device returned status=ok for that exact requested change.",
            "- If you inspected devices but did not call modify_device, say that you found the device but have not changed it yet.",
            "- If the current room has exactly one device matching the requested type or alias, that device is specific enough; call modify_device without asking for confirmation.",
            "- Do not ask for confirmation for ordinary reversible smart-home changes unless multiple matching devices or values remain ambiguous.",
            "- If you cannot choose the device, property, or value after inspecting available devices/properties, ask a clarification question instead of claiming success.",
            "- For requests about klimatyzacja, klima, air conditioning, or AC, prefer devices with type=climate.",
            "- To turn climate or air conditioning off, prefer modify_device with property_name=hvac_mode and value=off when hvac_mode is available.",
            "- After list_modifiable_properties returns the needed property for an action request, your next assistant response must be a modify_device tool call, not text.",
            '- Example: user says "wyłącz klimatyzację" in Office, list_devices shows one climate device "Study air conditioner", and list_modifiable_properties shows hvac_mode with off; then call modify_device(device="Study air conditioner", property_name="hvac_mode", value="off").',
            f"Current user: {user}",
            f"Current location: {location}",
            "Known rooms and aliases:",
            *area_lines,
            "Value vocabulary:",
            _format_value_vocabulary(),
        ]
    )


def _format_value_vocabulary() -> str:
    lines = []
    for property_name, value_aliases in PROPERTY_VALUE_ALIASES.items():
        for canonical_value, aliases in value_aliases.items():
            lines.append(f"- {property_name}: {canonical_value} aliases: {', '.join(aliases)}")
    return "\n".join(lines)


def _inventory_not_ready() -> dict[str, str]:
    return {"error": "home_assistant_inventory_not_ready"}


def _property_to_mapping(property_info: ModifiableProperty) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "property_name": property_info.property_name,
        "entity_id": property_info.entity_id,
        "domain": property_info.domain,
        "value_type": property_info.value_type,
        "description": property_info.description,
    }
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
    return _first_string(
        detail.get("name"),
        detail.get("original_name"),
        attributes.get("friendly_name"),
        entity_id,
    )


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
