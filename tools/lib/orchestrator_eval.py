from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ai_server.agent.orchestrator import OrchestratorAgent
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import ConversationInputEvent, ConversationOutputEvent, MessageBegin, MessageEnd
from ai_server.messages import MessageFragment, TextMessage, text_message_to_events
from ai_server.ollama import OllamaClient


DEFAULT_SCENARIOS = Path("tools/lib/orchestrator-eval/scenarios.yaml")


@dataclass(frozen=True)
class ExpectedPlanningContext:
    message_index: int
    salient_entities: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scenario:
    name: str
    messages: tuple[str, ...]
    model_responses: tuple[dict[str, Any], ...] | None
    domain_results: dict[str, dict[str, dict[str, Any]]]
    expected_tasks: tuple[dict[str, Any], ...]
    expected_replies: tuple[str, ...]
    expected_planning_context: tuple[ExpectedPlanningContext, ...] = ()


@dataclass
class ScenarioResult:
    scenario: Scenario
    replies: list[str] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    task_results: list[dict[str, Any]] = field(default_factory=list)
    planning_contexts: list[dict[str, Any]] = field(default_factory=list)
    model_requests: list[dict[str, Any]] = field(default_factory=list)
    model_responses: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.failures


class RecordingOllamaClient:
    def __init__(
        self,
        *,
        base_url: str,
        responses: tuple[dict[str, Any], ...] | None,
    ) -> None:
        self._responses = list(responses) if responses is not None else None
        self._inner = None if responses is not None else OllamaClient(base_url=base_url)
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(payload))
        if self._responses is None:
            assert self._inner is not None
            response = await self._inner.chat(payload)
            self.responses.append(copy.deepcopy(response))
            return response

        if not self._responses:
            raise AssertionError("unexpected orchestrator model call")

        response = self._responses.pop(0)
        if "plan" in response:
            content = json.dumps(response["plan"], ensure_ascii=False)
        elif "final_reply" in response:
            content = response["final_reply"]
        else:
            raise AssertionError("model response must contain plan or final_reply")
        chat_response = {"message": {"role": "assistant", "content": content}}
        self.responses.append(copy.deepcopy(chat_response))
        return chat_response

    async def close(self) -> None:
        if self._inner is not None:
            await self._inner.close()


