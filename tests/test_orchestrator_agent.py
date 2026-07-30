import asyncio
import json
import logging
from pathlib import Path

import pytest

from ai_server.config import ServerConfig
from ai_server.domain_agents.system_status.agent import (
    KNOWN_UTTERANCES as SYSTEM_STATUS_KNOWN_UTTERANCES,
)
from ai_server.domain_agents.system_status.agent import (
    ROUTING_SUMMARY as SYSTEM_STATUS_ROUTING_SUMMARY,
)
from ai_server.orchestrator.orchestrator import (
    GENERATION_FAILURE_MESSAGE,
    MAX_FIXED_PLANNING_PROMPT_BYTES,
    MAX_ROUTING_SUMMARY_BYTES,
    OrchestratorAgent,
    _build_planning_system_prompt,
    _parse_plan,
)
from conftest import FakeAgentChannel, TextMessage, agent_context, run_agent, text_message_to_events
from tools.lib.t007_route_prompt_measurement import _configured_summary_agents


DOMAINS = frozenset(
    {"time", "wikipedia", "weather", "home_assistant", "media_player", "system_status"}
)


def _task(
    domain: str,
    request: str,
    *,
    task_id: str = "t1",
    depends_on: list[str] | None = None,
) -> dict:
    return {
        "id": task_id,
        "domain": domain,
        "request": request,
        "depends_on": depends_on or [],
    }


def _plan(
    tasks: list[dict],
    *,
    kind: str = "single_task",
    confidence: float = 0.9,
    active_domain: str | None = None,
    needs_clarification: bool = False,
    clarification_question: str | None = None,
) -> dict:
    return {
        "kind": kind,
        "confidence": confidence,
        "tasks": tasks,
        "context_updates": {
            "salient_entities": [],
            "active_domain": active_domain,
        },
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
    }


def test_parse_plan_accepts_generic_route_tasks() -> None:
    raw = _plan(
        [
            _task("home_assistant", "Włącz światło w biurze."),
            _task(
                "media_player",
                "Potem puść muzykę w kuchni.",
                task_id="t2",
                depends_on=["t1"],
            ),
        ],
        kind="multi_task",
        active_domain="media_player",
    )

    assert _parse_plan(json.dumps(raw, ensure_ascii=False), DOMAINS) == raw


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda plan: plan.update(extra=True), "fields must be exactly"),
        (lambda plan: plan["tasks"][0].update(command={}), "fields must be exactly"),
        (lambda plan: plan["tasks"][0].update(tool={}), "fields must be exactly"),
        (lambda plan: plan["tasks"][0].update(domain="unknown"), "available domain"),
        (lambda plan: plan["tasks"][0].update(request=""), "non-empty string"),
        (lambda plan: plan["tasks"].append(_task("time", "again")), "duplicated"),
        (lambda plan: plan["tasks"][0].update(depends_on=["missing"]), "earlier task"),
        (lambda plan: plan["tasks"][0].update(depends_on=["t1"]), "depend on itself"),
        (
            lambda plan: plan["tasks"].extend(
                [_task("time", "second", task_id="t2", depends_on=["t3"])]
            ),
            "earlier task",
        ),
        (lambda plan: plan["context_updates"].update(invented=True), "invented or missing"),
    ],
)
def test_parse_plan_rejects_non_generic_or_invalid_plans(mutate, error: str) -> None:
    raw = _plan([_task("time", "Która godzina?")], active_domain="time")
    mutate(raw)
    with pytest.raises(ValueError, match=error):
        _parse_plan(json.dumps(raw), DOMAINS)


def test_parse_plan_rejects_invalid_json() -> None:
    with pytest.raises(ValueError, match="must be valid JSON"):
        _parse_plan("not-json", DOMAINS)


def test_planning_prompt_is_sorted_and_bounded() -> None:
    agents = {
        domain: RecordingDomainAgent(routing_summary=f"Owns {domain} requests.")
        for domain in reversed(sorted(DOMAINS))
    }

    prompt, contributions = _build_planning_system_prompt(agents)

    assert len(prompt.encode("utf-8")) <= MAX_FIXED_PLANNING_PROMPT_BYTES
    assert list(contributions) == sorted(DOMAINS)
    assert prompt.index("- home_assistant:") < prompt.index("- media_player:")
    assert "command" not in "\n".join(
        line for line in prompt.splitlines() if line.startswith("- ")
    )


