from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Mapping

from aiohttp import ClientSession

from ai_server.agent_loop.agent_callable_set import to_json_value
from ai_server.config import ServerConfig
from ai_server.domain_agents import DomainAgent, DomainTask
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import TextMessage
from ai_server.ollama import OLLAMA_BASE_URL, OllamaClient, OllamaError


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

PLANNING_SYSTEM_PROMPT = """
You are an orchestration planner for a Polish voice assistant.
Return only compact valid JSON. No markdown. No explanations.

Split the latest user utterance into domain tasks. Every utterance goes through you, even follow-ups.
Use active_context as a hint, not a jail: route to a new domain when the utterance asks for one.
Context naming:
- conversation.area is the user's Home Assistant area in the house, such as office or kitchen.
- conversation.server_location is the server's geographic location, such as Wrocław.
Never treat area as a geographic location.
For singular or local Home Assistant requests with no named area, prefer conversation.area when it is known.
When using conversation.area for Home Assistant selection, put it in selector.area, never selector.name.
If the user names an area/room in the utterance, that named area always overrides conversation.area.
Polish area aliases: salon means living room; biuro means office; sypialnia means bedroom.
Use scope="all" only when the user explicitly asks for all/every/wszystkie/każde/everywhere/whole house.
For Home Assistant pronouns such as ją/je/it/them, resolve selection from active_context.salient_entities.
For Home Assistant context_updates.salient_entities, store stable target references like climate.salon or light.bedroom_lamp, not numbers, temperatures, or generic words.
After a Home Assistant command targets a device type and area, preserve that target as <domain>.<area> for follow-up turns.

Return schema:
{
  "kind": "single_task|multi_task|followup|clarification_answer|chat",
  "confidence": 0.0,
  "tasks": [
    {
      "id": "t1",
      "domain": "home_assistant|time|wikipedia|weather|spotify|general",
      "command": {},
      "depends_on": [],
      "status": "ready|blocked",
      "clarification_question": null
    }
  ],
  "context_updates": {
    "salient_entities": [],
    "active_domain": "string or null"
  },
  "needs_clarification": false,
  "clarification_question": null
}
The top-level object must contain context_updates outside tasks. The tasks array must contain only task objects, never strings.
Set confidence from 0.0 to 1.0 for how likely this plan is the correct route and task structure.

For home_assistant tasks, command must use this envelope:
{
  "selection": {
    "include": [{"domain": "light|climate|switch|fan|cover", "scope": "all|single", "name": "optional", "area": "optional"}],
    "exclude": [{"name": "optional", "domain": "optional", "area": "optional"}]
  },
  "operation": {
    "intent": "turn_on|turn_off|set_temperature|set_hvac_mode|set_brightness_percent|adjust|query_state",
    "description": "natural language operation description",
    "parameters": {}
  }
}

For time tasks, command should include geo_location or timezone only when the user explicitly asks for a geographic place or timezone.
For plain questions like "która godzina?", omit geo_location and timezone; the time agent already knows server_location and server_timezone.
Never copy conversation.area into time.geo_location.
{"query": "original time question", "geo_location": "optional geographic place", "timezone": "optional"}

For wikipedia tasks, command should be:
{"intent": "lookup_fact|summary|where_is|coordinates", "topic": "article/search topic", "fact": "birth_year|coordinates|location optional"}
"""

FINAL_REPLY_SYSTEM_PROMPT = """
You are the final response writer for a Polish voice assistant.
Write the final user-facing reply in Polish.
Use the task results and clarification state. Be concise.
Do not claim a task succeeded unless its JSON result says it succeeded.
For a single successful time task, if task_results[0].text is a short direct answer such as "10:46", return it verbatim.
If a task is unsupported, say that capability is not connected yet.
If clarification is needed, ask exactly the needed question, optionally after summarizing completed independent tasks.
"""