class MockDomainAgent:
    def __init__(self, domain: str, results: dict[str, dict[str, Any]], traces: list[dict[str, Any]]) -> None:
        self._domain = domain
        self._results = results
        self._used_result_keys: set[str] = set()
        self._traces = traces

    async def run_task(
        self,
        conversation: Conversation,
        task: dict[str, Any],
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        result = copy.deepcopy(self._result_for_task(task["id"]))
        self._traces.append({"task": copy.deepcopy(task), "result": copy.deepcopy(result)})
        return result

    async def close(self) -> None:
        pass

    def _result_for_task(self, task_id: str) -> dict[str, Any]:
        if task_id in self._results and task_id not in self._used_result_keys:
            self._used_result_keys.add(task_id)
            return self._results[task_id]
        for result_key, result in self._results.items():
            if result_key not in self._used_result_keys:
                self._used_result_keys.add(result_key)
                return result
        return {"status": "ok", "text": f"{self._domain} mocked"}


class FakeConversationEndpoint(ConversationEndpoint):
    def __init__(self, incoming: list[TextMessage]) -> None:
        self._incoming: list[ConversationInputEvent] = []
        for message in incoming:
            self._incoming.extend(text_message_to_events(message))
        self.sent: list[ConversationOutputEvent] = []

    async def receive(self) -> ConversationInputEvent:
        if not self._incoming:
            raise AssertionError("unexpected receive")
        return self._incoming.pop(0)

    async def send(self, event: ConversationOutputEvent) -> None:
        self.sent.append(event)

    async def messages(self) -> AsyncIterator[TextMessage]:
        while self._incoming:
            text_parts: list[str] = []
            while True:
                event = await self.receive()
                if isinstance(event, MessageBegin):
                    text_parts.clear()
                    continue
                if isinstance(event, MessageFragment):
                    text_parts.append(event.text)
                    continue
                if isinstance(event, MessageEnd):
                    yield TextMessage(text="".join(text_parts))
                    break
                raise AssertionError(f"unsupported test event: {type(event).__name__}")

    async def send_message(self, message: TextMessage) -> None:
        for event in text_message_to_events(message):
            await self.send(event)


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
    if args.mocked_only:
        scenarios = [scenario for scenario in scenarios if scenario.model_responses is not None]
    if args.live_only:
        scenarios = [scenario for scenario in scenarios if scenario.model_responses is None]

    defaults = config.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be a mapping")
    results = asyncio.run(_run_scenarios(scenarios, defaults=defaults))
    if args.transcript:
        _print_transcripts(results)
    _print_results(results, verbose=args.verbose)
    return 0 if all(result.passed for result in results) else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate orchestrator planning and final response synthesis.")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS, help="YAML scenario file.")
    parser.add_argument("--scenario", action="append", default=[], help="Run only scenarios with this exact name.")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit.")
    parser.add_argument("--mocked-only", action="store_true", help="Run only scenarios with canned model responses.")
    parser.add_argument("--live-only", action="store_true", help="Run only scenarios that call the configured Ollama model.")
    parser.add_argument(
        "--no-transcript",
        dest="transcript",
        action="store_false",
        help="Do not print user/orchestrator/DSA message flow.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print scenario failures and observed tasks.")
    parser.set_defaults(transcript=True)
    return parser.parse_args()


def _load_eval_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file)
    if not isinstance(config, dict):
        raise ValueError("scenario file must contain a YAML mapping")
    if config.get("domain") != "orchestrator":
        raise ValueError("scenario file domain must be orchestrator")
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
        messages = _required_string_tuple(raw_scenario, "messages", name)
        model_responses = raw_scenario.get("model_responses")
        if model_responses is not None and not isinstance(model_responses, list):
            raise ValueError(f"{name}.model_responses must be a list")
        domain_results = raw_scenario.get("domain_results", {})
        if not isinstance(domain_results, dict):
            raise ValueError(f"{name}.domain_results must be a mapping")
        expected_tasks = raw_scenario.get("expected_tasks", [])
        if not isinstance(expected_tasks, list):
            raise ValueError(f"{name}.expected_tasks must be a list")
        expected_replies = _required_string_tuple(raw_scenario, "expected_replies", name)
        expected_context = _parse_expected_planning_context(raw_scenario.get("expected_planning_context", []), name)
        scenarios.append(
            Scenario(
                name=name,
                messages=messages,
                model_responses=tuple(copy.deepcopy(model_responses)) if model_responses is not None else None,
                domain_results=copy.deepcopy(domain_results),
                expected_tasks=tuple(copy.deepcopy(expected_tasks)),
                expected_replies=expected_replies,
                expected_planning_context=expected_context,
            )
        )
    return scenarios


async def _run_scenarios(scenarios: list[Scenario], defaults: dict[str, Any] | None = None) -> list[ScenarioResult]:
    results = []
    for scenario in scenarios:
        results.append(await _run_scenario(scenario, defaults or {}))
    return results


