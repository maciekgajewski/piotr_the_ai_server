import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from ai_server.agent_loop import AgentReply
from ai_server.domain_agents.system_status import (
    SystemStatusCollector,
    SystemStatusDomainAgent,
    SystemStatusOptions,
    SystemStatusStore,
)
from ai_server.interfaces import Conversation
from ai_server.orchestrator.known_utterances import collect_known_utterance_tasks, known_utterance_task
from ai_server.utils import JsonFileStore


def test_system_status_store_persists_baselines(tmp_path):
    store = SystemStatusStore(JsonFileStore(tmp_path))

    assert store.load_baselines() == {}

    store.store_baselines({"load.load_1m_per_cpu": {"ema": 0.2, "samples": 12}})

    assert SystemStatusStore(JsonFileStore(tmp_path)).load_baselines() == {
        "load.load_1m_per_cpu": {"ema": 0.2, "samples": 12},
    }


def test_system_status_store_ignores_malformed_baseline_container(tmp_path):
    JsonFileStore(tmp_path).store("system_status.baselines", {"metrics": []})
    store = SystemStatusStore(JsonFileStore(tmp_path))

    assert store.load_baselines() == {}


def test_system_status_collector_green_snapshot(monkeypatch, tmp_path):
    _patch_local_metrics(monkeypatch, disk_free=90, inode_free=90, memory_available=80, swap_used=0, load_per_cpu=0.2)
    store = SystemStatusStore(JsonFileStore(tmp_path))
    collector = SystemStatusCollector(store=store, options=SystemStatusOptions(temperature_critical_c=10_000, temperature_warning_c=10_000))

    snapshot = asyncio.run(collector.collect_once())

    assert snapshot["status"] == "ok"
    assert snapshot["issues"] == []
    assert store.load_snapshot()["status"] == "ok"


def test_system_status_collector_logs_snapshot_summary(monkeypatch, tmp_path, caplog):
    _patch_local_metrics(monkeypatch, disk_free=90, inode_free=90, memory_available=80, swap_used=0, load_per_cpu=0.2)
    collector = SystemStatusCollector(
        store=SystemStatusStore(JsonFileStore(tmp_path)),
        options=SystemStatusOptions(temperature_critical_c=10_000, temperature_warning_c=10_000),
    )

    with caplog.at_level(logging.DEBUG, logger="ai_server.domain_agents.system_status"):
        asyncio.run(collector.collect_once())

    assert "collected system status status=ok issues=0 warnings=0 critical=0" in caplog.text


def test_system_status_collector_flags_thresholds(monkeypatch, tmp_path):
    _patch_local_metrics(monkeypatch, disk_free=2, inode_free=2, memory_available=3, swap_used=90, load_per_cpu=3.0)
    collector = SystemStatusCollector(store=SystemStatusStore(JsonFileStore(tmp_path)), options=SystemStatusOptions())

    snapshot = asyncio.run(collector.collect_once())

    assert snapshot["status"] == "critical"
    assert {issue["code"] for issue in snapshot["issues"]} >= {"disk_space_low", "disk_inodes_low", "memory_low", "swap_high", "load_high"}


def test_system_status_collector_flags_baseline_deviation(monkeypatch, tmp_path):
    _patch_local_metrics(monkeypatch, disk_free=90, inode_free=90, memory_available=80, swap_used=0, load_per_cpu=0.5)
    store = SystemStatusStore(JsonFileStore(tmp_path))
    store.store_baselines({"load.load_1m_per_cpu": {"ema": 0.1, "samples": 10, "direction": "higher_bad"}})
    collector = SystemStatusCollector(
        store=store,
        options=SystemStatusOptions(load_per_cpu_warning=99, baseline_min_samples=10, baseline_deviation_ratio=1.0),
    )

    snapshot = asyncio.run(collector.collect_once())

    assert "baseline_deviation" in {issue["code"] for issue in snapshot["issues"]}