CLARIFICATION_SYSTEM_PROMPT = """
You are resolving a pending domain-agent clarification for a Polish voice assistant.
Return only compact valid JSON. No markdown. No explanations.

You receive:
- the user's latest answer
- the pending clarification question
- the original task that asked for clarification
- current conversation context

Return schema:
{
  "confidence": 0.0,
  "task": {
    "id": "same id as original task",
    "domain": "same domain as original task",
    "command": {},
    "depends_on": [],
    "status": "ready|blocked",
    "clarification_question": null
  }
}

Use the latest answer to update the original task command for the same domain.
If the answer still does not resolve the question, keep the same domain and return status="blocked" with a Polish clarification_question.
"""


class OrchestratorAgent:
    def __init__(
        self,
        orchestrator_model: str,
        domain_agents: Mapping[str, DomainAgent] | None = None,
        clarification_model: str | None = None,
        base_url: str = OLLAMA_BASE_URL,
        session: ClientSession | None = None,
        ollama_client: OllamaClient | None = None,
        owns_ollama_client: bool = True,
        server_config: ServerConfig = ServerConfig(),
    ) -> None:
        self._orchestrator_model = orchestrator_model
        self._clarification_model = clarification_model
        self._domain_agents = dict(domain_agents or {})
        self._ollama = ollama_client or OllamaClient(base_url=base_url, session=session)
        self._owns_ollama = owns_ollama_client
        self._server_config = server_config
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

    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        logger = logging.getLogger(f"{__name__}.OrchestratorAgent[{conversation.conversation_id}]")
        try:
            async for message in endpoint.messages():
                started_at = time.perf_counter()
                logger.info("received message text=%r", message.text)
                try:
                    reply_text = await self._handle_message(conversation, message.text)
                    logger.info("produced reply text=%r", reply_text)
                except Exception:
                    elapsed_ms = _elapsed_ms(started_at)
                    logger.exception("orchestration failed request_len=%s duration_ms=%s", len(message.text), elapsed_ms)
                    await endpoint.send_message(TextMessage(text=GENERATION_FAILURE_MESSAGE))
                    continue

                elapsed_ms = _elapsed_ms(started_at)
                logger.info(
                    "orchestration completed request_len=%s reply_len=%s duration_ms=%s",
                    len(message.text),
                    len(reply_text),
                    elapsed_ms,
                )
                await endpoint.send_message(TextMessage(text=reply_text))
        finally:
            state = conversation.state.get(ORCHESTRATOR_STATE_KEY)
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

    async def _handle_message(self, conversation: Conversation, user_input: str) -> str:
        state = _orchestrator_state(conversation)
        active_context = _active_context(state)
        if state.get("pending_clarification") is not None:
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

        plan = await self._plan_message(
            user_input=user_input,
            active_context=active_context,
            conversation=conversation,
        )
        self._logger.info(
            "plan ready conversation_id=%s kind=%s confidence=%s tasks=%s",
            conversation.conversation_id,
            plan["kind"],
            plan["confidence"],
            _tasks_summary(plan["tasks"]),
        )
        _resolve_context_references(plan, active_context)

        task_results = await self._dispatch_ready_tasks(conversation, plan, active_context)
        self._log_clarification_causing_utterance(conversation, user_input, plan, task_results)
        _apply_context_updates(state, plan)
        _apply_task_context_updates(state, plan, task_results)
        _store_pending_tasks(state, plan)
        _store_pending_clarification(state, plan, task_results)

        reply = await self._final_reply(
            user_input=user_input,
            active_context=_active_context(state),
            plan=plan,
            task_results=task_results,
        )
        _append_last_turn(state, user_input=user_input, assistant_reply=reply)
        return reply

    async def _handle_clarification_answer(
        self,
        *,
        conversation: Conversation,
        user_input: str,
        state: dict[str, Any],
        active_context: dict[str, Any],
    ) -> str:
        pending_clarification = state.get("pending_clarification")
        if not isinstance(pending_clarification, dict):
            state["pending_clarification"] = None
            raise ValueError("pending clarification must be an object")

        clarification = await self._clarification_task(
            user_input=user_input,
            pending_clarification=pending_clarification,
            active_context=active_context,
            conversation=conversation,
        )
        task = clarification["task"]
        self._logger.info(
            "clarification resolved conversation_id=%s confidence=%s task=%s",
            conversation.conversation_id,
            clarification["confidence"],
            _task_summary(task),
        )
        plan = {
            "kind": "clarification_answer",
            "confidence": clarification["confidence"],
            "tasks": [task],
            "context_updates": {},
            "needs_clarification": False,
            "clarification_question": None,
        }
        task_results = await self._dispatch_ready_tasks(conversation, plan, active_context)
        self._log_clarification_causing_utterance(conversation, user_input, plan, task_results)
        _apply_task_context_updates(state, plan, task_results)
        _store_pending_tasks(state, plan)
        _store_pending_clarification(state, plan, task_results)

        reply = await self._final_reply(
            user_input=user_input,
            active_context=_active_context(state),
            plan=plan,
            task_results=task_results,
        )
        return reply

    async def _plan_message(
        self,
        *,
        user_input: str,
        active_context: dict[str, Any],
        conversation: Conversation,
    ) -> dict[str, Any]:
        prompt = {
            "utterance": user_input,
            "conversation": {
                "conversation_id": conversation.conversation_id,
                "user": conversation.user,
                "area": conversation.area,
                "server_location": self._server_config.location,
                "server_timezone": self._server_config.timezone,
            },
            "active_context": active_context,
        }
        self._logger.info(
            "planning message conversation_id=%s model=%s text=%r",
            conversation.conversation_id,
            self._orchestrator_model,
            user_input,
        )
        plan = await self._plan_message_with_model(self._orchestrator_model, prompt)
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
        return await self._plan_message_with_model(self._clarification_model, prompt)

    async def _plan_message_with_model(self, model: str, prompt: dict[str, Any]) -> dict[str, Any]:
        try:
            self._logger.info("planning request model=%s utterance=%r", model, prompt.get("utterance"))
            response = await self._ollama.chat(
                {
                    "model": model,
                    "raw": False,
                    "think": False,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "1h",
                    "options": PLANNING_OPTIONS,
                    "messages": [
                        {"role": "system", "content": PLANNING_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                    ],
                }
            )
            plan = _parse_plan(_assistant_content(response))
            self._logger.info(
                "planning output model=%s kind=%s confidence=%s tasks=%s needs_clarification=%s",
                model,
                plan["kind"],
                plan["confidence"],
                _tasks_summary(plan["tasks"]),
                plan["needs_clarification"],
            )
            return plan
        except Exception:
            if model == self._orchestrator_model and self._clarification_model is not None and self._clarification_model != model:
                self._logger.warning("planning with orchestrator model failed, retrying clarification_model", exc_info=True)
                return await self._plan_message_with_model(self._clarification_model, prompt)
            raise

    async def _clarification_task(
        self,
        *,
        user_input: str,
        pending_clarification: dict[str, Any],
        active_context: dict[str, Any],
        conversation: Conversation,
    ) -> dict[str, Any]:
        model = self._clarification_model or self._orchestrator_model
        self._logger.info(
            "clarification request model=%s conversation_id=%s answer=%r pending=%s",
            model,
            conversation.conversation_id,
            user_input,
            _compact_json(pending_clarification),
        )
        prompt = {
            "utterance": user_input,
            "conversation": {
                "conversation_id": conversation.conversation_id,
                "user": conversation.user,
                "area": conversation.area,
                "server_location": self._server_config.location,
                "server_timezone": self._server_config.timezone,
            },
            "active_context": active_context,
            "pending_clarification": pending_clarification,
        }

        try:
            return await self._clarification_task_with_model(
                model=model,
                prompt=prompt,
                pending_clarification=pending_clarification,
            )
        except Exception:
            fallback_model = self._orchestrator_model
            if model != fallback_model:
                self._logger.warning(
                    "clarification with model=%s failed, retrying fallback_model=%s",
                    model,
                    fallback_model,
                    exc_info=True,
                )
                return await self._clarification_task_with_model(
                    model=fallback_model,
                    prompt=prompt,
                    pending_clarification=pending_clarification,
                )
            raise

    async def _clarification_task_with_model(
        self,
        *,
        model: str,
        prompt: dict[str, Any],
        pending_clarification: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._ollama.chat(
            {
                "model": model,
                "raw": False,
                "think": False,
                "format": "json",
                "stream": False,
                "keep_alive": "1h",
                "options": PLANNING_OPTIONS,
                "messages": [
                    {"role": "system", "content": CLARIFICATION_SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
            }
        )
        clarification = _parse_clarification_task(_assistant_content(response), pending_clarification)
        self._logger.info(
            "clarification output model=%s confidence=%s task=%s",
            model,
            clarification["confidence"],
            _task_summary(clarification["task"]),
        )
        return clarification

    async def _dispatch_ready_tasks(
        self,
        conversation: Conversation,
        plan: dict[str, Any],
        active_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        completed_task_ids: set[str] = set()
        results = []
        for task in plan["tasks"]:
            if task.get("status") == "blocked":
                result = _blocked_task_result(task)
                self._logger.info("task blocked task=%s result=%s", _task_summary(task), _result_summary(result))
                results.append(result)
                continue

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
                result = {"task_id": task["id"], "domain": task["domain"], **to_json_value(result)}

            self._logger.info("task result task_id=%s result=%s", task["id"], _result_summary(result))
            results.append(result)
            if result.get("status") not in {"blocked", "needs_clarification", "unsupported_domain"}:
                completed_task_ids.add(task["id"])
        return results

    def _log_clarification_causing_utterance(
        self,
        conversation: Conversation,
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
            source = (
                "orchestrator"
                if task is not None and task.get("status") == "blocked"
                else "dsa"
            )
            self._logger.warning(
                "utterance caused clarification source=%s conversation_id=%s utterance=%r task=%s result=%s",
                source,
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
        user_input: str,
        active_context: dict[str, Any],
        plan: dict[str, Any],
        task_results: list[dict[str, Any]],
    ) -> str:
        prompt = {
            "utterance": user_input,
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
        reply = await self._final_reply_with_model(self._orchestrator_model, prompt)
        if reply or self._clarification_model is None:
            return reply or GENERATION_FAILURE_MESSAGE

        self._logger.info("final reply from orchestrator model was empty, retrying clarification_model=%s", self._clarification_model)
        reply = await self._final_reply_with_model(self._clarification_model, prompt)
        return reply or GENERATION_FAILURE_MESSAGE

    async def _final_reply_with_model(self, model: str, prompt: dict[str, Any]) -> str:
        try:
            self._logger.info("final reply model request model=%s utterance=%r", model, prompt.get("utterance"))
            response = await self._ollama.chat(
                {
                    "model": model,
                    "raw": False,
                    "think": False,
                    "stream": False,
                    "keep_alive": "1h",
                    "options": FINAL_REPLY_OPTIONS,
                    "messages": [
                        {"role": "system", "content": FINAL_REPLY_SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                    ],
                }
            )
            reply = _assistant_content(response).strip()
            self._logger.info("final reply output model=%s text=%r", model, reply)
            return reply
        except Exception:
            if model == self._orchestrator_model and self._clarification_model is not None and self._clarification_model != model:
                self._logger.warning("final reply with orchestrator model failed, retrying clarification_model", exc_info=True)
                return await self._final_reply_with_model(self._clarification_model, prompt)
            raise


def _elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


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
            "status": task.get("status"),
            "command": task.get("command"),
            "clarification_question": task.get("clarification_question"),
        }
    )


def _tasks_summary(tasks: list[dict[str, Any]]) -> str:
    return _compact_json([{"id": task.get("id"), "domain": task.get("domain"), "status": task.get("status")} for task in tasks])


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


def _parse_plan(content: str) -> dict[str, Any]:
    try:
        raw_plan = json.loads(content)
    except json.JSONDecodeError as exc:
        raw_plan = _repair_malformed_plan(content)
        if raw_plan is None:
            raise ValueError(f"orchestrator plan must be valid JSON. Got: {content}") from exc
    if isinstance(raw_plan, str):
        repaired_plan = _repair_malformed_plan(raw_plan)
        if repaired_plan is not None:
            raw_plan = repaired_plan
    return _validate_plan(raw_plan)


def _parse_clarification_task(content: str, pending_clarification: dict[str, Any]) -> dict[str, Any]:
    try:
        raw_response = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"orchestrator clarification response must be valid JSON. Got: {content}") from exc
    if not isinstance(raw_response, dict):
        raise ValueError("orchestrator clarification response must be a JSON object")

    confidence = raw_response.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValueError("orchestrator clarification confidence must be a number between 0.0 and 1.0")

    task = _validate_task(raw_response.get("task"), 0)
    pending_domain = pending_clarification.get("domain")
    if isinstance(pending_domain, str) and task["domain"] != pending_domain:
        raise ValueError("orchestrator clarification task domain must match pending clarification domain")
    pending_task = pending_clarification.get("task")
    if isinstance(pending_task, dict):
        pending_task_id = pending_task.get("id")
        if isinstance(pending_task_id, str) and task["id"] != pending_task_id:
            raise ValueError("orchestrator clarification task id must match pending clarification task id")

    return {
        "confidence": float(confidence),
        "task": task,
    }


def _repair_malformed_plan(content: str) -> dict[str, Any] | None:
    candidate = _unquote_jsonish_content(content.strip())
    kind_match = re.search(r'"kind"\s*:\s*"([^"]+)"', candidate)
    tasks_match = re.search(r'"tasks"\s*:\s*\[', candidate)
    confidence = _decode_named_float(candidate, "confidence")
    if kind_match is None or tasks_match is None or confidence is None:
        return None

    tasks = _decode_leading_task_objects(candidate, tasks_match.end())
    if not tasks:
        return None

    context_updates = _decode_named_object(candidate, "context_updates") or {}
    return {
        "kind": kind_match.group(1),
        "confidence": confidence,
        "tasks": tasks,
        "context_updates": context_updates,
        "needs_clarification": _decode_named_bool(candidate, "needs_clarification", False),
        "clarification_question": _decode_named_string_or_null(candidate, "clarification_question"),
    }


def _unquote_jsonish_content(content: str) -> str:
    if not content.startswith('"'):
        return content
    try:
        decoded = json.loads(content)
    except json.JSONDecodeError:
        decoded = content[1:-1] if content.endswith('"') else content[1:]
        decoded = decoded.replace(r"\"", '"').replace(r"\\", "\\")
    return decoded if isinstance(decoded, str) else content


def _decode_leading_task_objects(content: str, start: int) -> list[Any]:
    decoder = json.JSONDecoder()
    tasks: list[Any] = []
    position = start
    while position < len(content):
        while position < len(content) and content[position] in " \t\r\n,":
            position += 1
        if position >= len(content) or content[position] == "]":
            break
        if content[position] != "{":
            break
        try:
            task, position = decoder.raw_decode(content, position)
        except json.JSONDecodeError:
            break
        tasks.append(task)
    return tasks


def _decode_named_object(content: str, name: str) -> dict[str, Any] | None:
    match = re.search(rf'"{re.escape(name)}"\s*:\s*{{', content)
    if match is None:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(content, match.end() - 1)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _decode_named_bool(content: str, name: str, default: bool) -> bool:
    match = re.search(rf'"{re.escape(name)}"\s*:\s*(true|false)', content)
    if match is None:
        return default
    return match.group(1) == "true"


def _decode_named_float(content: str, name: str) -> float | None:
    match = re.search(rf'"{re.escape(name)}"\s*:\s*([0-9]+(?:\.[0-9]+)?)', content)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _decode_named_string_or_null(content: str, name: str) -> str | None:
    match = re.search(rf'"{re.escape(name)}"\s*:\s*(null|"([^"]*)")', content)
    if match is None or match.group(1) == "null":
        return None
    return match.group(2)


def _validate_plan(raw_plan: Any) -> dict[str, Any]:
    if not isinstance(raw_plan, dict):
        raise ValueError("orchestrator plan must be a JSON object")

    kind = raw_plan.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("orchestrator plan kind must be a non-empty string")

    confidence = raw_plan.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValueError("orchestrator plan confidence must be a number between 0.0 and 1.0")

    tasks = raw_plan.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("orchestrator plan tasks must be a list")

    tasks = _repair_tasks(tasks, raw_plan)
    validated_tasks = [_validate_task(task, index) for index, task in enumerate(tasks)]
    context_updates = raw_plan.get("context_updates", {})
    if context_updates is None:
        context_updates = {}
    if not isinstance(context_updates, dict):
        raise ValueError("orchestrator plan context_updates must be an object")

    needs_clarification = raw_plan.get("needs_clarification", False)
    if not isinstance(needs_clarification, bool):
        raise ValueError("orchestrator plan needs_clarification must be a boolean")

    clarification_question = raw_plan.get("clarification_question")
    if clarification_question is not None and not isinstance(clarification_question, str):
        raise ValueError("orchestrator plan clarification_question must be a string or null")

    return {
        "kind": kind,
        "confidence": float(confidence),
        "tasks": validated_tasks,
        "context_updates": to_json_value(context_updates),
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
    }


def _repair_tasks(tasks: list[Any], raw_plan: dict[str, Any]) -> list[Any]:
    repaired_tasks = []
    for task in tasks:
        if isinstance(task, dict):
            repaired_tasks.append(task)
            continue
        if isinstance(task, str):
            _repair_embedded_plan_fragment(task, raw_plan)
    return repaired_tasks


def _repair_embedded_plan_fragment(fragment: str, raw_plan: dict[str, Any]) -> None:
    stripped_fragment = fragment.strip().strip(",")
    if stripped_fragment.startswith(("confidence", "context_updates", "needs_clarification", "clarification_question")):
        stripped_fragment = '{"' + stripped_fragment + "}"
    elif not stripped_fragment.startswith("{"):
        stripped_fragment = "{" + stripped_fragment + "}"
    try:
        parsed_fragment = json.loads(stripped_fragment)
    except json.JSONDecodeError:
        return
    if not isinstance(parsed_fragment, dict):
        return
    context_updates = parsed_fragment.get("context_updates")
    if isinstance(context_updates, dict) and not isinstance(raw_plan.get("context_updates"), dict):
        raw_plan["context_updates"] = context_updates
    for key in ("confidence", "needs_clarification", "clarification_question"):
        if key in parsed_fragment and key not in raw_plan:
            raw_plan[key] = parsed_fragment[key]


def _validate_task(raw_task: Any, index: int) -> DomainTask:
    if not isinstance(raw_task, dict):
        raise ValueError(f"orchestrator task #{index + 1} must be an object")

    task_id = raw_task.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError(f"orchestrator task #{index + 1} id must be a non-empty string")

    domain = raw_task.get("domain")
    if not isinstance(domain, str) or not domain:
        raise ValueError(f"orchestrator task {task_id} domain must be a non-empty string")

    command = raw_task.get("command", {})
    if command is None:
        command = {}
    if not isinstance(command, dict):
        raise ValueError(f"orchestrator task {task_id} command must be an object")
    if domain == "home_assistant":
        command = _validate_home_assistant_command(command, task_id)
    else:
        command = to_json_value(command)

    depends_on = raw_task.get("depends_on", [])
    if depends_on is None:
        depends_on = []
    if not isinstance(depends_on, list) or any(not isinstance(item, str) for item in depends_on):
        raise ValueError(f"orchestrator task {task_id} depends_on must be a list of strings")

    status = raw_task.get("status", "ready")
    if status not in {"ready", "blocked"}:
        raise ValueError(f"orchestrator task {task_id} status must be ready or blocked")

    clarification_question = raw_task.get("clarification_question")
    if clarification_question is not None and not isinstance(clarification_question, str):
        raise ValueError(f"orchestrator task {task_id} clarification_question must be a string or null")

    return {
        "id": task_id,
        "domain": domain,
        "command": command,
        "depends_on": depends_on,
        "status": status,
        "clarification_question": clarification_question,
    }


def _validate_home_assistant_command(command: dict[str, Any], task_id: str) -> dict[str, Any]:
    selection = command.get("selection")
    if not isinstance(selection, dict):
        raise ValueError(f"orchestrator task {task_id} home_assistant selection must be an object")

    include = _validate_selector_list(selection.get("include", []), task_id, "include")
    exclude = _validate_selector_list(selection.get("exclude", []), task_id, "exclude")

    operation = command.get("operation")
    if not isinstance(operation, dict):
        raise ValueError(f"orchestrator task {task_id} home_assistant operation must be an object")

    intent = operation.get("intent")
    if not isinstance(intent, str) or not intent:
        raise ValueError(f"orchestrator task {task_id} home_assistant operation.intent must be a non-empty string")

    description = operation.get("description")
    if not isinstance(description, str) or not description:
        raise ValueError(f"orchestrator task {task_id} home_assistant operation.description must be a non-empty string")

    parameters = operation.get("parameters", {})
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        raise ValueError(f"orchestrator task {task_id} home_assistant operation.parameters must be an object")

    return {
        "selection": {
            "include": include,
            "exclude": exclude,
        },
        "operation": {
            "intent": intent,
            "description": description,
            "parameters": to_json_value(parameters),
        },
    }


def _validate_selector_list(raw_selectors: Any, task_id: str, field: str) -> list[dict[str, Any]]:
    if raw_selectors is None:
        return []
    if not isinstance(raw_selectors, list):
        raise ValueError(f"orchestrator task {task_id} home_assistant selection.{field} must be a list")

    selectors = []
    for index, raw_selector in enumerate(raw_selectors):
        if not isinstance(raw_selector, dict):
            raise ValueError(
                f"orchestrator task {task_id} home_assistant selection.{field}[{index}] must be an object"
            )
        selector = {
            key: value
            for key, value in to_json_value(raw_selector).items()
            if value is not None
        }
        for key, value in selector.items():
            if key in {"domain", "scope", "name", "area"} and not isinstance(value, str):
                raise ValueError(
                    f"orchestrator task {task_id} home_assistant selection.{field}[{index}].{key} must be a string"
                )
        selectors.append(selector)
    return selectors


def _resolve_context_references(plan: dict[str, Any], active_context: dict[str, Any]) -> None:
    salient_entities = active_context.get("salient_entities", [])
    if not isinstance(salient_entities, list):
        return
    reference = next((entity for entity in salient_entities if isinstance(entity, str) and "." in entity), None)
    if reference is None:
        return

    selector = _selector_from_reference(reference)
    if selector is None:
        return

    for task in plan["tasks"]:
        if task["domain"] != "home_assistant":
            continue
        command = task.get("command")
        if not isinstance(command, dict):
            continue
        operation = command.get("operation")
        if not isinstance(operation, dict):
            continue
        description = operation.get("description", "")
        if not isinstance(description, str) or not _contains_reference_pronoun(description):
            continue
        selection = command.get("selection")
        if not isinstance(selection, dict):
            continue
        selection["include"] = [selector]
        context_updates = plan.get("context_updates")
        if isinstance(context_updates, dict):
            context_updates["salient_entities"] = [reference]
            context_updates["active_domain"] = "home_assistant"


def _selector_from_reference(reference: str) -> dict[str, str] | None:
    domain, target = reference.split(".", 1)
    if not domain or not target:
        return None
    selector = {
        "domain": domain,
        "scope": "single",
    }
    if "." in target:
        selector["name"] = reference
    else:
        selector["area"] = target
    return selector


def _contains_reference_pronoun(text: str) -> bool:
    normalized_text = f" {text.casefold()} "
    return any(
        f" {pronoun} " in normalized_text
        for pronoun in ("ją", "ja", "je", "go", "jego", "jej", "it", "them", "this", "that")
    )


def _orchestrator_state(conversation: Conversation) -> dict[str, Any]:
    state = conversation.state.setdefault(ORCHESTRATOR_STATE_KEY, {})
    if not isinstance(state, dict):
        state = {}
        conversation.state[ORCHESTRATOR_STATE_KEY] = state
    state.setdefault("last_turns", [])
    state.setdefault("salient_entities", [])
    state.setdefault("active_domain", None)
    state.setdefault("pending_tasks", [])
    state.setdefault("pending_clarification", None)
    return state


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
    successful_task_ids = {
        result["task_id"]
        for result in task_results
        if isinstance(result.get("task_id"), str)
        and result.get("status") not in {"blocked", "needs_clarification", "unsupported_domain", "failed"}
    }
    salient_entities = list(state.get("salient_entities", [])) if isinstance(state.get("salient_entities"), list) else []
    for task in plan["tasks"]:
        if task["id"] not in successful_task_ids:
            continue
        if task["domain"] != "home_assistant":
            continue
        for reference in _home_assistant_task_references(task):
            if reference not in salient_entities:
                salient_entities.insert(0, reference)
    if salient_entities:
        state["salient_entities"] = salient_entities[:8]
        state["active_domain"] = "home_assistant"


def _home_assistant_task_references(task: dict[str, Any]) -> list[str]:
    command = task.get("command")
    if not isinstance(command, dict):
        return []
    selection = command.get("selection")
    if not isinstance(selection, dict):
        return []
    include = selection.get("include", [])
    if not isinstance(include, list):
        return []

    references = []
    for selector in include:
        if not isinstance(selector, dict):
            continue
        domain = selector.get("domain")
        area = selector.get("area")
        name = selector.get("name")
        if isinstance(domain, str) and domain and isinstance(area, str) and area:
            references.append(f"{domain}.{area}")
            continue
        if isinstance(domain, str) and domain and isinstance(name, str) and name:
            references.append(f"{domain}.{name}")
            continue
        if isinstance(name, str) and name:
            references.append(name)
    return references


def _store_pending_tasks(state: dict[str, Any], plan: dict[str, Any]) -> None:
    pending_tasks = [task for task in plan["tasks"] if task.get("status") == "blocked"]
    state["pending_tasks"] = pending_tasks


def _store_pending_clarification(state: dict[str, Any], plan: dict[str, Any], task_results: list[dict[str, Any]]) -> None:
    tasks_by_id = {
        task["id"]: task
        for task in plan["tasks"]
        if isinstance(task.get("id"), str)
    }
    for result in task_results:
        if result.get("status") != "needs_clarification":
            continue
        task_id = result.get("task_id")
        task = tasks_by_id.get(task_id)
        if task is None:
            continue
        clarification_question = result.get("clarification_question")
        if clarification_question is not None and not isinstance(clarification_question, str):
            clarification_question = None
        if clarification_question is None and isinstance(result.get("text"), str):
            clarification_question = result["text"]
        state["pending_clarification"] = {
            "domain": task["domain"],
            "task": to_json_value(task),
            "task_result": to_json_value(result),
            "clarification_question": clarification_question,
        }
        return

    if plan.get("kind") == "clarification_answer":
        state["pending_clarification"] = None


def _append_last_turn(state: dict[str, Any], *, user_input: str, assistant_reply: str) -> None:
    last_turns = state.get("last_turns", [])
    if not isinstance(last_turns, list):
        last_turns = []
    last_turns.append({"user": user_input, "assistant": assistant_reply})
    state["last_turns"] = last_turns[-MAX_LAST_TURNS:]


def _blocked_task_result(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": task["id"],
        "domain": task["domain"],
        "status": "needs_clarification",
        "clarification_question": task.get("clarification_question"),
    }