async def _run_scenario(scenario: Scenario, defaults: dict[str, Any]) -> ScenarioResult:
    started_at = time.perf_counter()
    task_traces: list[dict[str, Any]] = []
    domain_agents = {
        domain: MockDomainAgent(domain, results, task_traces)
        for domain, results in scenario.domain_results.items()
        if isinstance(results, dict)
    }
    base_url = _str_or_default(defaults.get("ollama_url"), "http://127.0.0.1:11434")
    ollama = RecordingOllamaClient(base_url=base_url, responses=scenario.model_responses)
    agent = OrchestratorAgent(
        model=_str_or_default(defaults.get("model"), "qwen3:4b-instruct"),
        domain_agents=domain_agents,
        ollama_client=ollama,
        owns_ollama_client=True,
    )
    conversation = Conversation(conversation_id=f"eval-{scenario.name}", attributes={"location": "office"})
    endpoint = FakeConversationEndpoint([TextMessage(text=message) for message in scenario.messages])
    result = ScenarioResult(scenario=scenario)

    try:
        await agent.run_conversation(conversation, endpoint)
    finally:
        await agent.close()
    result.duration_seconds = time.perf_counter() - started_at
    result.replies = _sent_text_messages(endpoint.sent)
    result.model_requests = ollama.requests
    result.model_responses = ollama.responses
    result.task_results = task_traces
    requests = ollama.requests
    result.tasks = [trace["task"] for trace in task_traces] + _unsupported_tasks_from_final_requests(requests)
    result.planning_contexts = _planning_contexts(requests)
    _score_scenario(result)
    return result


def _score_scenario(result: ScenarioResult) -> None:
    if result.scenario.expected_replies and tuple(result.replies) != result.scenario.expected_replies:
        result.failures.append(f"expected replies {result.scenario.expected_replies!r}, got {tuple(result.replies)!r}")

    if len(result.tasks) < len(result.scenario.expected_tasks):
        result.failures.append(f"expected at least {len(result.scenario.expected_tasks)} task(s), got {len(result.tasks)}")
    for index, expected_task in enumerate(result.scenario.expected_tasks):
        if index >= len(result.tasks):
            break
        if not _partial_match(expected_task, result.tasks[index]):
            result.failures.append(f"task #{index + 1} mismatch expected={expected_task!r} actual={result.tasks[index]!r}")

    for expected_context in result.scenario.expected_planning_context:
        if expected_context.message_index >= len(result.planning_contexts):
            result.failures.append(f"missing planning context for message index {expected_context.message_index}")
            continue
        context = result.planning_contexts[expected_context.message_index]
        salient_entities = context.get("salient_entities", [])
        for expected_entity in expected_context.salient_entities:
            if expected_entity not in salient_entities:
                result.failures.append(
                    f"message index {expected_context.message_index} missing salient entity {expected_entity!r}"
                )


def _partial_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if set(expected) == {"$any"} and isinstance(expected["$any"], list):
            return any(_partial_match(candidate, actual) for candidate in expected["$any"])
        if not isinstance(actual, dict):
            return False
        for key, expected_value in expected.items():
            if key not in actual or not _partial_match(expected_value, actual[key]):
                return False
        return True
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) < len(expected):
            return False
        return all(_partial_match(expected_item, actual[index]) for index, expected_item in enumerate(expected))
    return expected == actual


def _sent_text_messages(events: list[Any]) -> list[str]:
    messages = []
    current: list[str] = []
    for event in events:
        event_type = type(event).__name__
        if event_type == "MessageBegin":
            current = []
        elif event_type == "MessageFragment":
            current.append(event.text)
        elif event_type == "MessageEnd":
            messages.append("".join(current))
    return messages


