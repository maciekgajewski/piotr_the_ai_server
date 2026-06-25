from __future__ import annotations

import json
import logging
from typing import Any, Callable

from ai_server.agent_loop import AgentCallableSet, AgentLoop, AgentLoopConfig, AgentLoopOllamaConnection
from ai_server.domain_agents.interfaces import DomainTask
from ai_server.domain_agents.planning_prompts import planning_prompt_for_domain
from ai_server.domain_agents.system_status.collector import SystemStatusCollector
from ai_server.interfaces import Conversation
from ai_server.ollama_client import OLLAMA_BASE_URL


SYSTEM_STATUS_SYSTEM_PROMPT = """
You are a system-status domain-specific agent for a Polish voice assistant.
You receive one structured task and a cached health snapshot.
Use only the supplied snapshot and issues. Do not invent metrics, failures, causes, or fixes.
Write naturally in Polish. Address the user by name when it fits, including correct vocative if you can infer it.
For casual green check-ins, keep the response warm and short.
For warnings or critical issues, produce a short report. If many issues are present, mention only the most important ones and offer a longer report.
For full_report intent, include more detail but stay concise enough for voice.

Return only compact valid JSON with this shape:
{
  "status": "ok|failed",
  "text": "Polish user-facing reply",
  "needs_clarification": false,
  "clarification_question": null,
  "entities": [],
  "final_reply_mode": "verbatim"
}
"""


def _task(intent: str, query: str) -> DomainTask:
    return {
        "id": "t1",
        "domain": "system_status",
        "command": {"intent": intent, "query": query},
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    }


KNOWN_UTTERANCES: dict[str, DomainTask] = {
    "Jak się masz?": _task("quick_check", "Jak się masz?"),
    "Co u ciebie?": _task("quick_check", "Co u ciebie?"),
    "Jak tam?": _task("quick_check", "Jak tam?"),
    "Status systemu": _task("summary", "Status systemu"),
    "Czy wszystko działa?": _task("summary", "Czy wszystko działa?"),
    "Daj pełny raport": _task("full_report", "Daj pełny raport"),
    "Pełny raport systemu": _task("full_report", "Pełny raport systemu"),
}


