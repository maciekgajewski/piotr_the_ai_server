from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ai_server.agent_loop import AgentLoop, AgentLoopConfig
from ai_server.ai_tools.home_assistant.home_assistant import HomeAssistantTool
from ai_server.config import AgentConfig
from ai_server.home_assistant import (
    DEVICE_TYPE_ALIASES,
    PROPERTY_VALUE_ALIASES,
    HomeAssistantArea,
    HomeAssistantDevice,
    HomeAssistantEntity,
    HomeAssistantInventory,
    HomeAssistantMediaPlayer,
    JsonScalar,
    ModifiableProperty,
)


DEFAULT_SCENARIOS = Path("tools/lib/agent-tool-eval/home_assistant.yaml")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"
GLOBAL_SCOPE_TERMS = (
    "all",
    "every",
    "whole house",
    "everywhere",
    "wszystkie",
    "kazde",
    "każde",
    "kazdy",
    "każdy",
    "wszedzie",
    "wszędzie",
    "caly dom",
    "cały dom",
    "calym domu",
    "całym domu",
    "w calym domu",
    "w całym domu",
)


@dataclass(frozen=True)
class ExpectedCall:
    tool: str
    arguments: dict[str, Any]
    result: Any = None
    optional: bool = False


@dataclass(frozen=True)
class ExpectedEffect:
    device: str
    property_name: str
    value: Any


@dataclass(frozen=True)
class ReplyExpectation:
    contains_all: tuple[str, ...] = ()
    one_sentence: bool = True


@dataclass(frozen=True)
class Scenario:
    name: str
    messages: tuple[str, ...]
    expected_calls: tuple[ExpectedCall, ...]
    expected_effects: tuple[ExpectedEffect, ...] = ()
    reply_expectations: tuple[ReplyExpectation, ...] = ()
    area: str | None = None
    user: str | None = None
    strict: bool = False


@dataclass(frozen=True)
class ToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    result: Any = None


@dataclass
class ScenarioResult:
    scenario: Scenario
    replies: list[str] = field(default_factory=list)
    actual_calls: list[ToolCallRecord] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    eval_count: int = 0
    duration_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.failures