def _planning_contexts(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contexts = []
    for request in requests:
        if request.get("format") != "json":
            continue
        content = request["messages"][-1]["content"]
        payload = json.loads(content)
        contexts.append(payload["active_context"])
    return contexts


def _unsupported_tasks_from_final_requests(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks = []
    for request in requests:
        if request.get("format") == "json":
            continue
        content = request["messages"][-1]["content"]
        payload = json.loads(content)
        for task_result in payload.get("task_results", []):
            if task_result.get("status") == "unsupported_domain":
                tasks.append(
                    {
                        "id": task_result["task_id"],
                        "domain": task_result["domain"],
                    }
                )
    return tasks


def _parse_expected_planning_context(raw_contexts: Any, scenario_name: str) -> tuple[ExpectedPlanningContext, ...]:
    if not isinstance(raw_contexts, list):
        raise ValueError(f"{scenario_name}.expected_planning_context must be a list")
    contexts = []
    for raw_context in raw_contexts:
        if not isinstance(raw_context, dict):
            raise ValueError(f"{scenario_name}.expected_planning_context entries must be mappings")
        message_index = raw_context.get("message_index")
        if not isinstance(message_index, int) or isinstance(message_index, bool) or message_index < 0:
            raise ValueError(f"{scenario_name}.expected_planning_context.message_index must be a non-negative integer")
        salient_entities = raw_context.get("salient_entities", [])
        if not isinstance(salient_entities, list) or any(not isinstance(item, str) for item in salient_entities):
            raise ValueError(f"{scenario_name}.expected_planning_context.salient_entities must be a list of strings")
        contexts.append(ExpectedPlanningContext(message_index=message_index, salient_entities=tuple(salient_entities)))
    return tuple(contexts)


def _required_string(raw_mapping: dict[str, Any], key: str, context: str) -> str:
    value = raw_mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value


def _required_string_tuple(raw_mapping: dict[str, Any], key: str, context: str) -> tuple[str, ...]:
    value = raw_mapping.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{context}.{key} must be a list of non-empty strings")
    return tuple(value)


def _str_or_default(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value else default


def _print_transcripts(results: list[ScenarioResult]) -> None:
    for result in results:
        print(f"=== scenario: {result.scenario.name} ===")
        for message_index, user_message in enumerate(result.scenario.messages):
            print(f"> user: {user_message}")
            plan_request, plan_response = _model_turn(result, message_index * 2)
            final_request, final_response = _model_turn(result, message_index * 2 + 1)
            final_payload = _request_payload(final_request) if final_request is not None else {}
            if plan_request is not None:
                planning_payload = _request_payload(plan_request)
                active_context = planning_payload.get("active_context", {})
                print("< orchestrator context:")
                print(_format_json(active_context, indent=2))
            if plan_response is not None:
                raw_plan = _response_content_as_json(plan_response)
                effective_plan = final_payload.get("plan", raw_plan)
                if raw_plan != effective_plan:
                    print("< orchestrator raw plan:")
                    print(_format_json(raw_plan, indent=2))
                    print("< orchestrator effective plan:")
                    print(_format_json(effective_plan, indent=2))
                else:
                    print("< orchestrator plan:")
                    print(_format_json(effective_plan, indent=2))

            task_results = final_payload.get("task_results", [])
            if task_results:
                print("< DSA results:")
                print(_format_json(task_results, indent=2))
            elif final_request is not None:
                print("< DSA results: []")

            reply = result.replies[message_index] if message_index < len(result.replies) else _response_content(final_response)
            if reply:
                print(f"< orchestrator reply: {reply}")
        print()


def _model_turn(result: ScenarioResult, index: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    request = result.model_requests[index] if index < len(result.model_requests) else None
    response = result.model_responses[index] if index < len(result.model_responses) else None
    return request, response


def _request_payload(request: dict[str, Any] | None) -> dict[str, Any]:
    if request is None:
        return {}
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        return {}
    last_message = messages[-1]
    if not isinstance(last_message, dict):
        return {}
    content = last_message.get("content")
    if not isinstance(content, str):
        return {}
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _response_content(response: dict[str, Any] | None) -> str:
    if response is None:
        return ""
    message = response.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _response_content_as_json(response: dict[str, Any]) -> Any:
    content = _response_content(response)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return content


def _format_json(value: Any, *, indent: int) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=True)


def _print_results(results: list[ScenarioResult], *, verbose: bool) -> None:
    passed = sum(1 for result in results if result.passed)
    duration = sum(result.duration_seconds for result in results)
    print(f"orchestrator-eval: {passed}/{len(results)} scenarios passed in {duration:.2f}s")
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.scenario.name} duration={result.duration_seconds:.2f}s tasks={len(result.tasks)}")
        if verbose or not result.passed:
            for failure in result.failures:
                print(f"  - {failure}")
            if verbose:
                print(f"  replies={result.replies!r}")
                print(f"  tasks={result.tasks!r}")


if __name__ == "__main__":
    raise SystemExit(main())
