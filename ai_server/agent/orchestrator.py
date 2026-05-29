from __future__ import annotations

import json
import logging
import time
from typing import Any, Mapping

from aiohttp import ClientSession

from ai_server.agent_loop.agent_callable_set import to_json_value
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

PLANNING_SYSTEM_PROMPT = """
You are an orchestration planner for a Polish voice assistant.
Return only compact valid JSON. No markdown. No explanations.

Split the latest user utterance into domain tasks. Every utterance goes through you, even follow-ups.
Use active_context as a hint, not a jail: route to a new domain when the utterance asks for one.
For singular or local Home Assistant requests with no named room, prefer conversation.location when it is known.
When using conversation.location for Home Assistant selection, put it in selector.area, never selector.name.
Use scope="all" only when the user explicitly asks for all/every/wszystkie/każde/everywhere/whole house.
For Home Assistant pronouns such as ją/je/it/them, resolve selection from active_context.salient_entities.
For Home Assistant context_updates.salient_entities, store stable target references like climate.salon or light.bedroom_lamp, not numbers, temperatures, or generic words.
After a Home Assistant command targets a device type and area, preserve that target as <domain>.<area> for follow-up turns.

Return schema:
{
  "kind": "single_task|multi_task|followup|clarification_answer|chat",
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

For time tasks, command should include any known location or timezone:
{"query": "original time question", "location": "optional", "timezone": "optional"}
"""

FINAL_REPLY_SYSTEM_PROMPT = """
You are the final response writer for a Polish voice assistant.
Write the final user-facing reply in Polish.
Use the task results and clarification state. Be concise.
Do not claim a task succeeded unless its JSON result says it succeeded.
If a task is unsupported, say that capability is not connected yet.
If clarification is needed, ask exactly the needed question, optionally after summarizing completed independent tasks.
"""


class OrchestratorAgent:
    def __init__(
        self,
        model: str,
        domain_agents: Mapping[str, DomainAgent] | None = None,
        base_url: str = OLLAMA_BASE_URL,
        session: ClientSession | None = None,
        ollama_client: OllamaClient | None = None,
        owns_ollama_client: bool = True,
    ) -> None:
        self._model = model
        self._domain_agents = dict(domain_agents or {})
        self._ollama = ollama_client or OllamaClient(base_url=base_url, session=session)
        self._owns_ollama = owns_ollama_client
        self._logger = logging.getLogger(f"{__name__}.OrchestratorAgent[{model}]")

    async def preload(self) -> None:
        try:
            await self._ollama.chat(
                {
                    "model": self._model,
                    "think": False,
                    "format": "json",
                    "stream": False,
                    "keep_alive": "1h",
                    "messages": [{"role": "user", "content": 'Return JSON: {"ok":true}'}],
                }
            )
        except Exception as exc:
            raise OllamaError(f"failed to preload Ollama model {self._model}") from exc

    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        logger = logging.getLogger(f"{__name__}.OrchestratorAgent[{conversation.conversation_id}]")
        async for message in endpoint.messages():
            started_at = time.perf_counter()
            try:
                reply_text = await self._handle_message(conversation, message.text)
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
        plan = await self._plan_message(
            user_input=user_input,
            active_context=active_context,
            conversation=conversation,
        )
        _resolve_context_references(plan, active_context)

        task_results = await self._dispatch_ready_tasks(conversation, plan, active_context)
        _apply_context_updates(state, plan)
        _apply_task_context_updates(state, plan, task_results)
        _store_pending_tasks(state, plan)

        reply = await self._final_reply(
            user_input=user_input,
            active_context=_active_context(state),
            plan=plan,
            task_results=task_results,
        )
        _append_last_turn(state, user_input=user_input, assistant_reply=reply)
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
                "location": conversation.location,
            },
            "active_context": active_context,
        }
        response = await self._ollama.chat(
            {
                "model": self._model,
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
        return _parse_plan(_assistant_content(response))

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
                results.append(_blocked_task_result(task))
                continue

            missing_dependencies = [dependency for dependency in task["depends_on"] if dependency not in completed_task_ids]
            if missing_dependencies:
                results.append(
                    {
                        "task_id": task["id"],
                        "domain": task["domain"],
                        "status": "blocked",
                        "error": "missing_dependencies",
                        "missing_dependencies": missing_dependencies,
                    }
                )
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
                result = await domain_agent.run_task(conversation, task, active_context)
                if not isinstance(result, dict):
                    raise ValueError(f"domain agent {task['domain']} returned non-object result")
                result = {"task_id": task["id"], "domain": task["domain"], **to_json_value(result)}

            results.append(result)
            if result.get("status") not in {"blocked", "needs_clarification", "unsupported_domain"}:
                completed_task_ids.add(task["id"])
        return results

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
        response = await self._ollama.chat(
            {
                "model": self._model,
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
        return _assistant_content(response).strip() or GENERATION_FAILURE_MESSAGE


def _elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


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
        raise ValueError("orchestrator plan must be valid JSON") from exc
    return _validate_plan(raw_plan)


def _validate_plan(raw_plan: Any) -> dict[str, Any]:
    if not isinstance(raw_plan, dict):
        raise ValueError("orchestrator plan must be a JSON object")

    kind = raw_plan.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ValueError("orchestrator plan kind must be a non-empty string")

    tasks = raw_plan.get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("orchestrator plan tasks must be a list")

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
        "tasks": validated_tasks,
        "context_updates": to_json_value(context_updates),
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
    }


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
    return state


def _active_context(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "last_turns": to_json_value(state.get("last_turns", [])),
        "salient_entities": to_json_value(state.get("salient_entities", [])),
        "active_domain": state.get("active_domain"),
        "pending_tasks": to_json_value(state.get("pending_tasks", [])),
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