class FakeHomeAssistantConnection:
    def __init__(
        self,
        inventory: HomeAssistantInventory,
        expected_calls: tuple[ExpectedCall, ...],
        *,
        transcript: bool = False,
    ) -> None:
        self._inventory = inventory
        self._expected_calls = expected_calls
        self._transcript = transcript
        self._used_expected_replies: set[int] = set()
        self.calls: list[ToolCallRecord] = []
        self._logger = logging.getLogger(f"{__name__}.FakeHomeAssistantConnection")

    @property
    def inventory(self) -> HomeAssistantInventory:
        return self._inventory

    def system_prompt_context(self, *, user: str | None, area: str | None) -> str:
        area_lines = []
        for area in sorted(self._inventory.areas_by_id.values(), key=lambda item: item.name.casefold()):
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
                'Example: user says "wyłącz klimatyzację" in Office, list_devices shows one climate device "Study air conditioner", and list_modifiable_properties shows hvac_mode with off; then call modify_device(device="Study air conditioner", property_name="hvac_mode", value="off").',
                'Example: user says "wyłącz wszystkie klimatyzatory"; call find_devices(query="klimatyzator klima klimatyzacja", device_type="climate"), then list_common_modifiable_properties, then modify_devices for every matching device.',
                "Scope rules:",
                '- If the user names an area or room, restrict the action to that area.',
                '- If the user does not name an area, prefer the current area only for singular or local requests.',
                '- If the user says "all", "every", "wszystkie", "każde", "wszędzie", "w całym domu", or similar global wording, do not restrict to the current area. Search all areas.',
                "- If the user does not use global wording, never modify devices outside the named area or current area.",
                "- For requests about all devices of a type, find every matching device before modifying any of them.",
                "- For multiple matching devices, modify every matching device unless the request is ambiguous or unsafe.",
                '- Do not report success for "all" unless every matching device was attempted. Still reply in one sentence.',
                f"Current user: {user or 'unknown'}",
                f"Current area: {area or 'unknown'}",
                "Known areas and aliases:",
                *area_lines,
                "Device type vocabulary:",
                _format_device_type_vocabulary(),
                "Value vocabulary:",
                _format_value_vocabulary(),
            ]
        )

    async def close(self) -> None:
        return None

    async def list_devices(self, area_name: str) -> list[dict[str, Any]] | dict[str, Any]:
        return await self._record_and_reply("list_devices", {"area_name": area_name}, self._default_list_devices(area_name))

    async def find_devices(
        self,
        query: str = "",
        device_type: str = "",
        area_name: str = "",
    ) -> list[dict[str, Any]] | dict[str, Any]:
        default = self._default_find_devices(query=query, device_type=device_type, area_name=area_name)
        return await self._record_and_reply(
            "find_devices",
            {"query": query, "device_type": device_type, "area_name": area_name},
            default,
        )

    async def list_modifiable_properties(self, device: str) -> list[dict[str, Any]] | dict[str, Any]:
        return await self._record_and_reply(
            "list_modifiable_properties",
            {"device": device},
            self._default_list_modifiable_properties(device),
        )

    async def list_common_modifiable_properties(self, devices: list[str]) -> dict[str, Any]:
        return await self._record_and_reply(
            "list_common_modifiable_properties",
            {"devices": devices},
            self._default_list_common_modifiable_properties(devices),
        )

    async def modify_device(self, device: str, property_name: str, value: JsonScalar) -> dict[str, Any]:
        return await self._record_and_reply(
            "modify_device",
            {"device": device, "property_name": property_name, "value": value},
            self._default_modify_device(device, property_name, value),
        )

    async def modify_devices(
        self,
        devices: list[str],
        property_name: str,
        value: JsonScalar,
        *,
        user_message: str = "",
        current_area: str | None = None,
    ) -> dict[str, Any]:
        return await self._record_and_reply(
            "modify_devices",
            {"devices": devices, "property_name": property_name, "value": value},
            self._default_modify_devices(devices, property_name, value, user_message=user_message, current_area=current_area),
        )

    async def list_media_players(
        self,
        *,
        area_name: str = "",
        music_assistant_only: bool = True,
        speakers_only: bool = True,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        return await self._record_and_reply(
            "list_media_players",
            {
                "area_name": area_name,
                "music_assistant_only": music_assistant_only,
                "speakers_only": speakers_only,
            },
            self._default_list_media_players(
                area_name=area_name,
                music_assistant_only=music_assistant_only,
                speakers_only=speakers_only,
            ),
        )

    async def media_player_stop(self, entity_ids: list[str]) -> dict[str, Any]:
        return await self._record_and_reply(
            "media_player_stop",
            {"entity_ids": entity_ids},
            {"status": "ok", "entity_ids": entity_ids},
        )

    async def media_player_volume_set(self, entity_ids: list[str], volume_level: float) -> dict[str, Any]:
        return await self._record_and_reply(
            "media_player_volume_set",
            {"entity_ids": entity_ids, "volume_level": volume_level},
            {"status": "ok", "entity_ids": entity_ids, "volume_level": volume_level},
        )

    async def media_player_shuffle_set(self, entity_ids: list[str], shuffle: bool) -> dict[str, Any]:
        return await self._record_and_reply(
            "media_player_shuffle_set",
            {"entity_ids": entity_ids, "shuffle": shuffle},
            {"status": "ok", "entity_ids": entity_ids, "shuffle": shuffle},
        )

    async def media_player_join(self, entity_id: str, group_members: list[str]) -> dict[str, Any]:
        return await self._record_and_reply(
            "media_player_join",
            {"entity_id": entity_id, "group_members": group_members},
            {"status": "ok", "entity_id": entity_id, "group_members": group_members},
        )

    async def media_player_volume_delta(self, entity_ids: list[str], delta: float) -> dict[str, Any]:
        results = []
        for entity_id in entity_ids:
            player = self._inventory.media_players_by_entity_id.get(entity_id)
            current_volume = player.volume_level if player and player.volume_level is not None else 0.5
            results.append({"entity_id": entity_id, "volume_level": min(1.0, max(0.0, current_volume + delta))})
        return await self._record_and_reply(
            "media_player_volume_delta",
            {"entity_ids": entity_ids, "delta": delta},
            {"status": "ok", "results": results},
        )

    async def media_player_now_playing(self, entity_id: str) -> dict[str, Any]:
        player = self._inventory.media_players_by_entity_id.get(entity_id)
        return await self._record_and_reply(
            "media_player_now_playing",
            {"entity_id": entity_id},
            {
                "status": "ok",
                "entity_id": entity_id,
                "title": player.attributes.get("media_title") if player else "",
                "artist": player.attributes.get("media_artist") if player else "",
            },
        )

    async def music_assistant_search(
        self,
        *,
        name: str,
        media_type: str = "",
        limit: int = 5,
        library_only: bool = False,
    ) -> dict[str, Any]:
        return await self._record_and_reply(
            "music_assistant_search",
            {"name": name, "media_type": media_type, "limit": limit, "library_only": library_only},
            self._default_music_assistant_search(name=name, media_type=media_type),
        )

    async def music_assistant_play_media(
        self,
        entity_ids: list[str],
        *,
        media_id: str,
        media_type: str = "",
        artist: str = "",
        album: str = "",
    ) -> dict[str, Any]:
        return await self._record_and_reply(
            "music_assistant_play_media",
            {
                "entity_ids": entity_ids,
                "media_id": media_id,
                "media_type": media_type,
                "artist": artist,
                "album": album,
            },
            {"status": "ok", "entity_ids": entity_ids, "media_id": media_id, "media_type": media_type},
        )

    async def music_assistant_get_queue(self, entity_id: str) -> dict[str, Any]:
        return await self._record_and_reply(
            "music_assistant_get_queue",
            {"entity_id": entity_id},
            {
                "status": "ok",
                "response": {
                    entity_id: {
                        "current_item": {
                            "uri": "spotify:playlist:current-focus",
                            "name": "Current Focus",
                            "media_type": "playlist",
                        }
                    }
                },
            },
        )

    async def _record_and_reply(self, tool: str, arguments: dict[str, Any], default_result: Any) -> Any:
        result = default_result
        for index, expected in enumerate(self._expected_calls):
            if index in self._used_expected_replies:
                continue
            if expected.tool != tool:
                continue
            if not _arguments_match(expected.arguments, arguments, self._inventory):
                continue
            self._used_expected_replies.add(index)
            if expected.result is not None:
                result = expected.result
            break
        self.calls.append(ToolCallRecord(tool=tool, arguments=arguments, result=result))
        if self._transcript:
            _print_transcript_tool_call(tool, arguments, result)
        return result

    def _default_list_devices(self, area_name: str) -> list[dict[str, Any]] | dict[str, Any]:
        area_id = self._resolve_area_id(area_name)
        if area_id is None:
            return {"error": "unknown_area", "area_name": area_name}
        return [_device_to_mapping(device, self._inventory) for device in self._inventory.devices_by_area.get(area_id, ())]

    def _default_find_devices(self, *, query: str, device_type: str, area_name: str) -> list[dict[str, Any]] | dict[str, Any]:
        area_id = self._resolve_area_id(area_name) if area_name else None
        normalized_type = _normalize_device_type(device_type) if device_type else ""
        query_terms = set(_normalize_text(query).split()) - set(_device_type_alias_terms())
        matches = []
        for device in self._inventory.devices_by_id.values():
            if area_id and device.area_id != area_id:
                continue
            if normalized_type and device.device_type != normalized_type:
                continue
            values = " ".join(_device_search_values(device, self._inventory))
            normalized_values = _normalize_text(values)
            if query_terms and not all(term in normalized_values for term in query_terms):
                continue
            matches.append(_device_to_mapping(device, self._inventory))
        return matches

    def _default_list_modifiable_properties(self, device: str) -> list[dict[str, Any]] | dict[str, Any]:
        resolved = self._resolve_device(device)
        if resolved is None:
            return {"error": "unknown_device", "device": device}
        return [_property_to_mapping(property_info) for property_info in resolved.properties]

    def _default_list_common_modifiable_properties(self, devices: list[str]) -> dict[str, Any]:
        resolved_devices = []
        missing = []
        for device in devices:
            resolved = self._resolve_device(device)
            if resolved is None:
                missing.append(device)
            else:
                resolved_devices.append(resolved)
        if missing:
            return {"error": "unknown_device", "missing_devices": missing}
        if not resolved_devices:
            return {"devices": [], "properties": []}

        common_names = set(resolved_devices[0].property_by_name)
        for device in resolved_devices[1:]:
            common_names &= set(device.property_by_name)
        return {
            "devices": [_device_to_mapping(device, self._inventory) for device in resolved_devices],
            "properties": [
                _property_to_mapping(resolved_devices[0].property_by_name[property_name])
                for property_name in sorted(common_names)
            ],
        }

    def _default_modify_device(self, device: str, property_name: str, value: JsonScalar) -> dict[str, Any]:
        resolved = self._resolve_device(device)
        if resolved is None:
            return {"status": "failed", "error": "unknown_device", "device": device}
        property_info = resolved.property_by_name.get(property_name)
        if property_info is None:
            return {"status": "skipped", "error": "unsupported_property", "property_name": property_name}
        return {
            "status": "ok",
            "service": _service_for_property(property_info),
            "entity_id": property_info.entity_id,
        }

    def _default_modify_devices(
        self,
        devices: list[str],
        property_name: str,
        value: JsonScalar,
        *,
        user_message: str,
        current_area: str | None,
    ) -> dict[str, Any]:
        resolved_devices = [self._resolve_device(device) for device in devices]
        concrete_devices = [device for device in resolved_devices if device is not None]
        scope_rejection = _batch_scope_rejection(
            concrete_devices,
            self._inventory,
            user_message=user_message,
            current_area=current_area,
        )
        if scope_rejection is not None:
            return scope_rejection

        results = []
        for device in devices:
            results.append(
                {
                    "device": device,
                    "result": self._default_modify_device(device, property_name, value),
                }
            )
        return {"status": "ok", "results": results}

    def _default_list_media_players(
        self,
        *,
        area_name: str,
        music_assistant_only: bool,
        speakers_only: bool,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        area_id = self._resolve_area_id(area_name) if area_name else None
        if area_name and area_id is None:
            return {"error": "unknown_area", "area_name": area_name}
        players = self._inventory.media_players_by_area.get(area_id, ()) if area_id else self._inventory.media_players_by_entity_id.values()
        return [
            _media_player_to_mapping(player)
            for player in players
            if (not music_assistant_only or player.is_music_assistant)
            and (not speakers_only or player.is_speaker)
        ]

    def _default_music_assistant_search(self, *, name: str, media_type: str) -> dict[str, Any]:
        normalized = _normalize_text(name)
        if "tok" in normalized:
            return {
                "status": "ok",
                "response": {"radio": [{"uri": "tunein://station/tok-fm", "name": "TOK FM", "media_type": "radio"}]},
            }
        return {
            "status": "ok",
            "response": {"items": [{"uri": f"spotify:search:{normalized.replace(' ', '-')}", "name": name, "media_type": media_type or "playlist"}]},
        }

    def _resolve_area_id(self, area_name: str) -> str | None:
        normalized = _normalize_text(area_name)
        for lookup_value, area_ids in self._inventory.area_lookup.items():
            if _normalize_text(lookup_value) == normalized:
                return area_ids[0]
        return None

    def _resolve_device(self, device_name: str) -> HomeAssistantDevice | None:
        normalized = _normalize_text(device_name)
        for lookup_value, device_ids in self._inventory.device_lookup.items():
            if _normalize_text(lookup_value) == normalized:
                return self._inventory.devices_by_id[device_ids[0]]
        return None


def main() -> int:
    args = _parse_args()
    _configure_logging(args.verbose)
    try:
        config = _load_eval_config(args.scenarios)
        domain = args.domain or config["domain"]
        if args.list:
            for scenario in _load_scenarios(config):
                print(scenario.name)
            return 0
        if domain != "home_assistant":
            raise ValueError(f"unsupported domain: {domain}")
        results = asyncio.run(_run_home_assistant_eval(config, args))
    except Exception as exc:
        print(f"agent-tool-eval failed: {exc}", file=sys.stderr)
        return 2

    _print_results(results, verbose=args.verbose)
    return 0 if all(result.passed for result in results) else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate how an Ollama model translates requests into agent tool calls.")
    parser.add_argument("--domain", help="Domain adapter to run. Currently supported: home_assistant.")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS, help="YAML scenario file.")
    parser.add_argument("--scenario", action="append", default=[], help="Run only scenarios with this exact name. May be repeated.")
    parser.add_argument("--model", help=f"Ollama model name. Default from YAML or {DEFAULT_MODEL}.")
    parser.add_argument("--ollama-url", help=f"Ollama URL. Default from YAML or {DEFAULT_OLLAMA_URL}.")
    parser.add_argument("--think", choices=("true", "false", "none"), help="Override Ollama think setting for /api/chat.")
    parser.add_argument("--timeout", type=float, help="Ollama request timeout in seconds.")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit.")
    parser.add_argument("--no-transcript", action="store_true", help="Do not print the live model/tool transcript.")
    parser.add_argument("--verbose", action="store_true", help="Print full replies, calls, and mismatch details.")
    return parser.parse_args()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.ERROR,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _load_eval_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    if not isinstance(config, dict):
        raise ValueError("scenario file must contain a YAML mapping")
    if not isinstance(config.get("domain"), str):
        raise ValueError("scenario file must contain domain")
    return config


async def _run_home_assistant_eval(config: dict[str, Any], args: argparse.Namespace) -> list[ScenarioResult]:
    inventory = _build_inventory(config.get("home_assistant", {}))
    scenarios = _load_scenarios(config)
    if args.scenario:
        selected = set(args.scenario)
        scenarios = [scenario for scenario in scenarios if scenario.name in selected]
        missing = selected - {scenario.name for scenario in scenarios}
        if missing:
            raise ValueError(f"unknown scenario(s): {', '.join(sorted(missing))}")

    results = []
    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be a mapping")

    for scenario in scenarios:
        transcript = not args.no_transcript
        fake_connection = FakeHomeAssistantConnection(inventory, scenario.expected_calls, transcript=transcript)
        tool_config = AgentConfig(
            type="home_assistant",
            options={
                "model": args.model or defaults.get("model", DEFAULT_MODEL),
                "ollama_url": args.ollama_url or defaults.get("ollama_url", DEFAULT_OLLAMA_URL),
                "home_assistant": {
                    "url": "http://fake-home-assistant.local:8123",
                    "token": "fake",
                },
            },
        )
        tool = HomeAssistantTool(tool_config, connection=fake_connection)
        loop_config = AgentLoopConfig(
            model=args.model or defaults.get("model", DEFAULT_MODEL),
            ollama_url=args.ollama_url or defaults.get("ollama_url", DEFAULT_OLLAMA_URL),
            options=_dict_or_empty(defaults.get("options")),
            think=_parse_think(args.think, defaults.get("think", False)),
            request_timeout_seconds=args.timeout or _float_or_default(defaults.get("request_timeout_seconds"), 60.0),
            max_tool_calls_per_message=_int_or_default(defaults.get("max_tool_calls_per_message"), 8),
            max_tool_repair_attempts=_int_or_default(defaults.get("max_tool_repair_attempts"), 2),
        )

        area = scenario.area or _str_or_none(defaults.get("area"))
        user = scenario.user or _str_or_none(defaults.get("user"))
        system_prompt = fake_connection.system_prompt_context(user=user, area=area)
        result = ScenarioResult(scenario=scenario)
        if transcript:
            _print_transcript_scenario_start(scenario, loop_config.model, loop_config.ollama_url, user, area)
        started_at = time.perf_counter()
        context_message_observer = _print_transcript_context_message if transcript and _think_enabled(loop_config.think) else None
        async with AgentLoop(
            loop_config,
            system_prompt=system_prompt,
            tools=tool,
            context_message_observer=context_message_observer,
        ) as loop:
            for message in scenario.messages:
                if transcript:
                    _print_transcript_user_message(message)
                tool.set_request_context(user_message=message, area=area)
                reply = await loop.send_user_message(message)
                result.replies.append(reply.reply_text)
                if transcript:
                    _print_transcript_assistant_reply(reply.reply_text, reply.end_conversation)
                if reply.end_conversation:
                    result.failures.append("agent loop ended conversation because of an unrecoverable error")
                    break
            result.eval_count = loop.eval_count
        result.duration_seconds = time.perf_counter() - started_at

        result.actual_calls.extend(fake_connection.calls)
        _score_scenario(result, inventory)
        if transcript:
            _print_transcript_scenario_end(result)
        results.append(result)
    return results


def _load_scenarios(config: dict[str, Any]) -> list[Scenario]:
    raw_scenarios = config.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise ValueError("scenarios must be a list")
    scenarios = []
    for index, raw_scenario in enumerate(raw_scenarios):
        if not isinstance(raw_scenario, dict):
            raise ValueError(f"scenario #{index + 1} must be a mapping")
        name = raw_scenario.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"scenario #{index + 1} must have a non-empty name")
        messages = _parse_messages(raw_scenario)
        expected_calls = _parse_expected_calls(raw_scenario.get("expected_calls", []), name)
        expected_effects = _parse_expected_effects(raw_scenario.get("expected_effects", []), name)
        reply_expectations = _parse_reply_expectations(raw_scenario, name)
        scenarios.append(
            Scenario(
                name=name,
                messages=messages,
                expected_calls=expected_calls,
                expected_effects=expected_effects,
                reply_expectations=reply_expectations,
                area=_str_or_none(raw_scenario.get("area")),
                user=_str_or_none(raw_scenario.get("user")),
                strict=bool(raw_scenario.get("strict", False)),
            )
        )
    return scenarios