def test_system_status_collector_uses_ha_allowlist(monkeypatch, tmp_path):
    _patch_local_metrics(monkeypatch, disk_free=90, inode_free=90, memory_available=80, swap_used=0, load_per_cpu=0.2)
    ha = FakeHomeAssistant({"sensor.piotr_health": {"entity_id": "sensor.piotr_health", "state": "unavailable", "attributes": {}}})
    collector = SystemStatusCollector(
        store=SystemStatusStore(JsonFileStore(tmp_path)),
        options=SystemStatusOptions(home_assistant_entities=("sensor.piotr_health", "sensor.missing")),
        home_assistant=ha,
    )

    snapshot = asyncio.run(collector.collect_once())

    assert {issue["code"] for issue in snapshot["issues"]} >= {"ha_entity_unavailable", "ha_entity_missing"}
    assert snapshot["metrics"]["home_assistant"]["configured_entities"] == ["sensor.piotr_health", "sensor.missing"]


def test_system_status_domain_agent_known_utterances_route_to_status(tmp_path):
    agent = SystemStatusDomainAgent(
        model="qwen3:4b",
        collector=FakeCollector(_green_snapshot()),
        ollama_connection=FakeOllamaConnection(),
        auto_start=False,
    )

    task = known_utterance_task("Jak się masz?", collect_known_utterance_tasks({"system_status": agent}))

    assert task["domain"] == "system_status"
    assert task["command"] == {"intent": "quick_check", "query": "Jak się masz?"}


def test_system_status_domain_agent_refuses_anonymous_user(tmp_path):
    loop_factory = FakeLoopFactory("{}")
    collector = FakeCollector(_green_snapshot())
    agent = SystemStatusDomainAgent(
        model="qwen3:4b",
        collector=collector,
        loop_factory=loop_factory.factory,
        ollama_connection=FakeOllamaConnection(),
        auto_start=True,
    )

    result = asyncio.run(agent.run_task(Conversation(conversation_id="c1", attributes={}), _task("quick_check"), {}))

    assert result == {
        "status": "failed",
        "text": "Nie mogę sprawdzić statusu systemu bez rozpoznanego użytkownika.",
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [],
        "final_reply_mode": "verbatim",
        "health_status": "unknown",
        "issue_count": 0,
        "snapshot_collected_at": None,
    }
    assert not collector.started
    assert loop_factory.loop is None


def test_system_status_domain_agent_calls_llm_for_green_reply(tmp_path):
    loop_factory = FakeLoopFactory(
        json.dumps(
            {
                "status": "ok",
                "text": "Wszystko działa dobrze, Kapitanie.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": [],
            },
            ensure_ascii=False,
        )
    )
    agent = SystemStatusDomainAgent(
        model="qwen3:4b",
        collector=FakeCollector(_green_snapshot()),
        loop_factory=loop_factory.factory,
        ollama_connection=FakeOllamaConnection(),
        auto_start=False,
    )
    conversation = Conversation(conversation_id="c1", attributes={"user": "Krzysztof"})

    result = asyncio.run(agent.run_task(conversation, _task("quick_check"), {}))

    assert result["text"] == "Wszystko działa dobrze, Kapitanie."
    assert result["final_reply_mode"] == "verbatim"
    payload = json.loads(loop_factory.loop.user_message)
    assert payload["conversation"]["user"] == "Krzysztof"
    assert payload["health_status"] == "ok"


def test_system_status_domain_agent_uses_fallback_model_for_ok_status(tmp_path):
    loop_factory = FakeLoopFactory(
        json.dumps(
            {
                "status": "ok",
                "text": "Wszystko działa dobrze.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": [],
            },
            ensure_ascii=False,
        )
    )
    agent = SystemStatusDomainAgent(
        model="large",
        fallback_model="small",
        collector=FakeCollector(_green_snapshot()),
        loop_factory=loop_factory.factory,
        ollama_connection=FakeOllamaConnection(),
        auto_start=False,
    )

    asyncio.run(agent.run_task(Conversation(conversation_id="c1", attributes={"user": "Krzysztof"}), _task("quick_check"), {}))

    assert loop_factory.config.model == "small"
    assert loop_factory.config.fallback_model == "large"


