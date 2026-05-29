from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_agent_tool_eval_module():
    module_path = Path(__file__).resolve().parents[1] / "tools/lib/agent_tool_eval.py"
    spec = importlib.util.spec_from_file_location("agent_tool_eval", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_home_assistant_scenario_file_loads() -> None:
    agent_tool_eval = load_agent_tool_eval_module()

    config = agent_tool_eval._load_eval_config(Path("tools/agent-tool-eval/home_assistant.yaml"))
    scenarios = agent_tool_eval._load_scenarios(config)

    assert config["domain"] == "home_assistant"
    assert {scenario.name for scenario in scenarios} >= {
        "office-turn-off-climate-from-current-location",
        "turn-off-all-climate-devices",
        "follow-up-uses-existing-climate-context",
    }


def test_transcript_extracts_native_and_tagged_thinking(capsys) -> None:
    agent_tool_eval = load_agent_tool_eval_module()

    assert agent_tool_eval._think_enabled(True)
    assert not agent_tool_eval._think_enabled(False)
    assert agent_tool_eval._extract_thinking({"role": "assistant", "thinking": "Rozważam.", "content": ""}) == "Rozważam."
    assert agent_tool_eval._extract_thinking({"role": "assistant", "content": "<think>Plan.</think>\nGotowe."}) == "Plan."

    agent_tool_eval._print_transcript_context_message(
        {"role": "assistant", "thinking": "Sprawdzam narzędzia.", "content": ""}
    )

    assert "< thinking: Sprawdzam narzędzia." in capsys.readouterr().out


def test_results_include_duration(capsys) -> None:
    agent_tool_eval = load_agent_tool_eval_module()
    scenario = agent_tool_eval.Scenario(
        name="timed-scenario",
        messages=("hej",),
        expected_calls=(),
    )
    result = agent_tool_eval.ScenarioResult(scenario=scenario, duration_seconds=1.234)

    agent_tool_eval._print_results([result], verbose=False)
    agent_tool_eval._print_transcript_scenario_end(result)

    output = capsys.readouterr().out
    assert "agent-tool-eval: 1/1 scenarios passed in 1.23s" in output
    assert "[PASS] timed-scenario duration=1.23s eval_count=0 calls=0" in output
    assert "=== PASS: timed-scenario duration=1.23s eval_count=0 ===" in output


def test_home_assistant_call_matching_accepts_device_and_value_aliases() -> None:
    agent_tool_eval = load_agent_tool_eval_module()
    config = agent_tool_eval._load_eval_config(Path("tools/agent-tool-eval/home_assistant.yaml"))
    inventory = agent_tool_eval._build_inventory(config["home_assistant"])

    assert agent_tool_eval._arguments_match(
        {
            "device": "Study air conditioner",
            "property_name": "hvac_mode",
            "value": "off",
        },
        {
            "device": "klima w biurze",
            "property_name": "hvac_mode",
            "value": "wyłącz",
        },
        inventory,
    )


def test_home_assistant_call_matching_accepts_yaml_boolean_off_for_hvac_mode() -> None:
    agent_tool_eval = load_agent_tool_eval_module()
    config = agent_tool_eval._load_eval_config(Path("tools/agent-tool-eval/home_assistant.yaml"))
    inventory = agent_tool_eval._build_inventory(config["home_assistant"])

    assert agent_tool_eval._arguments_match(
        {
            "devices": ["Study air conditioner", "Living room air conditioner", "Bedroom air conditioner"],
            "property_name": "hvac_mode",
            "value": False,
        },
        {
            "devices": ["office_ac", "living_room_ac", "bedroom_ac"],
            "property_name": "hvac_mode",
            "value": "off",
        },
        inventory,
    )


def test_home_assistant_call_matching_accepts_area_aliases_and_type_aliases() -> None:
    agent_tool_eval = load_agent_tool_eval_module()
    config = agent_tool_eval._load_eval_config(Path("tools/agent-tool-eval/home_assistant.yaml"))
    inventory = agent_tool_eval._build_inventory(config["home_assistant"])

    assert agent_tool_eval._arguments_match(
        {
            "area_name": "Living room",
            "device_type": "climate",
        },
        {
            "area_name": "salon",
            "device_type": "klima",
            "query": "klimatyzator",
        },
        inventory,
    )


def test_home_assistant_turn_off_all_climate_log_shape_scores_as_success() -> None:
    agent_tool_eval = load_agent_tool_eval_module()
    config = agent_tool_eval._load_eval_config(Path("tools/agent-tool-eval/home_assistant.yaml"))
    inventory = agent_tool_eval._build_inventory(config["home_assistant"])
    scenario = next(
        scenario
        for scenario in agent_tool_eval._load_scenarios(config)
        if scenario.name == "turn-off-all-climate-devices"
    )
    result = agent_tool_eval.ScenarioResult(
        scenario=scenario,
        actual_calls=[
            agent_tool_eval.ToolCallRecord(
                tool="find_devices",
                arguments={"query": "", "device_type": "climate", "area_name": ""},
            ),
            agent_tool_eval.ToolCallRecord(
                tool="list_common_modifiable_properties",
                arguments={"devices": ["office_ac", "living_room_ac", "bedroom_ac"]},
            ),
            agent_tool_eval.ToolCallRecord(
                tool="modify_devices",
                arguments={
                    "devices": ["office_ac", "living_room_ac", "bedroom_ac"],
                    "property_name": "hvac_mode",
                    "value": "off",
                },
            ),
        ],
    )

    agent_tool_eval._score_scenario(result, inventory)

    assert result.failures == []