def _parse_messages(raw_scenario: dict[str, Any]) -> tuple[str, ...]:
    if "messages" in raw_scenario:
        raw_messages = raw_scenario["messages"]
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError("scenario messages must be a non-empty list")
        messages = tuple(message for message in raw_messages if isinstance(message, str) and message)
        if len(messages) != len(raw_messages):
            raise ValueError("scenario messages must contain only non-empty strings")
        return messages
    message = raw_scenario.get("message")
    if not isinstance(message, str) or not message:
        raise ValueError("scenario must contain message or messages")
    return (message,)


def _parse_expected_calls(raw_calls: Any, scenario_name: str) -> tuple[ExpectedCall, ...]:
    if not isinstance(raw_calls, list):
        raise ValueError(f"scenario {scenario_name}: expected_calls must be a list")
    expected_calls = []
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, dict):
            raise ValueError(f"scenario {scenario_name}: expected call #{index + 1} must be a mapping")
        tool = raw_call.get("tool")
        if not isinstance(tool, str) or not tool:
            raise ValueError(f"scenario {scenario_name}: expected call #{index + 1} must contain tool")
        arguments = raw_call.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"scenario {scenario_name}: expected call #{index + 1} arguments must be a mapping")
        expected_calls.append(
            ExpectedCall(
                tool=tool,
                arguments=arguments,
                result=raw_call.get("result"),
                optional=bool(raw_call.get("optional", False)),
            )
        )
    return tuple(expected_calls)


