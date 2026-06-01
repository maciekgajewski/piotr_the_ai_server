from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypeAlias


DEFAULT_CONTROLLABLE_DOMAINS = ("climate", "light", "switch", "fan", "cover")
DEFAULT_INVENTORY_REFRESH_SECONDS = 30.0
DEFAULT_INVENTORY_SUMMARY_SECONDS = 300.0
HOME_ASSISTANT_WEBSOCKET_PATH = "/api/websocket"

JsonScalar: TypeAlias = str | int | float | bool

PROPERTY_VALUE_ALIASES: dict[str, dict[JsonScalar, tuple[str, ...]]] = {
    "hvac_mode": {
        "fan_only": ("wentylacja", "tryb wentylacji", "nawiew", "wiatrak", "fan", "ventilate", "ventilation"),
        "cool": ("chłodzenie", "klimatyzacja", "klima", "zimno"),
        "heat": ("grzanie", "ogrzewanie", "ciepło"),
        "off": ("wyłącz", "wyłączone", "off"),
    },
    "on": {
        True: ("włącz", "włączone", "on", "tak"),
        False: ("wyłącz", "wyłączone", "off", "nie"),
    },
}

DEVICE_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "climate": ("klima", "klimatyzacja", "klimatyzator", "air conditioner", "ac"),
    "light": ("światło", "światła", "oświetlenie"),
    "switch": ("przełącznik", "gniazdko", "kontakt", "switch"),
    "fan": ("wentylator", "wiatrak", "fan"),
    "cover": ("roleta", "rolety", "zasłona", "żaluzja"),
}


@dataclass(frozen=True)
class HomeAssistantOptions:
    url: str
    token: str
    controllable_domains: tuple[str, ...]
    inventory_refresh_seconds: float
    inventory_summary_seconds: float
    music_assistant_config_entry_id: str | None = None


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
class HomeAssistantEntity:
    entity_id: str
    device_id: str
    area_id: str
    domain: str
    name: str
    aliases: tuple[str, ...]
    state: str
    attributes: dict[str, Any]
    platform: str = ""
    config_entry_id: str = ""


@dataclass(frozen=True)
class HomeAssistantDevice:
    device_id: str
    area_id: str
    name: str
    aliases: tuple[str, ...]
    device_type: str
    entities: tuple[HomeAssistantEntity, ...]
    properties: tuple[ModifiableProperty, ...]

    @property
    def property_by_name(self) -> dict[str, ModifiableProperty]:
        return {property_info.property_name: property_info for property_info in self.properties}


@dataclass(frozen=True)
class HomeAssistantInventory:
    areas_by_id: dict[str, HomeAssistantArea]
    devices_by_id: dict[str, HomeAssistantDevice]
    devices_by_area: dict[str, tuple[HomeAssistantDevice, ...]]
    area_lookup: dict[str, tuple[str, ...]]
    device_lookup: dict[str, tuple[str, ...]]
    media_players_by_entity_id: dict[str, "HomeAssistantMediaPlayer"] = field(default_factory=dict)
    media_players_by_area: dict[str, tuple["HomeAssistantMediaPlayer", ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class HomeAssistantMediaPlayer:
    entity_id: str
    device_id: str
    area_id: str
    area_name: str
    name: str
    aliases: tuple[str, ...]
    state: str
    attributes: dict[str, Any]
    volume_level: float | None
    is_music_assistant: bool
    is_speaker: bool
    platform: str = ""
    config_entry_id: str = ""


@dataclass(frozen=True)
class HomeAssistantServiceCall:
    domain: str
    service: str
    entity_id: str | list[str] | None
    service_data: dict[str, Any]
    return_response: bool = False