def test_routing_summary_does_not_embed_exact_known_utterance_examples() -> None:
    normalized_summary = SYSTEM_STATUS_ROUTING_SUMMARY.casefold()
    for utterance in SYSTEM_STATUS_KNOWN_UTTERANCES:
        assert utterance.casefold().strip(" ?.!") not in normalized_summary


def test_planning_prompt_rejects_oversized_summary() -> None:
    agent = RecordingDomainAgent(routing_summary="x" * (MAX_ROUTING_SUMMARY_BYTES + 1))
    with pytest.raises(ValueError, match="routing summary.*exceeds"):
        _build_planning_system_prompt({"time": agent})


def test_planning_prompt_rejects_oversized_combined_prompt(monkeypatch) -> None:
    monkeypatch.setattr(
        "ai_server.orchestrator.orchestrator.MAX_FIXED_PLANNING_PROMPT_BYTES",
        32,
    )
    with pytest.raises(ValueError, match="fixed route-only planning prompt exceeds"):
        _build_planning_system_prompt(
            {"time": RecordingDomainAgent(routing_summary="Time.")}
        )


def test_registration_order_does_not_change_prompt() -> None:
    forward = {
        "time": RecordingDomainAgent(routing_summary="Time."),
        "weather": RecordingDomainAgent(routing_summary="Weather."),
    }
    reverse = dict(reversed(list(forward.items())))
    assert _build_planning_system_prompt(forward) == _build_planning_system_prompt(reverse)


def test_checked_in_measurement_fixture_is_within_acceptance_bounds() -> None:
    fixture = json.loads(
        Path("tests/fixtures/t007-route-only-planning-request.json").read_text(
            encoding="utf-8"
        )
    )
    evidence = fixture["recorded_evidence"]
    current_prompt, _ = _build_planning_system_prompt(_configured_summary_agents())
    assert evidence["model"] == fixture["model"]
    assert evidence["fixed_prompt_utf8_bytes"] == len(current_prompt.encode("utf-8"))
    assert (
        evidence["fixed_prompt_utf8_bytes"]
        <= fixture["acceptance"]["max_fixed_prompt_utf8_bytes"]
    )
    assert evidence["prompt_tokens"] <= fixture["acceptance"]["max_input_tokens"]
    planning_payload = {
        "utterance": fixture["utterance"],
        "conversation": fixture["conversation"],
        "active_context": fixture["active_context"],
    }
    serialized = json.dumps(planning_payload, ensure_ascii=False)
    assert "excluded_home_assistant_inventory" not in serialized
    assert '"area_id"' not in serialized
    assert '"aliases"' not in serialized
    assert "kitchen" not in serialized


def test_planner_payload_contains_only_routing_context(caplog) -> None:
    ollama = FakeOllamaConnection(
        [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(_plan([], kind="chat")),
                },
                "prompt_eval_count": 123,
                "eval_count": 12,
            },
            "Cześć.",
        ]
    )
    agent = OrchestratorAgent(
        orchestrator_model="router",
        domain_agents={},
        ollama_connection=ollama,
        owns_ollama_connection=False,
        server_config=ServerConfig(location="Wrocław", timezone="Europe/Warsaw"),
    )
    context = agent_context(
        conversation_id="c1",
        attributes={
            "medium": "voice",
            "area": "office",
            "user": "Maciek",
            "user_settings": {
                "credentials": "secret",
                "voice_profile_path": "/private/profile",
                "media": {"default": "Liked Songs"},
            },
        },
    )

    with caplog.at_level(logging.DEBUG, logger="ai_server.orchestrator"):
        asyncio.run(
            run_agent(
                agent,
                context,
                FakeAgentChannel([TextMessage(text="hej")]),
            )
        )

    payload = json.loads(ollama.requests[0]["messages"][-1]["content"])
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload == {
        "utterance": "hej",
        "conversation": {
            "user": "Maciek",
            "area": "office",
            "server_location": "Wrocław",
            "server_timezone": "Europe/Warsaw",
        },
        "active_context": {
            "last_turns": [],
            "salient_entities": [],
            "active_domain": None,
            "pending_tasks": [],
            "pending_clarification": None,
        },
    }
    for forbidden in (
        "credentials",
        "voice_profile",
        "Liked Songs",
        "home_assistant_areas",
        '"command"',
        '"tool"',
    ):
        assert forbidden not in serialized
    assert "prompt_tokens=123 completion_tokens=12 total_tokens=135" in caplog.text
    assert "fixed_prompt_bytes=" in caplog.text
    assert "dynamic_payload_bytes=" in caplog.text
    assert "routing_summary_bytes=" in caplog.text
    assert "duration_ms=" in caplog.text