def _parse_expected_effects(raw_effects: Any, scenario_name: str) -> tuple[ExpectedEffect, ...]:
    if not isinstance(raw_effects, list):
        raise ValueError(f"scenario {scenario_name}: expected_effects must be a list")
    expected_effects = []
    for index, raw_effect in enumerate(raw_effects):
        if not isinstance(raw_effect, dict):
            raise ValueError(f"scenario {scenario_name}: expected effect #{index + 1} must be a mapping")
        device = raw_effect.get("device")
        property_name = raw_effect.get("property_name")
        if not isinstance(device, str) or not device:
            raise ValueError(f"scenario {scenario_name}: expected effect #{index + 1} must contain device")
        if not isinstance(property_name, str) or not property_name:
            raise ValueError(f"scenario {scenario_name}: expected effect #{index + 1} must contain property_name")
        if "value" not in raw_effect:
            raise ValueError(f"scenario {scenario_name}: expected effect #{index + 1} must contain value")
        expected_effects.append(ExpectedEffect(device=device, property_name=property_name, value=raw_effect["value"]))
    return tuple(expected_effects)


def _parse_reply_expectations(raw_scenario: dict[str, Any], scenario_name: str) -> tuple[ReplyExpectation, ...]:
    if "confirmations" in raw_scenario:
        raw_confirmations = raw_scenario["confirmations"]
        if not isinstance(raw_confirmations, list):
            raise ValueError(f"scenario {scenario_name}: confirmations must be a list")
        return tuple(_parse_reply_expectation(item, scenario_name) for item in raw_confirmations)
    if "confirmation" in raw_scenario:
        return (_parse_reply_expectation(raw_scenario["confirmation"], scenario_name),)
    return (ReplyExpectation(),)


def _parse_reply_expectation(raw_expectation: Any, scenario_name: str) -> ReplyExpectation:
    if raw_expectation is None:
        return ReplyExpectation()
    if not isinstance(raw_expectation, dict):
        raise ValueError(f"scenario {scenario_name}: confirmation must be a mapping")
    raw_contains_all = raw_expectation.get("contains_all", ())
    if raw_contains_all is None:
        raw_contains_all = ()
    if not isinstance(raw_contains_all, list):
        raise ValueError(f"scenario {scenario_name}: confirmation.contains_all must be a list")
    contains_all = tuple(item for item in raw_contains_all if isinstance(item, str) and item)
    if len(contains_all) != len(raw_contains_all):
        raise ValueError(f"scenario {scenario_name}: confirmation.contains_all must contain only non-empty strings")
    return ReplyExpectation(
        contains_all=contains_all,
        one_sentence=bool(raw_expectation.get("one_sentence", True)),
    )


def _score_scenario(result: ScenarioResult, inventory: HomeAssistantInventory) -> None:
    _score_expected_effects(result, inventory)
    _score_reply_expectations(result)

    actual_index = 0
    matched_actual_indexes = set()
    for expected_index, expected in enumerate(result.scenario.expected_calls):
        match_index = _find_matching_call(expected, result.actual_calls, actual_index, inventory)
        if match_index is None:
            message = (
                f"missing expected call #{expected_index + 1}: "
                f"{expected.tool} {json.dumps(expected.arguments, ensure_ascii=False)}"
            )
            if expected.optional:
                result.warnings.append(f"optional {message}")
            else:
                result.failures.append(message)
            continue
        matched_actual_indexes.add(match_index)
        actual_index = match_index + 1

    extra_indexes = [index for index in range(len(result.actual_calls)) if index not in matched_actual_indexes]
    if result.scenario.strict and extra_indexes:
        for index in extra_indexes:
            actual = result.actual_calls[index]
            result.failures.append(
                f"unexpected call #{index + 1}: {actual.tool} {json.dumps(actual.arguments, ensure_ascii=False)}"
            )
    elif extra_indexes:
        result.warnings.append(f"{len(extra_indexes)} extra model tool call(s) were ignored by subset matching")