class SystemStatusDomainAgent:
    def __init__(
        self,
        *,
        model: str,
        collector: SystemStatusCollector,
        ollama_url: str = OLLAMA_BASE_URL,
        fallback_model: str | None = None,
        fallback_backoff_seconds: float = 300.0,
        ollama_connection: AgentLoopOllamaConnection | None = None,
        loop_factory: Callable[..., AgentLoop] = AgentLoop,
        processing_update_interval_seconds: float = 5.0,
        max_short_report_issues: int = 3,
        auto_start: bool = True,
    ) -> None:
        self._model = model
        self._ollama_url = ollama_url
        self._fallback_model = fallback_model
        self._fallback_backoff_seconds = fallback_backoff_seconds
        self._collector = collector
        self._ollama_connection = ollama_connection or AgentLoopOllamaConnection(base_url=ollama_url)
        self._owns_ollama_connection = ollama_connection is None
        self._loop_factory = loop_factory
        self._processing_update_interval_seconds = processing_update_interval_seconds
        self._max_short_report_issues = max_short_report_issues
        self._started = False
        self._auto_start = auto_start
        self._logger = logging.getLogger(f"{__name__}.SystemStatusDomainAgent[{model}]")

    async def ensure_started(self) -> None:
        if self._auto_start and not self._started:
            await self._collector.start()
            self._started = True

    def known_utterances(self) -> dict[str, DomainTask]:
        return KNOWN_UTTERANCES

    def planning_prompt(self) -> str:
        return planning_prompt_for_domain("system_status")

    async def run_task(
        self,
        conversation: Conversation,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        await self.ensure_started()
        snapshot = self._collector.latest_snapshot()
        if self._collector.snapshot_is_stale(snapshot):
            snapshot = await self._collector.collect_once()

        command = task.get("command", {})
        command = command if isinstance(command, dict) else {}
        intent = command.get("intent") if isinstance(command.get("intent"), str) else "summary"
        issue_count = len(snapshot.get("issues", [])) if isinstance(snapshot.get("issues"), list) else 0
        payload = {
            "task": task,
            "active_context": active_context,
            "intent": intent,
            "health_status": snapshot.get("status", "unknown"),
            "issue_count": issue_count,
            "max_short_report_issues": self._max_short_report_issues,
            "snapshot": snapshot,
            "conversation": {
                "user": conversation.user,
                "area": conversation.area,
                "user_settings": conversation.user_settings,
            },
        }
        loop_config = AgentLoopConfig(
            model=self._model,
            ollama_url=self._ollama_url,
            fallback_model=self._fallback_model,
            fallback_backoff_seconds=self._fallback_backoff_seconds,
            options={"num_predict": 384, "temperature": 0, "num_ctx": 4096},
            keep_alive="1h",
        )
        self._logger.debug("running System Status DSA task=%s status=%s issue_count=%s", task, snapshot.get("status"), issue_count)
        async with self._loop_factory(
            config=loop_config,
            system_prompt=SYSTEM_STATUS_SYSTEM_PROMPT,
            tools=AgentCallableSet(),
            ollama_connection=self._ollama_connection,
            processing_update_callback=conversation.processing_update_callback,
            processing_update_interval_seconds=self._processing_update_interval_seconds,
        ) as loop:
            reply = await loop.send_user_message(json.dumps(payload, ensure_ascii=False))
        if reply.end_conversation:
            return _fallback_result(snapshot)
        try:
            result = _parse_domain_reply(reply.reply_text)
        except ValueError:
            self._logger.debug("rejecting non-JSON System Status DSA reply=%r", reply.reply_text)
            return _fallback_result(snapshot)
        result.setdefault("health_status", snapshot.get("status", "unknown"))
        result.setdefault("issue_count", issue_count)
        result.setdefault("snapshot_collected_at", snapshot.get("collected_at"))
        result.setdefault("final_reply_mode", "verbatim")
        return result

    async def close(self) -> None:
        await self._collector.close()
        if self._owns_ollama_connection:
            await self._ollama_connection.close()


def _parse_domain_reply(content: str) -> dict[str, Any]:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("System Status DSA reply must be valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("System Status DSA reply must be an object")
    status = raw.get("status")
    if status not in {"ok", "failed"}:
        raise ValueError("System Status DSA reply status must be ok or failed")
    text = raw.get("text")
    if not isinstance(text, str) or not text:
        raise ValueError("System Status DSA reply text must be a non-empty string")
    needs_clarification = raw.get("needs_clarification", False)
    if not isinstance(needs_clarification, bool):
        raise ValueError("System Status DSA needs_clarification must be boolean")
    clarification_question = raw.get("clarification_question")
    if clarification_question is not None and not isinstance(clarification_question, str):
        raise ValueError("System Status DSA clarification_question must be string or null")
    entities = raw.get("entities", [])
    if not isinstance(entities, list) or any(not isinstance(entity, str) for entity in entities):
        raise ValueError("System Status DSA entities must be a list of strings")
    return {
        "status": status,
        "text": text,
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
        "entities": entities,
        "final_reply_mode": "verbatim",
    }


def _fallback_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    status = snapshot.get("status")
    if status == "ok":
        text = "Status systemu wygląda dobrze, ale nie mogę teraz ładnie sformatować odpowiedzi."
    elif status == "critical":
        text = "Widzę krytyczne problemy w statusie systemu, ale nie mogę teraz przygotować pełnego raportu."
    else:
        text = "Widzę ostrzeżenia w statusie systemu, ale nie mogę teraz przygotować pełnego raportu."
    issues = snapshot.get("issues", [])
    return {
        "status": "ok",
        "text": text,
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [],
        "final_reply_mode": "verbatim",
        "health_status": status or "unknown",
        "issue_count": len(issues) if isinstance(issues, list) else 0,
        "snapshot_collected_at": snapshot.get("collected_at"),
    }