def test_single_task_is_dispatched_as_user_text_and_verbatim_reply_is_fast() -> None:
    route = _plan(
        [_task("time", "Która godzina?")],
        active_domain="time",
    )
    ollama = FakeOllamaConnection([json.dumps(route)])
    domain_agent = RecordingDomainAgent(
        results=[
            {
                "status": "ok",
                "text": "Jest czternasta.",
                "entities": ["timezone.Europe/Warsaw"],
                "final_reply_mode": "verbatim",
            }
        ]
    )
    agent = OrchestratorAgent(
        orchestrator_model="router",
        domain_agents={"time": domain_agent},
        ollama_connection=ollama,
        owns_ollama_connection=False,
    )
    endpoint = FakeAgentChannel([TextMessage(text="Która jest godzina teraz?")])
    context = agent_context(conversation_id="c1", attributes={"medium": "voice"})

    asyncio.run(run_agent(agent, context, endpoint))

    assert domain_agent.tasks == [_task("time", "Która godzina?")]
    assert len(ollama.requests) == 1
    assert endpoint.sent == list(
        text_message_to_events(TextMessage(text="Jest czternasta."))
    )
    assert context.agent_state["orchestrator"]["salient_entities"] == [
        "timezone.Europe/Warsaw"
    ]


def test_known_utterance_bypasses_router_without_exposing_command() -> None:
    domain_agent = RecordingDomainAgent(
        known_utterances=("Która godzina?",),
        results=[
            {
                "status": "ok",
                "text": "Jest czternasta.",
                "final_reply_mode": "verbatim",
            }
        ],
    )
    ollama = FakeOllamaConnection([])
    agent = OrchestratorAgent(
        orchestrator_model="router",
        domain_agents={"time": domain_agent},
        ollama_connection=ollama,
        owns_ollama_connection=False,
    )

    asyncio.run(
        run_agent(
            agent,
            agent_context(conversation_id="c1", attributes={"medium": "voice"}),
            FakeAgentChannel([TextMessage(text="Która godzina?")]),
        )
    )

    assert ollama.requests == []
    assert domain_agent.tasks == [_task("time", "Która godzina?")]


def test_multi_task_dependency_order_and_partial_failure() -> None:
    route = _plan(
        [
            _task("home_assistant", "Włącz światło.", task_id="t1"),
            _task("media_player", "Puść muzykę.", task_id="t2"),
            _task(
                "weather",
                "Sprawdź pogodę.",
                task_id="t3",
                depends_on=["t1"],
            ),
        ],
        kind="multi_task",
    )
    ha = RecordingDomainAgent(results=[{"status": "failed", "text": "Nie udało się."}])
    media = RecordingDomainAgent(results=[{"status": "ok", "text": "Gra."}])
    weather = RecordingDomainAgent()
    ollama = FakeOllamaConnection([json.dumps(route), "Światło się nie udało, muzyka gra."])
    agent = OrchestratorAgent(
        orchestrator_model="router",
        domain_agents={
            "home_assistant": ha,
            "media_player": media,
            "weather": weather,
        },
        ollama_connection=ollama,
        owns_ollama_connection=False,
    )

    asyncio.run(
        run_agent(
            agent,
            agent_context(conversation_id="c1", attributes={"medium": "text"}),
            FakeAgentChannel([TextMessage(text="zrób trzy rzeczy")]),
        )
    )

    assert len(ha.tasks) == 1
    assert len(media.tasks) == 1
    assert weather.tasks == []
    final_payload = json.loads(ollama.requests[-1]["messages"][-1]["content"])
    assert final_payload["task_results"][2]["error"] == "missing_dependencies"