def _score_expected_effects(result: ScenarioResult, inventory: HomeAssistantInventory) -> None:
    if not result.scenario.expected_effects:
        return

    expected_effects = [_normalize_effect(effect, inventory) for effect in result.scenario.expected_effects]
    actual_effects = _actual_modification_effects(result.actual_calls, inventory)

    for expected in expected_effects:
        if expected not in actual_effects:
            result.failures.append(f"missing expected effect: {_format_effect(expected)}")

    for actual in actual_effects:
        if actual not in expected_effects:
            result.failures.append(f"unexpected modification effect: {_format_effect(actual)}")


def _score_reply_expectations(result: ScenarioResult) -> None:
    if not result.replies:
        result.failures.append("missing assistant confirmation reply")
        return

    expectations = result.scenario.reply_expectations or (ReplyExpectation(),)
    if len(expectations) == len(result.replies):
        pairs = tuple(zip(result.replies, expectations))
    else:
        pairs = tuple((reply, ReplyExpectation()) for reply in result.replies[:-1])
        pairs += ((result.replies[-1], expectations[-1]),)

    for index, (reply, expectation) in enumerate(pairs, start=1):
        if not reply.strip():
            result.failures.append(f"reply #{index} is empty")
            continue
        if expectation.one_sentence and not _is_one_sentence(reply):
            result.failures.append(f"reply #{index} is not one sentence: {reply!r}")
        normalized_reply = _normalize_text(reply)
        for expected_text in expectation.contains_all:
            if _normalize_text(expected_text) not in normalized_reply:
                result.failures.append(f"reply #{index} does not contain {expected_text!r}: {reply!r}")


def _actual_modification_effects(
    actual_calls: list[ToolCallRecord],
    inventory: HomeAssistantInventory,
) -> list[tuple[str, str, Any]]:
    effects = []
    for call in actual_calls:
        property_name = call.arguments.get("property_name")
        value = call.arguments.get("value")
        if not isinstance(property_name, str):
            continue
        if call.tool == "modify_device":
            if not _single_modify_succeeded(call.result):
                continue
            device = call.arguments.get("device")
            if isinstance(device, str):
                effects.append(_normalize_effect(ExpectedEffect(device=device, property_name=property_name, value=value), inventory))
        elif call.tool == "modify_devices":
            if _batch_modify_rejected(call.result):
                continue
            devices = call.arguments.get("devices")
            successful_devices = _successful_batch_devices(call.result)
            if successful_devices:
                for device in successful_devices:
                    effects.append(_normalize_effect(ExpectedEffect(device=device, property_name=property_name, value=value), inventory))
            elif isinstance(devices, list):
                for device in devices:
                    if isinstance(device, str):
                        effects.append(_normalize_effect(ExpectedEffect(device=device, property_name=property_name, value=value), inventory))
    return effects


def _single_modify_succeeded(result: Any) -> bool:
    if result is None:
        return True
    return isinstance(result, dict) and result.get("status") == "ok"


def _batch_modify_rejected(result: Any) -> bool:
    return isinstance(result, dict) and result.get("status") == "rejected"


def _successful_batch_devices(result: Any) -> list[str]:
    if not isinstance(result, dict):
        return []
    raw_results = result.get("results")
    if not isinstance(raw_results, list):
        return []
    devices = []
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            continue
        nested_result = raw_result.get("result")
        if isinstance(nested_result, dict) and nested_result.get("status") != "ok":
            continue
        if raw_result.get("status") not in (None, "ok"):
            continue
        device = raw_result.get("device") or raw_result.get("device_id") or raw_result.get("device_name")
        if isinstance(device, str):
            devices.append(device)
    return devices