def test_system_status_domain_agent_uses_main_model_for_warning_status(tmp_path):
    loop_factory = FakeLoopFactory(
        json.dumps(
            {
                "status": "ok",
                "text": "Widzę ostrzeżenie.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": [],
            },
            ensure_ascii=False,
        )
    )
    snapshot = _green_snapshot()
    snapshot["status"] = "warning"
    snapshot["issues"] = [{"severity": "warning", "code": "load_high", "message": "problem", "details": {}}]
    agent = SystemStatusDomainAgent(
        model="large",
        fallback_model="small",
        collector=FakeCollector(snapshot),
        loop_factory=loop_factory.factory,
        ollama_connection=FakeOllamaConnection(),
        auto_start=False,
    )

    asyncio.run(agent.run_task(Conversation(conversation_id="c1", attributes={"user": "Krzysztof"}), _task("summary"), {}))

    assert loop_factory.config.model == "large"
    assert loop_factory.config.fallback_model == "small"


def test_system_status_domain_agent_logs_task_and_result(tmp_path, caplog):
    loop_factory = FakeLoopFactory(
        json.dumps(
            {
                "status": "ok",
                "text": "Wszystko działa dobrze.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": [],
            },
            ensure_ascii=False,
        ),
        prompt_eval_count=123,
        eval_count=17,
        duration_ms=456,
    )
    agent = SystemStatusDomainAgent(
        model="qwen3:4b",
        collector=FakeCollector(_green_snapshot()),
        loop_factory=loop_factory.factory,
        ollama_connection=FakeOllamaConnection(),
        auto_start=False,
    )

    with caplog.at_level(logging.INFO, logger="ai_server.domain_agents.system_status"):
        asyncio.run(agent.run_task(Conversation(conversation_id="c1", attributes={"user": "Krzysztof"}), _task("quick_check"), {}))

    assert "running system status task conversation_id=c1 task_id=t1 intent=quick_check snapshot_status=ok issue_count=0" in caplog.text
    assert "system status DSA LLM request conversation_id=c1 task_id=t1 model=qwen3:4b fallback_model=None intent=quick_check" in caplog.text
    assert (
        "system status DSA LLM reply conversation_id=c1 task_id=t1 model=qwen3:4b end_conversation=False "
        "reply_len=129 prompt_tokens=123 completion_tokens=17 total_tokens=140 duration_ms=456"
    ) in caplog.text
    assert "system status task result conversation_id=c1 task_id=t1 result_status=ok health_status=ok issue_count=0" in caplog.text


def test_system_status_domain_agent_mentions_many_issue_offer_context(tmp_path):
    loop_factory = FakeLoopFactory(
        json.dumps(
            {
                "status": "ok",
                "text": "Są cztery problemy. Mogę przygotować dłuższy raport.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": [],
            },
            ensure_ascii=False,
        )
    )
    snapshot = _green_snapshot()
    snapshot["status"] = "warning"
    snapshot["issues"] = [{"severity": "warning", "code": f"issue_{index}", "message": "problem", "details": {}} for index in range(4)]
    agent = SystemStatusDomainAgent(
        model="qwen3:4b",
        collector=FakeCollector(snapshot),
        max_short_report_issues=3,
        loop_factory=loop_factory.factory,
        ollama_connection=FakeOllamaConnection(),
        auto_start=False,
    )

    result = asyncio.run(agent.run_task(Conversation(conversation_id="c1", attributes={"user": "Krzysztof"}), _task("summary"), {}))

    assert "dłuższy raport" in result["text"]
    payload = json.loads(loop_factory.loop.user_message)
    assert payload["issue_count"] == 4
    assert payload["max_short_report_issues"] == 3