def test_low_confidence_route_retries_with_clarification_model() -> None:
    low = _plan([], kind="chat", confidence=0.2)
    high = _plan([], kind="chat", confidence=0.95)
    ollama = FakeOllamaConnection([json.dumps(low), json.dumps(high), "Odpowiedź."])
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        domain_agents={},
        ollama_connection=ollama,
        owns_ollama_connection=False,
    )

    asyncio.run(
        run_agent(
            agent,
            agent_context(conversation_id="c1", attributes={"medium": "text"}),
            FakeAgentChannel([TextMessage(text="hmm")]),
        )
    )

    assert [request["model"] for request in ollama.requests[:2]] == ["small", "big"]


def test_invalid_route_retries_with_clarification_model_and_logs_each_request(caplog) -> None:
    route = _plan([], kind="chat", confidence=0.95)
    ollama = FakeOllamaConnection(
        [
            {
                "message": {"role": "assistant", "content": "not-json"},
                "prompt_eval_count": 101,
                "eval_count": 7,
            },
            {
                "message": {"role": "assistant", "content": json.dumps(route)},
                "prompt_eval_count": 103,
                "eval_count": 9,
            },
            "Odpowiedź.",
        ]
    )
    agent = OrchestratorAgent(
        orchestrator_model="small",
        clarification_model="big",
        domain_agents={},
        ollama_connection=ollama,
        owns_ollama_connection=False,
    )

    caplog.set_level(logging.INFO)
    asyncio.run(
        run_agent(
            agent,
            agent_context(conversation_id="c1", attributes={"medium": "text"}),
            FakeAgentChannel([TextMessage(text="hmm")]),
        )
    )

    assert [request["model"] for request in ollama.requests[:2]] == ["small", "big"]
    assert (
        "planning output model=small outcome=invalid prompt_tokens=101 "
        "completion_tokens=7 total_tokens=108 duration_ms="
    ) in caplog.text
    assert (
        "planning output model=big outcome=ok kind=chat confidence=0.95 "
        "tasks=[] needs_clarification=False prompt_tokens=103 "
        "completion_tokens=9 total_tokens=112 duration_ms="
    ) in caplog.text


def test_dsa_clarification_resumes_same_dsa_without_router_and_passes_opaque_value() -> None:
    route = _plan(
        [_task("home_assistant", "Włącz światło.")],
        active_domain="home_assistant",
    )
    continuation = {
        "candidate_ids": ["light.office"],
        "selection": {"preferred": None},
        "step": 2,
    }
    dsa = RecordingDomainAgent(
        results=[
            {
                "status": "needs_clarification",
                "text": "Które światło?",
                "clarification_question": "Które światło?",
                "continuation": continuation,
            }
        ],
        resumed_result={
            "status": "ok",
            "text": "Włączyłem lampę.",
            "final_reply_mode": "verbatim",
        },
    )
    ollama = FakeOllamaConnection([json.dumps(route), "Które światło?"])
    endpoint = FakeAgentChannel(
        [TextMessage(text="włącz światło"), TextMessage(text="lampę biurkową")]
    )
    agent = OrchestratorAgent(
        orchestrator_model="router",
        domain_agents={"home_assistant": dsa},
        ollama_connection=ollama,
        owns_ollama_connection=False,
    )

    asyncio.run(
        run_agent(
            agent,
            agent_context(conversation_id="c1", attributes={"medium": "voice"}),
            endpoint,
        )
    )

    assert len(ollama.requests) == 2
    assert len(dsa.resume_calls) == 1
    assert dsa.resume_calls[0]["continuation"] == continuation
    assert dsa.resume_calls[0]["answer"] == "lampę biurkową"
    assert endpoint.control_events == ["request_follow_up", "end_conversation"]