def _batch_scope_rejection(
    devices: list[HomeAssistantDevice],
    inventory: HomeAssistantInventory,
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

    current_area_id = _canonical_area_value(current_area, inventory) if current_area else ""
    if current_area_id and all(device.area_id == current_area_id for device in devices):
        return None

    current_area_record = inventory.areas_by_id.get(current_area_id)
    target_area_text = current_area_record.name if current_area_record is not None else "the named/current area"
    return {
        "status": "rejected",
        "error": "batch_scope_not_allowed",
        "message": (
            "The user did not ask for all matching devices. Do not claim success. "
            f"Use modify_device for the single matching device in {target_area_text}, "
            "or ask a clarification question if no single device is clear."
        ),
        "current_area_name": current_area or "",
        "current_area": _area_to_mapping(current_area_record) if current_area_record is not None else None,
        "rejected_devices": [_device_to_mapping(device, inventory) for device in devices],
    }


def _has_global_scope(user_message: str) -> bool:
    normalized_message = _normalize_text(user_message)
    return any(_phrase_in_normalized_text(term, normalized_message) for term in GLOBAL_SCOPE_TERMS)


def _phrase_in_normalized_text(phrase: str, normalized_text: str) -> bool:
    normalized_phrase = _normalize_text(phrase)
    return f" {normalized_phrase} " in f" {normalized_text} "


def _normalize_effect(effect: ExpectedEffect, inventory: HomeAssistantInventory) -> tuple[str, str, Any]:
    return (
        _canonical_device_value(effect.device, inventory),
        _normalize_property_name(effect.property_name),
        _normalize_property_value(effect.property_name, effect.value),
    )


def _format_effect(effect: tuple[str, str, Any]) -> str:
    return json.dumps(
        {
            "device": effect[0],
            "property_name": effect[1],
            "value": effect[2],
        },
        ensure_ascii=False,
    )


def _is_one_sentence(reply: str) -> bool:
    stripped = reply.strip()
    if "\n" in stripped:
        return False
    terminal_count = sum(stripped.count(character) for character in ".!?")
    return terminal_count <= 1


def _find_matching_call(
    expected: ExpectedCall,
    actual_calls: list[ToolCallRecord],
    start_index: int,
    inventory: HomeAssistantInventory,
) -> int | None:
    for index in range(start_index, len(actual_calls)):
        actual = actual_calls[index]
        if actual.tool != expected.tool:
            continue
        if _arguments_match(expected.arguments, actual.arguments, inventory):
            return index
    return None


def _arguments_match(expected: dict[str, Any], actual: dict[str, Any], inventory: HomeAssistantInventory) -> bool:
    for key, expected_value in expected.items():
        if key not in actual:
            return False
        if not _value_matches(key, expected_value, actual[key], expected, actual, inventory):
            return False
    return True


def _value_matches(
    key: str,
    expected: Any,
    actual: Any,
    expected_arguments: dict[str, Any],
    actual_arguments: dict[str, Any],
    inventory: HomeAssistantInventory,
) -> bool:
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        normalized_actual = [_normalize_argument_value(key, item, actual_arguments, inventory) for item in actual]
        return all(_normalize_argument_value(key, item, expected_arguments, inventory) in normalized_actual for item in expected)
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return _arguments_match(expected, actual, inventory)
    return _normalize_argument_value(key, expected, expected_arguments, inventory) == _normalize_argument_value(
        key,
        actual,
        actual_arguments,
        inventory,
    )


def _normalize_argument_value(
    key: str,
    value: Any,
    arguments: dict[str, Any],
    inventory: HomeAssistantInventory,
) -> Any:
    if key == "value":
        property_name = arguments.get("property_name")
        if isinstance(property_name, str):
            return _normalize_property_value(property_name, value)
    if not isinstance(value, str):
        return value
    if key in ("device", "devices"):
        return _canonical_device_value(value, inventory)
    if key == "area_name":
        return _canonical_area_value(value, inventory)
    if key == "device_type":
        return _normalize_device_type(value)
    if key == "property_name":
        return _normalize_property_name(value)
    if key == "query":
        return frozenset(_normalize_text(value).split())
    return _normalize_text(value)


def _build_inventory(raw_home_assistant: Any) -> HomeAssistantInventory:
    raw_inventory = {}
    if isinstance(raw_home_assistant, dict):
        raw_inventory = raw_home_assistant.get("inventory", {})
    if not isinstance(raw_inventory, dict):
        raw_inventory = {}

    raw_areas = raw_inventory.get("areas", _default_areas())
    raw_devices = raw_inventory.get("devices", _default_devices())
    if not isinstance(raw_areas, list) or not isinstance(raw_devices, list):
        raise ValueError("home_assistant.inventory.areas and devices must be lists")

    areas_by_id = {}
    area_lookup: dict[str, list[str]] = {}
    for raw_area in raw_areas:
        if not isinstance(raw_area, dict):
            raise ValueError("area entries must be mappings")
        area = HomeAssistantArea(
            area_id=_required_str(raw_area, "area_id"),
            name=_required_str(raw_area, "name"),
            aliases=tuple(_str_list(raw_area.get("aliases", []))),
        )
        areas_by_id[area.area_id] = area
        for lookup_value in (area.area_id, area.name, *area.aliases):
            area_lookup.setdefault(lookup_value, []).append(area.area_id)

    devices_by_id = {}
    devices_by_area: dict[str, list[HomeAssistantDevice]] = {}
    device_lookup: dict[str, list[str]] = {}
    for raw_device in raw_devices:
        if not isinstance(raw_device, dict):
            raise ValueError("device entries must be mappings")
        device = _build_device(raw_device)
        devices_by_id[device.device_id] = device
        devices_by_area.setdefault(device.area_id, []).append(device)
        lookup_values = [device.device_id, device.name, *device.aliases]
        for entity in device.entities:
            lookup_values.extend([entity.entity_id, entity.name, *entity.aliases])
        for lookup_value in lookup_values:
            device_lookup.setdefault(lookup_value, []).append(device.device_id)

    media_players = _build_media_players(devices_by_id, areas_by_id)
    return HomeAssistantInventory(
        areas_by_id=areas_by_id,
        devices_by_id=devices_by_id,
        devices_by_area={area_id: tuple(devices) for area_id, devices in devices_by_area.items()},
        area_lookup={key: tuple(value) for key, value in area_lookup.items()},
        device_lookup={key: tuple(value) for key, value in device_lookup.items()},
        media_players_by_entity_id={player.entity_id: player for player in media_players},
        media_players_by_area=_build_media_players_by_area(media_players, areas_by_id),
    )


def _build_device(raw_device: dict[str, Any]) -> HomeAssistantDevice:
    device_id = _required_str(raw_device, "device_id")
    area_id = _required_str(raw_device, "area_id")
    device_type = _normalize_device_type(_required_str(raw_device, "type"))
    raw_entities = raw_device.get("entities", [])
    raw_properties = raw_device.get("properties", [])
    if not isinstance(raw_entities, list) or not isinstance(raw_properties, list):
        raise ValueError("device entities and properties must be lists")

    entities = tuple(_build_entity(raw_entity, device_id, area_id, device_type) for raw_entity in raw_entities)
    properties = tuple(_build_property(raw_property, device_type, entities) for raw_property in raw_properties)
    return HomeAssistantDevice(
        device_id=device_id,
        area_id=area_id,
        name=_required_str(raw_device, "name"),
        aliases=tuple(_str_list(raw_device.get("aliases", []))),
        device_type=device_type,
        entities=entities,
        properties=properties,
    )


def _build_entity(
    raw_entity: dict[str, Any],
    device_id: str,
    area_id: str,
    device_type: str,
) -> HomeAssistantEntity:
    if not isinstance(raw_entity, dict):
        raise ValueError("entity entries must be mappings")
    return HomeAssistantEntity(
        entity_id=_required_str(raw_entity, "entity_id"),
        device_id=device_id,
        area_id=area_id,
        domain=_str_or_default(raw_entity.get("domain"), device_type),
        name=_required_str(raw_entity, "name"),
        aliases=tuple(_str_list(raw_entity.get("aliases", []))),
        state=_str_or_default(raw_entity.get("state"), "unknown"),
        attributes=_dict_or_empty(raw_entity.get("attributes")),
        platform=_str_or_default(raw_entity.get("platform"), ""),
        config_entry_id=_str_or_default(raw_entity.get("config_entry_id"), ""),
    )


def _build_property(
    raw_property: dict[str, Any],
    device_type: str,
    entities: tuple[HomeAssistantEntity, ...],
) -> ModifiableProperty:
    if not isinstance(raw_property, dict):
        raise ValueError("property entries must be mappings")
    entity_id = _str_or_none(raw_property.get("entity_id"))
    if entity_id is None and entities:
        entity_id = entities[0].entity_id
    if entity_id is None:
        raise ValueError("property entity_id is required when device has no entities")
    return ModifiableProperty(
        property_name=_required_str(raw_property, "property_name"),
        entity_id=entity_id,
        domain=_str_or_default(raw_property.get("domain"), device_type),
        value_type=_str_or_default(raw_property.get("value_type"), "string"),
        description=_str_or_default(raw_property.get("description"), ""),
        min_value=_float_or_none(raw_property.get("min_value")),
        max_value=_float_or_none(raw_property.get("max_value")),
        step=_float_or_none(raw_property.get("step")),
        allowed_values=tuple(raw_property.get("allowed_values", ())),
    )


def _build_media_players(
    devices_by_id: dict[str, HomeAssistantDevice],
    areas_by_id: dict[str, HomeAssistantArea],
) -> tuple[HomeAssistantMediaPlayer, ...]:
    players = []
    for device in devices_by_id.values():
        area = areas_by_id.get(device.area_id)
        if area is None:
            continue
        for entity in device.entities:
            if entity.domain != "media_player":
                continue
            players.append(
                HomeAssistantMediaPlayer(
                    entity_id=entity.entity_id,
                    device_id=device.device_id,
                    area_id=device.area_id,
                    area_name=area.name,
                    name=device.name,
                    aliases=device.aliases,
                    state=entity.state,
                    attributes=entity.attributes,
                    volume_level=_float_or_none(entity.attributes.get("volume_level")),
                    is_music_assistant=entity.platform == "music_assistant",
                    is_speaker=bool(entity.attributes.get("is_volume_muted") is not None or entity.attributes.get("supported_features")),
                    platform=entity.platform,
                    config_entry_id=entity.config_entry_id,
                )
            )
    return tuple(sorted(players, key=lambda player: player.name.casefold()))


def _build_media_players_by_area(
    media_players: tuple[HomeAssistantMediaPlayer, ...],
    areas_by_id: dict[str, HomeAssistantArea],
) -> dict[str, tuple[HomeAssistantMediaPlayer, ...]]:
    return {
        area_id: tuple(player for player in media_players if player.area_id == area_id)
        for area_id in areas_by_id
    }


def _print_results(results: list[ScenarioResult], *, verbose: bool) -> None:
    passed = sum(1 for result in results if result.passed)
    total_duration_seconds = sum(result.duration_seconds for result in results)
    print(f"agent-tool-eval: {passed}/{len(results)} scenarios passed in {_format_duration(total_duration_seconds)}")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(
            f"\n[{status}] {result.scenario.name} "
            f"duration={_format_duration(result.duration_seconds)} eval_count={result.eval_count} calls={len(result.actual_calls)}"
        )
        if result.failures:
            for failure in result.failures:
                print(f"  failure: {failure}")
        if result.warnings:
            for warning in result.warnings:
                print(f"  warning: {warning}")
        if verbose or not result.passed:
            for index, call in enumerate(result.actual_calls, start=1):
                print(f"  call {index}: {call.tool} {json.dumps(call.arguments, ensure_ascii=False)}")
            for index, reply in enumerate(result.replies, start=1):
                print(f"  reply {index}: {reply}")


def _print_transcript_scenario_start(
    scenario: Scenario,
    model: str,
    ollama_url: str,
    user: str | None,
    area: str | None,
) -> None:
    print("")
    print(f"=== Scenario: {scenario.name} ===", flush=True)
    print(f"model: {model}  ollama: {ollama_url}  user: {user or 'unknown'}  area: {area or 'unknown'}", flush=True)


def _print_transcript_user_message(message: str) -> None:
    print(f"> user: {message}", flush=True)


def _print_transcript_tool_call(tool: str, arguments: dict[str, Any], result: Any) -> None:
    print(f"> tool call: {tool} {json.dumps(arguments, ensure_ascii=False)}", flush=True)
    print(f"< tool reply: {json.dumps(result, ensure_ascii=False)}", flush=True)


def _print_transcript_context_message(message: dict[str, Any]) -> None:
    if message.get("role") != "assistant":
        return
    thinking = _extract_thinking(message)
    if thinking:
        print(f"< thinking: {thinking}", flush=True)


def _extract_thinking(message: dict[str, Any]) -> str:
    thinking = message.get("thinking")
    if isinstance(thinking, str) and thinking.strip():
        return thinking.strip()

    content = message.get("content")
    if not isinstance(content, str):
        return ""
    start_tag = "<think>"
    end_tag = "</think>"
    start = content.find(start_tag)
    end = content.find(end_tag)
    if start == -1 or end == -1 or end <= start:
        return ""
    return content[start + len(start_tag) : end].strip()


def _print_transcript_assistant_reply(reply_text: str, end_conversation: bool) -> None:
    suffix = " end_conversation=true" if end_conversation else ""
    print(f"< assistant{suffix}: {reply_text}", flush=True)


def _print_transcript_scenario_end(result: ScenarioResult) -> None:
    status = "PASS" if result.passed else "FAIL"
    print(
        f"=== {status}: {result.scenario.name} "
        f"duration={_format_duration(result.duration_seconds)} eval_count={result.eval_count} ===",
        flush=True,
    )


def _format_duration(duration_seconds: float) -> str:
    if duration_seconds < 1:
        return f"{duration_seconds * 1000:.0f}ms"
    return f"{duration_seconds:.2f}s"


def _format_device_type_vocabulary() -> str:
    lines = []
    for device_type, aliases in sorted(DEVICE_TYPE_ALIASES.items()):
        lines.append(f"- {device_type}: {', '.join(aliases)}")
    return "\n".join(lines)


def _format_value_vocabulary() -> str:
    lines = []
    for property_name, values in sorted(PROPERTY_VALUE_ALIASES.items()):
        value_text = "; ".join(f"{value}: {', '.join(aliases)}" for value, aliases in values.items())
        lines.append(f"- {property_name}: {value_text}")
    return "\n".join(lines)


def _device_to_mapping(device: HomeAssistantDevice, inventory: HomeAssistantInventory) -> dict[str, Any]:
    area = inventory.areas_by_id.get(device.area_id)
    return {
        "device_id": device.device_id,
        "name": device.name,
        "type": device.device_type,
        "aliases": list(device.aliases),
        "area_id": device.area_id,
        "area_name": area.name if area else device.area_id,
    }


def _media_player_to_mapping(player: HomeAssistantMediaPlayer) -> dict[str, Any]:
    return {
        "entity_id": player.entity_id,
        "device_id": player.device_id,
        "name": player.name,
        "area_id": player.area_id,
        "area_name": player.area_name,
        "aliases": list(player.aliases),
        "state": player.state,
        "volume_level": player.volume_level,
        "is_music_assistant": player.is_music_assistant,
        "is_speaker": player.is_speaker,
        "platform": player.platform,
        "config_entry_id": player.config_entry_id,
    }


def _area_to_mapping(area: HomeAssistantArea) -> dict[str, Any]:
    return {"area_id": area.area_id, "name": area.name, "aliases": list(area.aliases)}


def _property_to_mapping(property_info: ModifiableProperty) -> dict[str, Any]:
    value: dict[str, Any] = {
        "property_name": property_info.property_name,
        "entity_id": property_info.entity_id,
        "domain": property_info.domain,
        "value_type": property_info.value_type,
        "description": property_info.description,
    }
    if property_info.min_value is not None:
        value["min_value"] = property_info.min_value
    if property_info.max_value is not None:
        value["max_value"] = property_info.max_value
    if property_info.step is not None:
        value["step"] = property_info.step
    if property_info.allowed_values:
        value["allowed_values"] = list(property_info.allowed_values)
    return value


def _service_for_property(property_info: ModifiableProperty) -> str:
    if property_info.domain == "climate" and property_info.property_name == "hvac_mode":
        return "climate.set_hvac_mode"
    if property_info.domain == "climate" and property_info.property_name == "target_temperature":
        return "climate.set_temperature"
    if property_info.property_name == "on":
        return "homeassistant.turn_on_off"
    return f"{property_info.domain}.set_{property_info.property_name}"


def _device_search_values(device: HomeAssistantDevice, inventory: HomeAssistantInventory) -> tuple[str, ...]:
    area = inventory.areas_by_id.get(device.area_id)
    values = [device.device_id, device.name, device.device_type, *device.aliases]
    if area is not None:
        values.extend([area.area_id, area.name, *area.aliases])
    for entity in device.entities:
        values.extend([entity.entity_id, entity.name, entity.domain, *entity.aliases])
    values.extend(DEVICE_TYPE_ALIASES.get(device.device_type, ()))
    return tuple(values)


def _normalize_device_type(value: str) -> str:
    normalized = _normalize_text(value)
    for device_type, aliases in DEVICE_TYPE_ALIASES.items():
        if normalized == _normalize_text(device_type):
            return device_type
        if normalized in {_normalize_text(alias) for alias in aliases}:
            return device_type
    return normalized


def _normalize_property_value(property_name: str, value: Any) -> Any:
    normalized = _normalize_text(_yaml_scalar_alias(value))
    for canonical_value, aliases in PROPERTY_VALUE_ALIASES.get(property_name, {}).items():
        if normalized == _normalize_text(str(canonical_value)):
            return canonical_value
        if normalized in {_normalize_text(alias) for alias in aliases}:
            return canonical_value
    return normalized


def _normalize_property_name(value: str) -> str:
    return "_".join(_normalize_text(value).split())


def _yaml_scalar_alias(value: Any) -> str:
    if value is False:
        return "off"
    if value is True:
        return "on"
    return str(value)


def _canonical_device_value(value: str, inventory: HomeAssistantInventory) -> str:
    normalized = _normalize_text(value)
    for lookup_value, device_ids in inventory.device_lookup.items():
        if _normalize_text(lookup_value) == normalized:
            return device_ids[0]
    return normalized


def _canonical_area_value(value: str, inventory: HomeAssistantInventory) -> str:
    normalized = _normalize_text(value)
    for lookup_value, area_ids in inventory.area_lookup.items():
        if _normalize_text(lookup_value) == normalized:
            return area_ids[0]
    return normalized


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    ascii_value = "".join(character for character in decomposed if not unicodedata.combining(character))
    return " ".join(ascii_value.replace("_", " ").replace("-", " ").split())


def _device_type_alias_terms() -> set[str]:
    terms = set()
    for device_type, aliases in DEVICE_TYPE_ALIASES.items():
        terms.update(_normalize_text(device_type).split())
        for alias in aliases:
            terms.update(_normalize_text(alias).split())
    return terms


def _default_areas() -> list[dict[str, Any]]:
    return [
        {"area_id": "office", "name": "Office", "aliases": ["biuro", "gabinet", "pracownia"]},
        {"area_id": "living_room", "name": "Living room", "aliases": ["salon", "pokój dzienny"]},
        {"area_id": "bedroom", "name": "Bedroom", "aliases": ["sypialnia"]},
        {"area_id": "bathroom", "name": "Bathroom", "aliases": ["łazienka", "kibel"]},
    ]


def _default_devices() -> list[dict[str, Any]]:
    return [
        _default_climate_device("office_ac", "office", "Study air conditioner", "climate.study_air_conditioner", ["klima w biurze", "klimatyzacja w biurze", "office air conditioner"]),
        _default_climate_device("living_room_ac", "living_room", "Living room air conditioner", "climate.living_room_air_conditioner", ["klima w salonie", "klimatyzacja w salonie", "salon air conditioner"]),
        _default_climate_device("bedroom_ac", "bedroom", "Bedroom air conditioner", "climate.bedroom_air_conditioner", ["klima w sypialni", "klimatyzacja w sypialni", "bedroom air conditioner"]),
        _default_media_player_device("office_sonos", "office", "Office", "media_player.office", is_music_assistant=False),
        _default_media_player_device("office_ma", "office", "Office", "media_player.office_2", is_music_assistant=True),
        _default_media_player_device("bedroom_sonos", "bedroom", "Bedroom", "media_player.bedroom", is_music_assistant=False),
        _default_media_player_device("bedroom_ma", "bedroom", "Bedroom", "media_player.bedroom_2", is_music_assistant=True),
        _default_media_player_device("living_room_sonos", "living_room", "Living Room", "media_player.living_room", is_music_assistant=False),
        _default_media_player_device("living_room_ma", "living_room", "Living Room", "media_player.living_room_2", is_music_assistant=True),
        _default_media_player_device("bathroom_sonos", "bathroom", "Bathroom", "media_player.bathroom", is_music_assistant=False),
        _default_media_player_device("bathroom_ma", "bathroom", "Bathroom", "media_player.bathroom_2", is_music_assistant=True),
    ]


def _default_climate_device(
    device_id: str,
    area_id: str,
    name: str,
    entity_id: str,
    aliases: list[str],
) -> dict[str, Any]:
    return {
        "device_id": device_id,
        "area_id": area_id,
        "name": name,
        "type": "climate",
        "aliases": aliases,
        "entities": [
            {
                "entity_id": entity_id,
                "name": name,
                "aliases": aliases,
                "state": "cool",
                "attributes": {"temperature": 24, "hvac_modes": ["off", "cool", "heat", "fan_only"]},
            }
        ],
        "properties": [
            {
                "property_name": "hvac_mode",
                "entity_id": entity_id,
                "domain": "climate",
                "value_type": "string",
                "description": "HVAC mode",
                "allowed_values": ["off", "cool", "heat", "fan_only"],
            },
            {
                "property_name": "target_temperature",
                "entity_id": entity_id,
                "domain": "climate",
                "value_type": "number",
                "description": "Target temperature in Celsius",
                "min_value": 16,
                "max_value": 30,
                "step": 0.5,
            },
        ],
    }


def _default_media_player_device(
    device_id: str,
    area_id: str,
    name: str,
    entity_id: str,
    *,
    is_music_assistant: bool,
) -> dict[str, Any]:
    platform = "music_assistant" if is_music_assistant else "sonos"
    return {
        "device_id": device_id,
        "area_id": area_id,
        "name": name,
        "type": "media_player",
        "aliases": [f"{name} speaker", f"{name} music"],
        "entities": [
            {
                "entity_id": entity_id,
                "domain": "media_player",
                "name": name,
                "aliases": [f"{name} speaker", f"{name} music"],
                "state": "idle",
                "platform": platform,
                "config_entry_id": "ma-1" if is_music_assistant else "sonos-1",
                "attributes": {
                    "friendly_name": name,
                    "supported_features": 4127295,
                    "volume_level": 0.3,
                    "is_volume_muted": False,
                },
            }
        ],
        "properties": [],
    }


def _parse_think(raw_override: str | None, default: Any) -> bool | str | None:
    if raw_override == "true":
        return True
    if raw_override == "false":
        return False
    if raw_override == "none":
        return None
    if isinstance(default, (bool, str)) or default is None:
        return default
    return False


def _think_enabled(value: bool | str | None) -> bool:
    if value is True:
        return True
    if not isinstance(value, str):
        return False
    return value.casefold() not in ("", "0", "false", "none", "no")


def _required_str(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _str_or_default(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("expected a list of strings")
    strings = [item for item in value if isinstance(item, str)]
    if len(strings) != len(value):
        raise ValueError("expected a list of strings")
    return strings


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _float_or_default(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return default


def _int_or_default(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


if __name__ == "__main__":
    raise SystemExit(main())