def test_system_status_domain_agent_falls_back_on_llm_failure(tmp_path):
    agent = SystemStatusDomainAgent(
        model="qwen3:4b",
        collector=FakeCollector(_green_snapshot()),
        loop_factory=FakeLoopFactory("to nie jest json").factory,
        ollama_connection=FakeOllamaConnection(),
        auto_start=False,
    )

    result = asyncio.run(agent.run_task(Conversation(conversation_id="c1", attributes={"user": "Krzysztof"}), _task("quick_check"), {}))

    assert result["status"] == "ok"
    assert result["health_status"] == "ok"
    assert "nie mogę teraz" in result["text"]


def _patch_local_metrics(monkeypatch, *, disk_free: float, inode_free: float, memory_available: float, swap_used: float, load_per_cpu: float) -> None:
    import ai_server.domain_agents.system_status.collector as collector_module

    total = 1000
    monkeypatch.setattr(collector_module.shutil, "disk_usage", lambda path: SimpleNamespace(total=total, used=total - disk_free * 10, free=disk_free * 10))
    monkeypatch.setattr(collector_module.os, "statvfs", lambda path: SimpleNamespace(f_files=total, f_ffree=inode_free * 10))
    monkeypatch.setattr(collector_module.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(collector_module.os, "getloadavg", lambda: (load_per_cpu * 4, load_per_cpu * 4, load_per_cpu * 4))
    monkeypatch.setattr(
        collector_module,
        "_read_meminfo",
        lambda: {
            "MemTotal": total,
            "MemAvailable": int(memory_available * 10),
            "SwapTotal": total,
            "SwapFree": int((100 - swap_used) * 10),
        },
    )


def _green_snapshot():
    return {
        "status": "ok",
        "collected_at_epoch": 1000.0,
        "collected_at": "2026-06-24T00:00:00+00:00",
        "metrics": {"load": {"load_1m_per_cpu": 0.2}},
        "issues": [],
    }


def _task(intent: str):
    return {
        "id": "t1",
        "domain": "system_status",
        "command": {"intent": intent, "query": "Jak się masz?"},
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }


class FakeHomeAssistant:
    inventory = object()

    def __init__(self, states):
        self._states = states

    def cached_state(self, entity_id):
        return self._states.get(entity_id)


class FakeCollector:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    def latest_snapshot(self):
        return self._snapshot

    def snapshot_is_stale(self, snapshot=None):
        return False

    async def collect_once(self):
        return self._snapshot


class FakeLoopFactory:
    def __init__(
        self,
        reply_text: str,
        *,
        prompt_eval_count: int | None = None,
        eval_count: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self._reply_text = reply_text
        self._prompt_eval_count = prompt_eval_count
        self._eval_count = eval_count
        self._duration_ms = duration_ms
        self.loop = None
        self.config = None

    def factory(self, **kwargs):
        self.config = kwargs["config"]
        self.loop = FakeLoop(
            self._reply_text,
            prompt_eval_count=self._prompt_eval_count,
            eval_count=self._eval_count,
            duration_ms=self._duration_ms,
        )
        return self.loop


class FakeLoop:
    def __init__(
        self,
        reply_text: str,
        *,
        prompt_eval_count: int | None = None,
        eval_count: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        self._reply_text = reply_text
        self._prompt_eval_count = prompt_eval_count
        self._eval_count = eval_count
        self._duration_ms = duration_ms
        self.user_message = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def send_user_message(self, message: str) -> AgentReply:
        self.user_message = message
        return AgentReply(
            reply_text=self._reply_text,
            end_conversation=False,
            prompt_eval_count=self._prompt_eval_count,
            eval_count=self._eval_count,
            duration_ms=self._duration_ms,
        )


class FakeOllamaConnection:
    async def close(self) -> None:
        pass
