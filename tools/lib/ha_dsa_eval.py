from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import agent_tool_eval
from ai_server.domain_agents.home_assistant import HomeAssistantDomainAgent
from ai_server.interfaces import Conversation


DEFAULT_SCENARIOS = Path("tools/lib/ha-dsa-eval/scenarios.yaml")


@dataclass(frozen=True)
class Scenario:
    name: str
    task: dict[str, Any]
    expected_calls: tuple[Any, ...]
    expected_effects: tuple[Any, ...]
    reply_expectations: tuple[Any, ...]
    active_context: dict[str, Any] = field(default_factory=dict)
    location: str | None = None
    user: str | None = None
    strict: bool = False


def main() -> int:
    args = _parse_args()
    config = _load_eval_config(args.scenarios)
    scenarios = _load_scenarios(config)
    if args.scenario:
        selected = set(args.scenario)
        scenarios = [scenario for scenario in scenarios if scenario.name in selected]
        missing = selected - {scenario.name for scenario in scenarios}
        if missing:
            raise ValueError(f"unknown scenario(s): {', '.join(sorted(missing))}")
    if args.list:
        for scenario in scenarios:
            print(scenario.name)
        return 0

    defaults = _dict_or_empty(config.get("defaults"))
    results = asyncio.run(_run_scenarios(scenarios, defaults, args))
    if not args.no_transcript:
        _print_transcripts(results)
    agent_tool_eval._print_results(results, verbose=args.verbose)
    return 0 if all(result.passed for result in results) else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate Home Assistant DSA command execution.")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS, help="YAML scenario file.")
    parser.add_argument("--scenario", action="append", default=[], help="Run only scenarios with this exact name.")
    parser.add_argument("--model", help="Ollama model name. Defaults from YAML.")
    parser.add_argument("--ollama-url", help="Ollama URL. Defaults from YAML.")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit.")
    parser.add_argument("--no-transcript", action="store_true", help="Do not print DSA transcript.")
    parser.add_argument("--verbose", action="store_true", help="Print full calls and mismatch details.")
    return parser.parse_args()


def _load_eval_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    if not isinstance(config, dict):
        raise ValueError("scenario file must contain a YAML mapping")
    if config.get("domain") != "home_assistant_dsa":
        raise ValueError("scenario file domain must be home_assistant_dsa")
    return config


def _load_scenarios(config: dict[str, Any]) -> list[Scenario]:
    raw_scenarios = config.get("scenarios")
    if not isinstance(raw_scenarios, list):
        raise ValueError("scenarios must be a list")

    scenarios = []
    for index, raw_scenario in enumerate(raw_scenarios):
        if not isinstance(raw_scenario, dict):
            raise ValueError(f"scenario #{index + 1} must be a mapping")
        name = _required_string(raw_scenario, "name", f"scenario #{index + 1}")
        task = raw_scenario.get("task")
        if not isinstance(task, dict):
            raise ValueError(f"{name}.task must be a mapping")
        active_context = raw_scenario.get("active_context", {})
        if not isinstance(active_context, dict):
            raise ValueError(f"{name}.active_context must be a mapping")
        scenarios.append(
            Scenario(
                name=name,
                task=copy.deepcopy(task),
                active_context=copy.deepcopy(active_context),
                expected_calls=agent_tool_eval._parse_expected_calls(raw_scenario.get("expected_calls", []), name),
                expected_effects=agent_tool_eval._parse_expected_effects(raw_scenario.get("expected_effects", []), name),
                reply_expectations=agent_tool_eval._parse_reply_expectations(raw_scenario, name),
                location=_str_or_none(raw_scenario.get("location")),
                user=_str_or_none(raw_scenario.get("user")),
                strict=bool(raw_scenario.get("strict", False)),
            )
        )
    return scenarios


async def _run_scenarios(scenarios: list[Scenario], defaults: dict[str, Any], args: argparse.Namespace) -> list[Any]:
    inventory_path = Path(_str_or_default(defaults.get("inventory"), "tools/lib/agent-tool-eval/home_assistant.yaml"))
    inventory_config = agent_tool_eval._load_eval_config(inventory_path)
    inventory = agent_tool_eval._build_inventory(inventory_config.get("home_assistant", {}))
    results = []
    for scenario in scenarios:
        results.append(await _run_scenario(scenario, defaults, args, inventory))
    return results


async def _run_scenario(
    scenario: Scenario,
    defaults: dict[str, Any],
    args: argparse.Namespace,
    inventory,
) -> Any:
    model = args.model or _str_or_default(defaults.get("model"), "qwen3:4b-instruct")
    ollama_url = args.ollama_url or _str_or_default(defaults.get("ollama_url"), "http://127.0.0.1:11434")
    location = scenario.location or _str_or_none(defaults.get("location"))
    user = scenario.user or _str_or_none(defaults.get("user"))
    fake_connection = agent_tool_eval.FakeHomeAssistantConnection(inventory, scenario.expected_calls, transcript=False)
    dsa = HomeAssistantDomainAgent(model=model, ollama_url=ollama_url, connection=fake_connection)
    conversation = Conversation(
        conversation_id=f"ha-dsa-eval-{scenario.name}",
        attributes={key: value for key, value in (("location", location), ("user", user)) if isinstance(value, str)},
    )
    started_at = time.perf_counter()
    result = agent_tool_eval.ScenarioResult(
        scenario=agent_tool_eval.Scenario(
            name=scenario.name,
            messages=(json.dumps(scenario.task, ensure_ascii=False),),
            expected_calls=scenario.expected_calls,
            expected_effects=scenario.expected_effects,
            reply_expectations=scenario.reply_expectations,
            location=location,
            user=user,
            strict=scenario.strict,
        )
    )
    result.task = scenario.task
    result.active_context = scenario.active_context
    try:
        dsa_result = await dsa.run_task(conversation, scenario.task, scenario.active_context)
    finally:
        await dsa.close()
    result.duration_seconds = time.perf_counter() - started_at
    result.replies.append(dsa_result.get("text", ""))
    result.dsa_result = dsa_result
    result.actual_calls.extend(fake_connection.calls)
    agent_tool_eval._score_scenario(result, inventory)
    return result


def _print_transcripts(results: list[Any]) -> None:
    for result in results:
        print(f"=== scenario: {result.scenario.name} ===")
        print("> orchestrator command:")
        print(json.dumps(result.task, ensure_ascii=False, indent=2, sort_keys=True))
        print("> active context:")
        print(json.dumps(result.active_context, ensure_ascii=False, indent=2, sort_keys=True))
        print("< HA tool calls:")
        print(json.dumps([call.__dict__ for call in result.actual_calls], ensure_ascii=False, indent=2, sort_keys=True))
        print("< HA DSA result:")
        print(json.dumps(result.dsa_result, ensure_ascii=False, indent=2, sort_keys=True))
        print()


def _required_string(raw_mapping: dict[str, Any], key: str, context: str) -> str:
    value = raw_mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _str_or_default(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value else default


if __name__ == "__main__":
    raise SystemExit(main())
