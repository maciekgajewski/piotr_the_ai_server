from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path


def load_orchestrator_eval_module():
    module_path = Path(__file__).resolve().parents[1] / "tools/lib/orchestrator_eval.py"
    spec = importlib.util.spec_from_file_location("orchestrator_eval", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_orchestrator_scenario_file_loads() -> None:
    orchestrator_eval = load_orchestrator_eval_module()

    config = orchestrator_eval._load_eval_config(Path("tools/lib/orchestrator-eval/scenarios.yaml"))
    scenarios = orchestrator_eval._load_scenarios(config)

    assert config["domain"] == "orchestrator"
    assert {scenario.name for scenario in scenarios} == {
        "compose-ha-and-wikipedia",
        "followup-uses-active-context",
        "compose-ha-and-time",
        "ha-office-turn-off-climate-from-current-location",
        "ha-turn-off-all-climate-devices",
        "ha-living-room-climate-fan-only-by-room-alias",
        "ha-current-location-set-climate-temperature",
        "ha-follow-up-uses-existing-climate-context",
    }


def test_orchestrator_eval_mocked_scenarios_pass() -> None:
    orchestrator_eval = load_orchestrator_eval_module()
    config = orchestrator_eval._load_eval_config(Path("tools/lib/orchestrator-eval/scenarios.yaml"))
    scenarios = [
        scenario
        for scenario in orchestrator_eval._load_scenarios(config)
        if scenario.model_responses is not None
    ]

    results = asyncio.run(orchestrator_eval._run_scenarios(scenarios))

    assert all(result.passed for result in results)
