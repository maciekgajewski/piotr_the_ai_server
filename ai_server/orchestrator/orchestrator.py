from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Mapping

from aiohttp import ClientSession

from ai_server.conversations.agent_context import AgentExecutionContext
from ai_server.agent_loop.agent_callable_set import to_json_value
from ai_server.config import ServerConfig
from ai_server.conversations.agent_runtime import AgentChannel, ConversationAgent
from ai_server.conversations.contexts import ConversationContext, ConversationMedium
from ai_server.domain_agents import DomainAgent, DomainTask
from ai_server.ollama_connection import OLLAMA_BASE_URL, OllamaConnection, OllamaError
from ai_server.orchestrator.known_utterances import (
    KnownUtteranceDomains,
    collect_known_utterance_domains,
    known_utterance_task,
)
from ai_server.utils.conversation_style import reply_style_instruction, system_prompt_with_reply_style
from ai_server.utils.processing import ProcessingUpdateCallback, ProcessingUpdateThrottle, await_with_processing_updates
from ai_server.utils.text import normalize_text


ORCHESTRATOR_STATE_KEY = "orchestrator"
GENERATION_FAILURE_MESSAGE = "Przepraszam, nie mogę teraz odpowiedzieć."
PLANNING_OPTIONS = {
    "num_predict": 768,
    "temperature": 0,
    "num_ctx": 4096,
}
FINAL_REPLY_OPTIONS = {
    "num_predict": 192,
    "temperature": 0,
    "num_ctx": 4096,
}
MAX_LAST_TURNS = 4
PLANNING_CONFIDENCE_THRESHOLD = 0.7
MAX_ROUTING_SUMMARY_BYTES = 512
MAX_FIXED_PLANNING_PROMPT_BYTES = 4096

PLANNING_SYSTEM_PROMPT_TEMPLATE = """
You are a compact route-only planner for a Polish voice assistant.
Return only compact valid JSON. No markdown. No explanations.

Route and split the latest user utterance. Tasks carry user request text, never commands.
Use active_context as a hint, not a jail: route to a new domain when the utterance asks for one.
conversation.area is a local room; server_location is geographic. Never confuse them.

Available task domains: {available_domains}
Only create tasks for the available task domains. If no available domain fits the user utterance, return kind="chat" with tasks=[].

Return this schema:
{"kind":"single_task|multi_task|followup|clarification_answer|chat","confidence":0.0,"tasks":[{"id":"t1","domain":"available domain","request":"user task text","depends_on":[]}],"context_updates":{"salient_entities":[],"active_domain":null},"needs_clarification":false,"clarification_question":null}

Preserve the user's wording for one task. For a composite utterance, make faithful
self-contained clause splits without inventing parameters. Keep multiple clauses for
one domain in one task unless a real dependency requires separation. Dependencies may
reference only earlier task ids. Ask clarification only when the domain cannot be
selected or split; domain-specific missing details belong to the selected domain.

Domain routing summaries:
{routing_summaries}
"""

FINAL_REPLY_SYSTEM_PROMPT = """
You are the final response writer for a Polish voice assistant.
Write the final user-facing reply in Polish.
Use the task results and clarification state. Be concise.
Do not claim a task succeeded unless its JSON result says it succeeded.
When a task result has final_reply_mode="verbatim", preserve that result's text exactly when composing the reply.
If a task is unsupported, say that capability is not connected yet.
If clarification is needed, ask exactly the needed question, optionally after summarizing completed independent tasks.
"""