def test_router_clarification_returns_to_router() -> None:
    ambiguous = _plan(
        [],
        kind="chat",
        confidence=0.5,
        needs_clarification=True,
        clarification_question="Czy chodzi o pogodę czy godzinę?",
    )
    resolved = _plan(
        [_task("weather", "Sprawdź pogodę.")],
        kind="clarification_answer",
        active_domain="weather",
    )
    weather = RecordingDomainAgent(
        results=[
            {
                "status": "ok",
                "text": "Jest słonecznie.",
                "final_reply_mode": "verbatim",
            }
        ]
    )
    ollama = FakeOllamaConnection(
        [json.dumps(ambiguous), "Czy chodzi o pogodę czy godzinę?", json.dumps(resolved)]
    )
    agent = OrchestratorAgent(
        orchestrator_model="router",
        domain_agents={"weather": weather},
        ollama_connection=ollama,
        owns_ollama_connection=False,
    )

    asyncio.run(
        run_agent(
            agent,
            agent_context(conversation_id="c1", attributes={"medium": "voice"}),
            FakeAgentChannel(
                [TextMessage(text="sprawdź to"), TextMessage(text="pogodę")]
            ),
        )
    )

    assert len(ollama.requests) == 3
    second_route_payload = json.loads(ollama.requests[2]["messages"][-1]["content"])
    assert "Odpowiedź użytkownika: pogodę" in second_route_payload["utterance"]
    assert weather.tasks == [_task("weather", "Sprawdź pogodę.")]


def test_planning_failure_logs_metrics_and_ends_with_existing_failure_reply(caplog) -> None:
    ollama = FakeOllamaConnection([RuntimeError("down")])
    agent = OrchestratorAgent(
        orchestrator_model="router",
        domain_agents={},
        ollama_connection=ollama,
        owns_ollama_connection=False,
    )
    endpoint = FakeAgentChannel([TextMessage(text="hej")])

    caplog.set_level(logging.INFO)
    asyncio.run(
        run_agent(
            agent,
            agent_context(conversation_id="c1", attributes={"medium": "voice"}),
            endpoint,
        )
    )

    assert endpoint.sent == list(
        text_message_to_events(TextMessage(text=GENERATION_FAILURE_MESSAGE))
    )
    assert endpoint.control_events == ["end_conversation"]
    assert (
        "planning output model=router outcome=request_failed prompt_tokens=None "
        "completion_tokens=None total_tokens=None duration_ms="
    ) in caplog.text


class FakeOllamaConnection:
    def __init__(self, contents: list[str | dict | Exception]) -> None:
        self._contents = list(contents)
        self.requests: list[dict] = []

    async def chat(self, payload: dict) -> dict:
        self.requests.append(payload)
        content = self._contents.pop(0)
        if isinstance(content, Exception):
            raise content
        if isinstance(content, dict):
            return content
        return {"message": {"role": "assistant", "content": content}}

    async def close(self) -> None:
        pass


class RecordingDomainAgent:
    def __init__(
        self,
        results: list[dict] | None = None,
        *,
        resumed_result: dict | None = None,
        known_utterances: tuple[str, ...] = (),
        routing_summary: str = "Test routing ownership.",
    ) -> None:
        self.tasks: list[dict] = []
        self.resume_calls: list[dict] = []
        self._results = list(results or [{"status": "ok", "text": "Gotowe."}])
        self._resumed_result = resumed_result or {
            "status": "ok",
            "text": "Gotowe.",
        }
        self._known_utterances = known_utterances
        self._routing_summary = routing_summary

    def known_utterances(self) -> tuple[str, ...]:
        return self._known_utterances

    def routing_summary(self) -> str:
        return self._routing_summary

    async def run_task(self, conversation, task, active_context) -> dict:
        self.tasks.append(task)
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]

    async def resume_clarification(
        self,
        conversation,
        task,
        question,
        answer,
        active_context,
        continuation,
    ) -> dict:
        self.resume_calls.append(
            {
                "task": task,
                "question": question,
                "answer": answer,
                "active_context": active_context,
                "continuation": continuation,
            }
        )
        return self._resumed_result

    async def close(self) -> None:
        pass
