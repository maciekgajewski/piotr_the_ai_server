from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any

from ai_server.domain_agents.current_time import ROUTING_SUMMARY as TIME_ROUTING_SUMMARY
from ai_server.domain_agents.home_assistant import ROUTING_SUMMARY as HOME_ASSISTANT_ROUTING_SUMMARY
from ai_server.domain_agents.media_player.agent import ROUTING_SUMMARY as MEDIA_PLAYER_ROUTING_SUMMARY
from ai_server.domain_agents.system_status.agent import ROUTING_SUMMARY as SYSTEM_STATUS_ROUTING_SUMMARY
from ai_server.domain_agents.weather.agent import ROUTING_SUMMARY as WEATHER_ROUTING_SUMMARY
from ai_server.domain_agents.wikipedia import ROUTING_SUMMARY as WIKIPEDIA_ROUTING_SUMMARY
from ai_server.orchestrator.orchestrator import PLANNING_OPTIONS, _build_planning_system_prompt


DEFAULT_FIXTURE = Path("tests/fixtures/t007-route-only-planning-request.json")
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


class _SummaryAgent:
    def __init__(self, summary: str) -> None:
        self._summary = summary

    def routing_summary(self) -> str:
        return self._summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure the checked-in T-007 route-only planning request with Ollama."
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--model")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    args = parser.parse_args()

    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    model = args.model or fixture["model"]
    system_prompt, summary_bytes = _build_planning_system_prompt(_configured_summary_agents())
    planning_payload = {
        "utterance": fixture["utterance"],
        "conversation": fixture["conversation"],
        "active_context": fixture["active_context"],
    }
    request_payload = {
        "model": model,
        "raw": False,
        "think": False,
        "format": "json",
        "stream": False,
        "keep_alive": "1h",
        # Input tokenization is identical to the planner request. One generated
        # token keeps this acceptance measurement focused on prompt evaluation.
        "options": {**PLANNING_OPTIONS, "num_predict": 1},
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    planning_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
    }
    response = _post_json(
        f"{args.ollama_url.rstrip('/')}/api/chat",
        request_payload,
    )
    prompt_tokens = _required_int(response, "prompt_eval_count")
    completion_tokens = _required_int(response, "eval_count")
    fixed_prompt_bytes = len(system_prompt.encode("utf-8"))
    dynamic_payload_bytes = len(
        request_payload["messages"][1]["content"].encode("utf-8")
    )
    max_input_tokens = fixture["acceptance"]["max_input_tokens"]
    if prompt_tokens > max_input_tokens:
        raise RuntimeError(
            f"route-only request used {prompt_tokens} input tokens; limit is {max_input_tokens}"
        )

    print(
        json.dumps(
            {
                "model": model,
                "fixed_prompt_utf8_bytes": fixed_prompt_bytes,
                "dynamic_payload_utf8_bytes": dynamic_payload_bytes,
                "routing_summary_utf8_bytes": summary_bytes,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


def _configured_summary_agents() -> dict[str, Any]:
    return {
        "time": _SummaryAgent(TIME_ROUTING_SUMMARY),
        "wikipedia": _SummaryAgent(WIKIPEDIA_ROUTING_SUMMARY),
        "weather": _SummaryAgent(WEATHER_ROUTING_SUMMARY),
        "home_assistant": _SummaryAgent(HOME_ASSISTANT_ROUTING_SUMMARY),
        "media_player": _SummaryAgent(MEDIA_PLAYER_ROUTING_SUMMARY),
        "system_status": _SummaryAgent(SYSTEM_STATUS_ROUTING_SUMMARY),
    }


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("Ollama response must be a JSON object")
    return result


def _required_int(response: dict[str, Any], key: str) -> int:
    value = response.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"Ollama response is missing integer {key}")
    return value


if __name__ == "__main__":
    main()