class OrchestratorAgent(ConversationAgent):
    def __init__(
        self,
        orchestrator_model: str,
        domain_agents: Mapping[str, DomainAgent] | None = None,
        clarification_model: str | None = None,
        base_url: str = OLLAMA_BASE_URL,
        session: ClientSession | None = None,
        ollama_connection: OllamaConnection | None = None,
        owns_ollama_connection: bool = True,
        server_config: ServerConfig = ServerConfig(),
        processing_update_interval_seconds: float = 5.0,
    ) -> None:
        self._orchestrator_model = orchestrator_model
        self._clarification_model = clarification_model
        self._domain_agents = dict(domain_agents or {})
        self._planning_system_prompt, self._routing_summary_bytes = _build_planning_system_prompt(self._domain_agents)
        self._known_utterance_domains = collect_known_utterance_domains(self._domain_agents)
        self._ollama = ollama_connection or OllamaConnection(base_url=base_url, session=session)
        self._owns_ollama = owns_ollama_connection
        self._server_config = server_config
        self._processing_update_interval_seconds = processing_update_interval_seconds
        self._logger = logging.getLogger(f"{__name__}.OrchestratorAgent[{orchestrator_model}]")

    async def preload(self) -> None:
        try:
            await self._ollama.chat(
                {
                    "model": self._orchestrator_model,
                    "think": False,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "1h",
                    "messages": [{"role": "user", "content": 'Return JSON: {"ok":true}'}],
                }
            )
        except Exception as exc:
            raise OllamaError(f"failed to preload Ollama model {self._orchestrator_model}") from exc

    async def run_agent_conversation(self, context: ConversationContext, channel: AgentChannel) -> None:
        processing_update_throttle = ProcessingUpdateThrottle(
            channel.processing_update,
            interval_seconds=self._processing_update_interval_seconds,
        )
        conversation = AgentExecutionContext(
            conversation=context,
            processing_update_callback=processing_update_throttle.emit,
            processing_update_interval_seconds=self._processing_update_interval_seconds,
        )
        await self._run_execution_context(conversation, channel, processing_update_throttle)

    async def _run_execution_context(
        self,
        conversation: AgentExecutionContext,
        channel: AgentChannel,
        processing_update_throttle: ProcessingUpdateThrottle | None = None,
    ) -> None:
        logger = logging.getLogger(f"{__name__}.OrchestratorAgent[{conversation.conversation_id}]")
        processing_update_throttle = processing_update_throttle or ProcessingUpdateThrottle(
            channel.processing_update,
            interval_seconds=self._processing_update_interval_seconds,
        )
        conversation.processing_update_callback = processing_update_throttle.emit
        conversation.processing_update_interval_seconds = self._processing_update_interval_seconds
        try:
            while True:
                try:
                    message = await channel.receive_user_message()
                except StopAsyncIteration:
                    return
                processing_update_throttle.reset()
                started_at = time.perf_counter()
                logger.info("received message text=%r", message.text)
                try:
                    reply_text = await self._handle_message(conversation, message.text)
                    logger.info("produced reply text=%r", reply_text)
                except Exception:
                    elapsed_ms = _elapsed_ms(started_at)
                    logger.exception("orchestration failed request_len=%s duration_ms=%s", len(message.text), elapsed_ms)
                    await channel.send_message(GENERATION_FAILURE_MESSAGE)
                    await channel.end_conversation()
                    return

                elapsed_ms = _elapsed_ms(started_at)
                logger.info(
                    "orchestration completed request_len=%s reply_len=%s duration_ms=%s",
                    len(message.text),
                    len(reply_text),
                    elapsed_ms,
                )
                await channel.send_message(reply_text)
                if _has_pending_clarification(conversation):
                    await channel.request_follow_up()
                    continue
                await channel.end_conversation()
                return
        finally:
            state = conversation.agent_state.get(ORCHESTRATOR_STATE_KEY)
            if isinstance(state, dict) and state.get("pending_clarification") is not None:
                logger.info("dropping unanswered clarification pending=%s", _compact_json(state.get("pending_clarification")))
                state["pending_clarification"] = None

    async def close(self) -> None:
        try:
            for domain_agent in self._domain_agents.values():
                await domain_agent.close()
        finally:
            if self._owns_ollama:
                await self._ollama.close()

    async def _handle_message(self, conversation: AgentExecutionContext, user_input: str) -> str:
        state = _orchestrator_state(conversation)
        active_context = _active_context(state)
        if state.get("pending_clarification") is not None:
            if _cancels_pending_clarification(user_input):
                self._logger.info(
                    "clearing pending clarification conversation_id=%s user_input=%r pending=%s",
                    conversation.conversation_id,
                    user_input,
                    _compact_json(state.get("pending_clarification")),
                )
                state["pending_clarification"] = None
                state["pending_tasks"] = []
                reply = ""
                _append_last_turn(state, user_input=user_input, assistant_reply=reply)
                return reply
            self._logger.info(
                "resuming pending clarification conversation_id=%s user_input=%r pending=%s",
                conversation.conversation_id,
                user_input,
                _compact_json(state.get("pending_clarification")),
            )
            reply = await self._handle_clarification_answer(
                conversation=conversation,
                user_input=user_input,
                state=state,
                active_context=active_context,
            )
            _append_last_turn(state, user_input=user_input, assistant_reply=reply)
            return reply

        plan = _short_path_plan(
            user_input,
            known_utterance_domains=self._known_utterance_domains,
            active_context=active_context,
        )
        if plan is None:
            plan = await self._plan_message(
                user_input=user_input,
                active_context=active_context,
                conversation=conversation,
            )
        else:
            self._logger.info(
                "short path matched conversation_id=%s normalized_utterance=%r tasks=%s",
                conversation.conversation_id,
                normalize_text(user_input),
                _tasks_summary(plan["tasks"]),
            )
        self._logger.info(
            "plan ready conversation_id=%s kind=%s confidence=%s tasks=%s",
            conversation.conversation_id,
            plan["kind"],
            plan["confidence"],
            _tasks_summary(plan["tasks"]),
        )
        task_results = await self._dispatch_ready_tasks(conversation, plan, active_context)
        self._log_clarification_causing_utterance(conversation, user_input, plan, task_results)
        _apply_context_updates(state, plan)
        _apply_task_context_updates(state, plan, task_results)
        _store_pending_tasks(state, plan)
        _store_pending_clarification(state, plan, task_results, request=user_input)

        reply = await self._final_reply(
            conversation=conversation,
            user_input=user_input,
            active_context=_active_context(state),
            plan=plan,
            task_results=task_results,
            processing_update_callback=conversation.processing_update_callback,
        )
        _append_last_turn(state, user_input=user_input, assistant_reply=reply)
        return reply

    async def _handle_clarification_answer(
        self,
        *,
        conversation: AgentExecutionContext,
        user_input: str,
        state: dict[str, Any],
        active_context: dict[str, Any],
    ) -> str:
        pending_clarification = state.get("pending_clarification")
        if not isinstance(pending_clarification, dict):
            state["pending_clarification"] = None
            raise ValueError("pending clarification must be an object")

        owner = pending_clarification.get("owner")
        if owner == "router":
            original_request = pending_clarification.get("request")
            question = pending_clarification.get("question")
            if not isinstance(original_request, str) or not isinstance(question, str):
                raise ValueError("router clarification state is invalid")
            plan = await self._plan_message(
                user_input=(
                    f"{original_request}\n"
                    f"Pytanie routera: {question}\n"
                    f"Odpowiedź użytkownika: {user_input}"
                ),
                active_context=active_context,
                conversation=conversation,
            )
            plan["kind"] = "clarification_answer"
            task_results = await self._dispatch_ready_tasks(conversation, plan, active_context)
        elif owner == "dsa":
            task = pending_clarification.get("task")
            question = pending_clarification.get("question")
            domain = task.get("domain") if isinstance(task, dict) else None
            domain_agent = self._domain_agents.get(domain) if isinstance(domain, str) else None
            if domain_agent is None or not isinstance(task, dict) or not isinstance(question, str):
                raise ValueError("DSA clarification state is invalid")
            self._logger.info(
                "resuming DSA clarification conversation_id=%s domain=%s task_id=%s",
                conversation.conversation_id,
                domain,
                task.get("id"),
            )
            result = await domain_agent.resume_clarification(
                conversation,
                task,
                question,
                user_input,
                active_context,
                pending_clarification.get("continuation"),
            )
            if not isinstance(result, dict):
                raise ValueError(f"domain agent {domain} returned non-object clarification result")
            task_result = {"task_id": task["id"], "domain": domain, **_domain_result_json(result)}
            plan = {
                "kind": "clarification_answer",
                "confidence": 1.0,
                "tasks": [task],
                "context_updates": {"salient_entities": [], "active_domain": domain},
                "needs_clarification": False,
                "clarification_question": None,
            }
            task_results = [task_result]
        else:
            raise ValueError("pending clarification owner is invalid")
        self._log_clarification_causing_utterance(conversation, user_input, plan, task_results)
        _apply_context_updates(state, plan)
        _apply_task_context_updates(state, plan, task_results)
        _store_pending_tasks(state, plan)
        _store_pending_clarification(state, plan, task_results, request=user_input)

        reply = await self._final_reply(
            conversation=conversation,
            user_input=user_input,
            active_context=_active_context(state),
            plan=plan,
            task_results=task_results,
            processing_update_callback=conversation.processing_update_callback,
        )
        return reply

    async def _plan_message(
        self,
        *,
        user_input: str,
        active_context: dict[str, Any],
        conversation: AgentExecutionContext,
    ) -> dict[str, Any]:
        conversation_payload = {
            "user": conversation.user,
            "area": conversation.area,
            "server_location": self._server_config.location,
            "server_timezone": self._server_config.timezone,
        }
        prompt = {
            "utterance": user_input,
            "conversation": {key: value for key, value in conversation_payload.items() if value is not None},
            "active_context": active_context,
        }
        self._logger.info(
            "planning message conversation_id=%s model=%s text=%r",
            conversation.conversation_id,
            self._orchestrator_model,
            user_input,
        )
        plan = await self._plan_message_with_model(
            self._orchestrator_model,
            prompt,
            conversation.processing_update_callback,
        )
        if (
            plan["confidence"] >= PLANNING_CONFIDENCE_THRESHOLD
            or self._clarification_model is None
            or self._clarification_model == self._orchestrator_model
        ):
            return plan

        self._logger.info(
            "planning confidence below threshold confidence=%s threshold=%s retry_model=%s",
            plan["confidence"],
            PLANNING_CONFIDENCE_THRESHOLD,
            self._clarification_model,
        )
        return await self._plan_message_with_model(
            self._clarification_model,
            prompt,
            conversation.processing_update_callback,
        )

    async def _plan_message_with_model(
        self,
        model: str,
        prompt: dict[str, Any],
        processing_update_callback: ProcessingUpdateCallback | None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        metrics_logged = False
        dynamic_payload = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
        fixed_prompt_bytes = len(self._planning_system_prompt.encode("utf-8"))
        dynamic_payload_bytes = len(dynamic_payload.encode("utf-8"))
        self._logger.debug(
            "planning prompt sizes model=%s fixed_prompt_bytes=%s dynamic_payload_bytes=%s routing_summary_bytes=%s",
            model,
            fixed_prompt_bytes,
            dynamic_payload_bytes,
            self._routing_summary_bytes,
        )
        try:
            self._logger.info("planning request model=%s utterance=%r", model, prompt.get("utterance"))
            response = await await_with_processing_updates(
                self._ollama.chat(
                    {
                        "model": model,
                        "raw": False,
                        "think": False,
                        "format": "json",
                        "stream": False,
                        "keep_alive": "1h",
                        "options": PLANNING_OPTIONS,
                        "messages": [
                            {"role": "system", "content": self._planning_system_prompt},
                            {"role": "user", "content": dynamic_payload},
                        ],
                    }
                ),
                callback=processing_update_callback,
                logger=self._logger,
                interval_seconds=self._processing_update_interval_seconds,
            )
            prompt_tokens, completion_tokens, total_tokens = _response_token_counts(response)
            duration_ms = _elapsed_ms(started_at)
            try:
                plan = _parse_plan(
                    _assistant_content(response),
                    frozenset(self._domain_agents),
                )
            except Exception:
                self._logger.info(
                    "planning output model=%s outcome=invalid prompt_tokens=%s completion_tokens=%s "
                    "total_tokens=%s duration_ms=%s",
                    model,
                    prompt_tokens,
                    completion_tokens,
                    total_tokens,
                    duration_ms,
                )
                metrics_logged = True
                raise
            self._logger.info(
                "planning output model=%s outcome=ok kind=%s confidence=%s tasks=%s needs_clarification=%s "
                "prompt_tokens=%s completion_tokens=%s total_tokens=%s duration_ms=%s",
                model,
                plan["kind"],
                plan["confidence"],
                _tasks_summary(plan["tasks"]),
                plan["needs_clarification"],
                prompt_tokens,
                completion_tokens,
                total_tokens,
                duration_ms,
            )
            metrics_logged = True
            return plan
        except Exception:
            if not metrics_logged:
                self._logger.info(
                    "planning output model=%s outcome=request_failed prompt_tokens=None "
                    "completion_tokens=None total_tokens=None duration_ms=%s",
                    model,
                    _elapsed_ms(started_at),
                )
            if model == self._orchestrator_model and self._clarification_model is not None and self._clarification_model != model:
                self._logger.warning("planning with orchestrator model failed, retrying clarification_model", exc_info=True)
                return await self._plan_message_with_model(self._clarification_model, prompt, processing_update_callback)
            raise

    async def _dispatch_ready_tasks(
        self,
        conversation: AgentExecutionContext,
        plan: dict[str, Any],
        active_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        completed_task_ids: set[str] = set()
        results = []
        for task in plan["tasks"]:
            missing_dependencies = [dependency for dependency in task["depends_on"] if dependency not in completed_task_ids]
            if missing_dependencies:
                result = {
                    "task_id": task["id"],
                    "domain": task["domain"],
                    "status": "blocked",
                    "error": "missing_dependencies",
                    "missing_dependencies": missing_dependencies,
                }
                self._logger.info("task missing dependencies task=%s result=%s", _task_summary(task), _result_summary(result))
                results.append(result)
                continue

            domain_agent = self._domain_agents.get(task["domain"])
            if domain_agent is None:
                result = {
                    "task_id": task["id"],
                    "domain": task["domain"],
                    "status": "unsupported_domain",
                    "message": f"Domain agent is not available: {task['domain']}",
                }
            else:
                self._logger.info("dispatching task task=%s", _task_summary(task))
                result = await domain_agent.run_task(conversation, task, active_context)
                if not isinstance(result, dict):
                    raise ValueError(f"domain agent {task['domain']} returned non-object result")
                result = {
                    "task_id": task["id"],
                    "domain": task["domain"],
                    **_domain_result_json(result),
                }

            self._logger.info("task result task_id=%s result=%s", task["id"], _result_summary(result))
            results.append(result)
            if result.get("status") not in {
                "blocked",
                "failed",
                "needs_clarification",
                "unsupported_domain",
            }:
                completed_task_ids.add(task["id"])
        return results

    def _log_clarification_causing_utterance(
        self,
        conversation: AgentExecutionContext,
        user_input: str,
        plan: dict[str, Any],
        task_results: list[dict[str, Any]],
    ) -> None:
        tasks_by_id = {
            task["id"]: task
            for task in plan["tasks"]
            if isinstance(task.get("id"), str)
        }
        logged = False
        for result in task_results:
            if result.get("status") != "needs_clarification":
                continue
            task = tasks_by_id.get(result.get("task_id"))
            self._logger.warning(
                "utterance caused clarification source=%s conversation_id=%s utterance=%r task=%s result=%s",
                "dsa",
                conversation.conversation_id,
                user_input,
                _task_summary(task) if task is not None else None,
                _result_summary(result),
            )
            logged = True

        if not logged and plan.get("needs_clarification"):
            self._logger.warning(
                "utterance caused clarification source=orchestrator conversation_id=%s utterance=%r plan_question=%r",
                conversation.conversation_id,
                user_input,
                plan.get("clarification_question"),
            )

    async def _final_reply(
        self,
        *,
        conversation: AgentExecutionContext,
        user_input: str,
        active_context: dict[str, Any],
        plan: dict[str, Any],
        task_results: list[dict[str, Any]],
        processing_update_callback: ProcessingUpdateCallback | None,
    ) -> str:
        prompt = {
            "utterance": user_input,
            "conversation": {
                "medium": conversation.medium.value,
                "reply_style": reply_style_instruction(conversation.medium),
            },
            "active_context": active_context,
            "plan": plan,
            "task_results": task_results,
        }
        self._logger.info(
            "final reply request model=%s utterance=%r task_results=%s",
            self._orchestrator_model,
            user_input,
            _results_summary(task_results),
        )
        verbatim_reply = _single_verbatim_reply(task_results)
        if verbatim_reply:
            self._logger.info("final reply using verbatim task result text=%r", verbatim_reply)
            return verbatim_reply

        reply = await self._final_reply_with_model(
            self._orchestrator_model,
            prompt,
            medium=conversation.medium,
            processing_update_callback=processing_update_callback,
        )
        if reply or self._clarification_model is None:
            return reply or GENERATION_FAILURE_MESSAGE

        self._logger.info("final reply from orchestrator model was empty, retrying clarification_model=%s", self._clarification_model)
        reply = await self._final_reply_with_model(
            self._clarification_model,
            prompt,
            medium=conversation.medium,
            processing_update_callback=processing_update_callback,
        )
        return reply or GENERATION_FAILURE_MESSAGE

    async def _final_reply_with_model(
        self,
        model: str,
        prompt: dict[str, Any],
        *,
        medium: ConversationMedium,
        processing_update_callback: ProcessingUpdateCallback | None,
    ) -> str:
        try:
            self._logger.info("final reply model request model=%s utterance=%r", model, prompt.get("utterance"))
            response = await await_with_processing_updates(
                self._ollama.chat(
                    {
                        "model": model,
                        "raw": False,
                        "think": False,
                        "stream": False,
                        "keep_alive": "1h",
                        "options": FINAL_REPLY_OPTIONS,
                        "messages": [
                            {"role": "system", "content": system_prompt_with_reply_style(FINAL_REPLY_SYSTEM_PROMPT, medium)},
                            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                        ],
                    }
                ),
                callback=processing_update_callback,
                logger=self._logger,
                interval_seconds=self._processing_update_interval_seconds,
            )
            reply = _assistant_content(response).strip()
            self._logger.info("final reply output model=%s text=%r", model, reply)
            return reply
        except Exception:
            if model == self._orchestrator_model and self._clarification_model is not None and self._clarification_model != model:
                self._logger.warning("final reply with orchestrator model failed, retrying clarification_model", exc_info=True)
                return await self._final_reply_with_model(
                    self._clarification_model,
                    prompt,
                    medium=medium,
                    processing_update_callback=processing_update_callback,
                )
            raise


def _elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def _short_path_plan(
    user_input: str,
    *,
    known_utterance_domains: KnownUtteranceDomains,
    active_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    del active_context
    task = known_utterance_task(user_input, known_utterance_domains)
    if task is None:
        return None
    return {
        "kind": "single_task",
        "confidence": 1.0,
        "tasks": [task],
        "context_updates": {"salient_entities": [], "active_domain": task["domain"]},
        "needs_clarification": False,
        "clarification_question": None,
    }


def _single_verbatim_reply(task_results: list[dict[str, Any]]) -> str | None:
    if len(task_results) != 1:
        return None
    result = task_results[0]
    if result.get("status") != "ok" or result.get("final_reply_mode") != "verbatim":
        return None
    text = result.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    return text or None


def _compact_json(value: Any, max_length: int = 500) -> str:
    text = json.dumps(to_json_value(value), ensure_ascii=False, sort_keys=True)
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."


def _task_summary(task: dict[str, Any]) -> str:
    return _compact_json(
        {
            "id": task.get("id"),
            "domain": task.get("domain"),
            "request": task.get("request"),
            "depends_on": task.get("depends_on"),
        }
    )


def _tasks_summary(tasks: list[dict[str, Any]]) -> str:
    return _compact_json([{"id": task.get("id"), "domain": task.get("domain")} for task in tasks])


def _build_planning_system_prompt(
    domain_agents: Mapping[str, DomainAgent],
) -> tuple[str, dict[str, int]]:
    summaries: list[str] = []
    summary_bytes: dict[str, int] = {}
    for domain in sorted(domain_agents):
        summary = domain_agents[domain].routing_summary().strip()
        if not summary:
            raise ValueError(f"routing summary for {domain} must be non-empty")
        size = len(summary.encode("utf-8"))
        if size > MAX_ROUTING_SUMMARY_BYTES:
            raise ValueError(
                f"routing summary for {domain} exceeds {MAX_ROUTING_SUMMARY_BYTES} UTF-8 bytes: {size}"
            )
        summary_bytes[domain] = size
        summaries.append(f"- {domain}: {summary}")

    available_domains = ", ".join(sorted(domain_agents)) if domain_agents else "none"
    prompt = (
        PLANNING_SYSTEM_PROMPT_TEMPLATE.replace("{available_domains}", available_domains)
        .replace("{routing_summaries}", "\n".join(summaries) if summaries else "- none")
        .strip()
    )
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > MAX_FIXED_PLANNING_PROMPT_BYTES:
        raise ValueError(
            f"fixed route-only planning prompt exceeds {MAX_FIXED_PLANNING_PROMPT_BYTES} UTF-8 bytes: "
            f"{prompt_bytes}; routing_summary_bytes={summary_bytes}"
        )
    return prompt, summary_bytes


def _response_token_counts(response: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    prompt_tokens = _optional_int(response.get("prompt_eval_count"))
    completion_tokens = _optional_int(response.get("eval_count"))
    total_tokens = prompt_tokens + completion_tokens if prompt_tokens is not None and completion_tokens is not None else None
    return prompt_tokens, completion_tokens, total_tokens


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _result_summary(result: dict[str, Any]) -> str:
    return _compact_json(
        {
            "task_id": result.get("task_id"),
            "domain": result.get("domain"),
            "status": result.get("status"),
            "text": result.get("text"),
            "clarification_question": result.get("clarification_question"),
            "error": result.get("error"),
        }
    )


def _results_summary(results: list[dict[str, Any]]) -> str:
    return _compact_json(
        [
            {
                "task_id": result.get("task_id"),
                "domain": result.get("domain"),
                "status": result.get("status"),
                "text": result.get("text"),
                "clarification_question": result.get("clarification_question"),
            }
            for result in results
        ]
    )


def _assistant_content(response: dict[str, Any]) -> str:
    message = response.get("message")
    if not isinstance(message, dict):
        raise ValueError("Ollama chat response must contain message object")
    role = message.get("role", "assistant")
    if role != "assistant":
        raise ValueError("Ollama chat message role must be assistant")
    content = message.get("content")
    if not isinstance(content, str):
        raise ValueError("Ollama response missing string content field")
    return content


def _parse_plan(content: str, available_domains: frozenset[str]) -> dict[str, Any]:
    try:
        raw_plan = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"orchestrator plan must be valid JSON. Got: {content}") from exc
    return _validate_plan(raw_plan, available_domains)


def _validate_plan(raw_plan: Any, available_domains: frozenset[str]) -> dict[str, Any]:
    if not isinstance(raw_plan, dict):
        raise ValueError("orchestrator plan must be a JSON object")
    expected_keys = {
        "kind",
        "confidence",
        "tasks",
        "context_updates",
        "needs_clarification",
        "clarification_question",
    }
    if set(raw_plan) != expected_keys:
        raise ValueError(
            f"orchestrator plan fields must be exactly {sorted(expected_keys)}"
        )

    kind = raw_plan.get("kind")
    valid_kinds = {
        "single_task",
        "multi_task",
        "followup",
        "clarification_answer",
        "chat",
    }
    if kind not in valid_kinds:
        raise ValueError("orchestrator plan kind is invalid")

    confidence = raw_plan.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValueError("orchestrator plan confidence must be a number between 0.0 and 1.0")

    tasks = raw_plan.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("orchestrator plan tasks must be a list")
    validated_tasks: list[DomainTask] = []
    earlier_ids: set[str] = set()
    for index, task in enumerate(tasks):
        validated_task = _validate_task(
            task,
            index,
            available_domains=available_domains,
            earlier_ids=earlier_ids,
        )
        if validated_task["id"] in earlier_ids:
            raise ValueError(f"orchestrator task id is duplicated: {validated_task['id']}")
        earlier_ids.add(validated_task["id"])
        validated_tasks.append(validated_task)

    context_updates = raw_plan.get("context_updates")
    if not isinstance(context_updates, dict):
        raise ValueError("orchestrator plan context_updates must be an object")
    if set(context_updates) != {"salient_entities", "active_domain"}:
        raise ValueError("orchestrator plan context_updates has invented or missing fields")
    salient_entities = context_updates.get("salient_entities")
    if not isinstance(salient_entities, list) or any(not isinstance(item, str) for item in salient_entities):
        raise ValueError("orchestrator plan context_updates.salient_entities must be a list of strings")
    active_domain = context_updates.get("active_domain")
    if active_domain is not None and active_domain not in available_domains:
        raise ValueError("orchestrator plan context_updates.active_domain must be an available domain or null")

    needs_clarification = raw_plan.get("needs_clarification")
    if not isinstance(needs_clarification, bool):
        raise ValueError("orchestrator plan needs_clarification must be a boolean")

    clarification_question = raw_plan.get("clarification_question")
    if clarification_question is not None and not isinstance(clarification_question, str):
        raise ValueError("orchestrator plan clarification_question must be a string or null")
    if needs_clarification and not clarification_question:
        raise ValueError("orchestrator plan clarification_question is required when clarification is needed")

    return {
        "kind": kind,
        "confidence": float(confidence),
        "tasks": validated_tasks,
        "context_updates": {
            "salient_entities": salient_entities,
            "active_domain": active_domain,
        },
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
    }


def _validate_task(
    raw_task: Any,
    index: int,
    *,
    available_domains: frozenset[str],
    earlier_ids: set[str],
) -> DomainTask:
    if not isinstance(raw_task, dict):
        raise ValueError(f"orchestrator task #{index + 1} must be an object")
    expected_keys = {"id", "domain", "request", "depends_on"}
    if set(raw_task) != expected_keys:
        raise ValueError(
            f"orchestrator task #{index + 1} fields must be exactly {sorted(expected_keys)}"
        )

    task_id = raw_task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError(f"orchestrator task #{index + 1} id must be a non-empty string")

    domain = raw_task.get("domain")
    if not isinstance(domain, str) or domain not in available_domains:
        raise ValueError(f"orchestrator task {task_id} domain must be an available domain")

    request = raw_task.get("request")
    if not isinstance(request, str) or not request.strip():
        raise ValueError(f"orchestrator task {task_id} request must be a non-empty string")

    depends_on = raw_task.get("depends_on")
    if not isinstance(depends_on, list) or any(not isinstance(item, str) for item in depends_on):
        raise ValueError(f"orchestrator task {task_id} depends_on must be a list of strings")
    for dependency in depends_on:
        if dependency == task_id:
            raise ValueError(f"orchestrator task {task_id} cannot depend on itself")
        if dependency not in earlier_ids:
            raise ValueError(
                f"orchestrator task {task_id} dependency must name an earlier task: {dependency}"
            )

    return {
        "id": task_id,
        "domain": domain,
        "request": request,
        "depends_on": depends_on,
    }


def _cancels_pending_clarification(user_input: str) -> bool:
    normalized = normalize_text(user_input)
    return normalized in {
        "dziękuję",
        "dziekuje",
        "dzięki",
        "dzieki",
        "ok",
        "okej",
        "nieważne",
        "niewazne",
        "anuluj",
        "stop",
        "nie",
    }


def _domain_result_json(result: dict[str, Any]) -> dict[str, Any]:
    has_continuation = "continuation" in result
    continuation = result.get("continuation")
    normalized = to_json_value(result)
    if not isinstance(normalized, dict):
        raise ValueError("domain result must be a JSON object")
    if has_continuation:
        normalized["continuation"] = _validate_opaque_json(continuation)
    return normalized


def _validate_opaque_json(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("DSA continuation numbers must be finite")
        return value
    if isinstance(value, list):
        for item in value:
            _validate_opaque_json(item)
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("DSA continuation object keys must be strings")
            _validate_opaque_json(item)
        return value
    raise ValueError("DSA continuation must be JSON-serializable")


def _orchestrator_state(conversation: AgentExecutionContext) -> dict[str, Any]:
    state = conversation.agent_state.setdefault(ORCHESTRATOR_STATE_KEY, {})
    if not isinstance(state, dict):
        state = {}
        conversation.agent_state[ORCHESTRATOR_STATE_KEY] = state
    state.setdefault("last_turns", [])
    state.setdefault("salient_entities", [])
    state.setdefault("active_domain", None)
    state.setdefault("pending_tasks", [])
    state.setdefault("pending_clarification", None)
    return state


def _has_pending_clarification(conversation: AgentExecutionContext) -> bool:
    state = conversation.agent_state.get(ORCHESTRATOR_STATE_KEY)
    return isinstance(state, dict) and state.get("pending_clarification") is not None


def _active_context(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_turns": to_json_value(state.get("last_turns", [])),
        "salient_entities": to_json_value(state.get("salient_entities", [])),
        "active_domain": state.get("active_domain"),
        "pending_tasks": to_json_value(state.get("pending_tasks", [])),
        "pending_clarification": to_json_value(state.get("pending_clarification")),
    }


def _apply_context_updates(state: dict[str, Any], plan: dict[str, Any]) -> None:
    updates = plan.get("context_updates", {})
    if not isinstance(updates, dict):
        return
    if "salient_entities" in updates and isinstance(updates["salient_entities"], list):
        state["salient_entities"] = updates["salient_entities"]
    if "active_domain" in updates and (updates["active_domain"] is None or isinstance(updates["active_domain"], str)):
        state["active_domain"] = updates["active_domain"]


def _apply_task_context_updates(state: dict[str, Any], plan: dict[str, Any], task_results: list[dict[str, Any]]) -> None:
    salient_entities = list(state.get("salient_entities", [])) if isinstance(state.get("salient_entities"), list) else []
    active_domain = state.get("active_domain")
    for result in task_results:
        if result.get("status") in {"blocked", "needs_clarification", "unsupported_domain", "failed"}:
            continue
        entities = result.get("entities")
        if isinstance(entities, list):
            for reference in entities:
                if isinstance(reference, str) and reference and reference not in salient_entities:
                    salient_entities.insert(0, reference)
        if isinstance(result.get("domain"), str):
            active_domain = result["domain"]
    if salient_entities:
        state["salient_entities"] = salient_entities[:8]
    if isinstance(active_domain, str):
        state["active_domain"] = active_domain


def _store_pending_tasks(state: dict[str, Any], plan: dict[str, Any]) -> None:
    del plan
    state["pending_tasks"] = []


def _store_pending_clarification(
    state: dict[str, Any],
    plan: dict[str, Any],
    task_results: list[dict[str, Any]],
    *,
    request: str,
) -> None:
    tasks_by_id = {
        task["id"]: task
        for task in plan["tasks"]
        if isinstance(task.get("id"), str)
    }
    for result in task_results:
        if result.get("status") != "needs_clarification":
            continue
        task = tasks_by_id.get(result.get("task_id"))
        if task is None:
            continue
        question = result.get("clarification_question")
        if not isinstance(question, str) or not question:
            question = result.get("text")
        if not isinstance(question, str) or not question:
            raise ValueError("DSA clarification result must include a question")
        has_continuation = "continuation" in result
        continuation = _validate_opaque_json(result.get("continuation"))
        pending_clarification: dict[str, Any] = {
            "owner": "dsa",
            "task": to_json_value(task),
            "question": question,
        }
        if has_continuation:
            pending_clarification["continuation"] = continuation
        state["pending_clarification"] = pending_clarification
        return

    if plan.get("needs_clarification"):
        question = plan.get("clarification_question")
        if not isinstance(question, str) or not question:
            raise ValueError("router clarification plan must include a question")
        state["pending_clarification"] = {
            "owner": "router",
            "request": request,
            "question": question,
        }
        return

    state["pending_clarification"] = None


def _append_last_turn(state: dict[str, Any], *, user_input: str, assistant_reply: str) -> None:
    last_turns = state.get("last_turns", [])
    if not isinstance(last_turns, list):
        last_turns = []
    last_turns.append({"user": user_input, "assistant": assistant_reply})
    state["last_turns"] = last_turns[-MAX_LAST_TURNS:]
