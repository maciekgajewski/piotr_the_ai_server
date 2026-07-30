from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Annotated, Any, Callable

from ai_server.agent_loop import AgentCallableSet, AgentLoop, AgentLoopConfig, AgentLoopOllamaConnection
from ai_server.domain_agents.interfaces import DomainTask
from ai_server.home_assistant import HomeAssistantConnection
from ai_server.home_assistant.toolset import HomeAssistantToolSet
from ai_server.conversations.agent_context import AgentExecutionContext
from ai_server.orchestrator.messages import JsonValue
from ai_server.utils.conversation_style import reply_style_instruction, system_prompt_with_reply_style


ROUTING_SUMMARY = (
    "Home Assistant lights, climate, switches, fans, covers, device changes, "
    "and read-only device state, configuration, counts, or lists."
)


SYSTEM_PROMPT_TEMPLATE = """
{home_assistant_context}

You are a Home Assistant domain-specific agent.
You receive exactly one routed task containing the user's request.
Interpret the request inside this domain. Your first assistant action must be the tool call
execute_home_assistant_task with a concrete Home Assistant command.
That tool validates the command and performs the required inspection or modification.
Do not ask for another user turn directly. Do not own the conversation.
Use conversation.area only when the request does not name an area. A named area overrides it.
Use active_context for pronouns and follow-ups. Use scope="all" only when explicitly requested.
Area names and aliases in the Home Assistant context are authoritative.
Return final JSON only after execute_home_assistant_task returns.
If the tool returns status="ok" or "partial", your JSON must keep that status.
If the tool returns status="needs_clarification", your JSON must keep needs_clarification=true and include the clarification_question.
Never claim success unless execute_home_assistant_task returned status="ok" or "partial".

Routed input shape:
{{
  "task": {{
    "id": "string",
    "domain": "home_assistant",
    "request": "user's task text",
    "depends_on": []
  }},
  "active_context": {{}}
}}

Tool command shape:
{{
  "selection": {{
    "include": [{{"domain": "climate|light|switch|fan|cover", "scope": "single|all", "name": "optional", "area": "optional"}}],
    "exclude": [{{"name": "optional", "domain": "optional", "area": "optional"}}]
  }},
  "operation": {{
    "intent": "turn_on|turn_off|set_temperature|set_hvac_mode|set_brightness_percent|adjust|query_state|query_configuration",
    "description": "the user's requested operation",
    "parameters": {{}}
  }}
}}

Return only compact valid JSON with this shape:
{{
  "status": "ok|partial|failed|needs_clarification",
  "text": "short Polish result text",
  "needs_clarification": false,
  "clarification_question": null,
  "entities": []
}}

If the task is ambiguous after inspecting Home Assistant, return status="needs_clarification" and a Polish clarification_question.
Never claim success unless a modifying tool returned status=ok for the requested change.
"""


class HomeAssistantDomainAgent:
    def __init__(
        self,
        *,
        model: str,
        ollama_url: str,
        connection: HomeAssistantConnection,
        fallback_model: str | None = None,
        fallback_backoff_seconds: float = 300.0,
        ollama_connection: AgentLoopOllamaConnection | None = None,
        loop_factory: Callable[..., AgentLoop] = AgentLoop,
        processing_update_interval_seconds: float = 5.0,
    ) -> None:
        self._model = model
        self._ollama_url = ollama_url
        self._fallback_model = fallback_model
        self._fallback_backoff_seconds = fallback_backoff_seconds
        self._ollama_connection = ollama_connection or AgentLoopOllamaConnection(base_url=ollama_url)
        self._owns_ollama_connection = ollama_connection is None
        self._connection = connection
        self._loop_factory = loop_factory
        self._processing_update_interval_seconds = processing_update_interval_seconds
        self._logger = logging.getLogger(f"{__name__}.HomeAssistantDomainAgent[{model}]")

    def known_utterances(self) -> tuple[str, ...]:
        return ()

    def routing_summary(self) -> str:
        return ROUTING_SUMMARY

    async def run_task(
        self,
        conversation: AgentExecutionContext,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = task.get("id", "unknown")
        logger = logging.getLogger(f"{__name__}.HomeAssistantDomainAgent[{self._model}:{conversation.conversation_id}:{task_id}]")
        toolset = HomeAssistantDomainToolSet(
            self._connection,
            routed_task=task,
            active_context=active_context,
            logger_name=f"{__name__}.HomeAssistantToolSet[{conversation.conversation_id}:{task_id}]",
        )
        request_text = _task_request_text(task)
        toolset.set_request_context(user_message=request_text, area=conversation.area)
        deterministic_command = _deterministic_command(
            request_text,
            conversation_area=conversation.area,
            active_context=active_context,
        )
        if deterministic_command is not None:
            logger.info(
                "home_assistant DSA deterministic path conversation_id=%s task_id=%s intent=%s",
                conversation.conversation_id,
                task_id,
                deterministic_command["operation"]["intent"],
            )
            return await toolset.execute_home_assistant_task(deterministic_command)
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            home_assistant_context=self._connection.system_prompt_context(
                user=conversation.user,
                area=conversation.area,
            )
        )
        loop_config = AgentLoopConfig(
            model=self._model,
            ollama_url=self._ollama_url,
            fallback_model=self._fallback_model,
            fallback_backoff_seconds=self._fallback_backoff_seconds,
        )
        payload = {
            "task": task,
            "active_context": active_context,
            "conversation": {
                "user": conversation.user,
                "area": conversation.area,
                "medium": conversation.medium.value,
                "reply_style": reply_style_instruction(conversation.medium),
                "user_settings": conversation.user_settings,
            },
        }
        operation = _operation_summary(task)
        selection_count = _selection_include_count(task)
        logger.info(
            "home_assistant DSA LLM request conversation_id=%s task_id=%s cloud_model=%s local_model=%s "
            "operation=%s selection_count=%s payload_len=%s",
            conversation.conversation_id,
            task_id,
            self._model,
            self._fallback_model,
            operation,
            selection_count,
            len(json.dumps(payload, ensure_ascii=False)),
        )
        logger.debug("running Home Assistant DSA task=%s active_context=%s", task, active_context)
        async with self._loop_factory(
            config=loop_config,
            system_prompt=system_prompt_with_reply_style(system_prompt, conversation.medium),
            tools=toolset,
            ollama_connection=self._ollama_connection,
            processing_update_callback=conversation.processing_update_callback,
            processing_update_interval_seconds=self._processing_update_interval_seconds,
        ) as loop:
            reply = await loop.send_user_message(json.dumps(payload, ensure_ascii=False))
        prompt_tokens = getattr(reply, "prompt_eval_count", None)
        completion_tokens = getattr(reply, "eval_count", None)
        duration_ms = getattr(reply, "duration_ms", None)
        logger.info(
            "home_assistant DSA LLM reply conversation_id=%s task_id=%s cloud_model=%s local_model=%s "
            "end_conversation=%s reply_len=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s duration_ms=%s "
            "tool_executed=%s",
            conversation.conversation_id,
            task_id,
            self._model,
            self._fallback_model,
            reply.end_conversation,
            len(reply.reply_text),
            prompt_tokens,
            completion_tokens,
            _token_total(prompt_tokens, completion_tokens),
            duration_ms,
            toolset.last_execution_result is not None,
        )
        logger.debug("Home Assistant DSA raw reply=%r end_conversation=%s", reply.reply_text, reply.end_conversation)
        if reply.end_conversation:
            logger.info("home_assistant DSA failed conversation_id=%s task_id=%s reason=end_conversation", conversation.conversation_id, task_id)
            return {
                "status": "failed",
                "text": "Nie mogę teraz wykonać polecenia Home Assistant.",
                "needs_clarification": False,
                "entities": [],
            }
        execution_result = toolset.last_execution_result
        if execution_result is None:
            logger.warning(
                "home_assistant DSA model returned without execute_home_assistant_task "
                "conversation_id=%s task_id=%s operation=%s",
                conversation.conversation_id,
                task_id,
                operation,
            )
            return _failed_result("Nie mogę teraz wykonać polecenia Home Assistant.")
        try:
            parsed_reply = _parse_domain_reply(reply.reply_text)
        except ValueError as exc:
            if execution_result is not None:
                logger.info(
                    "home_assistant DSA using tool execution result after invalid model reply "
                    "conversation_id=%s task_id=%s status=%s parse_error=%s",
                    conversation.conversation_id,
                    task_id,
                    execution_result.get("status"),
                    exc,
                )
                logger.debug("using Home Assistant execution result after non-JSON DSA reply=%r", reply.reply_text)
                return execution_result
            logger.warning(
                "home_assistant DSA failed invalid model reply with no executed command conversation_id=%s task_id=%s "
                "parse_error=%s reply=%r",
                conversation.conversation_id,
                task_id,
                exc,
                _abbreviate(reply.reply_text),
            )
            logger.debug("rejecting invalid Home Assistant DSA text reply with no executed command=%r", reply.reply_text)
            return {
                "status": "failed",
                "text": "Nie mogę teraz wykonać polecenia Home Assistant.",
                "needs_clarification": False,
                "entities": [],
            }
        if execution_result is not None:
            logger.info(
                "home_assistant DSA completed from tool execution result conversation_id=%s task_id=%s status=%s",
                conversation.conversation_id,
                task_id,
                execution_result.get("status"),
            )
            return execution_result
        logger.info(
            "home_assistant DSA completed from model final JSON conversation_id=%s task_id=%s status=%s",
            conversation.conversation_id,
            task_id,
            parsed_reply.get("status"),
        )
        return parsed_reply

    async def resume_clarification(
        self,
        conversation: AgentExecutionContext,
        task: DomainTask,
        question: str,
        answer: str,
        active_context: dict[str, Any],
        continuation: JsonValue,
    ) -> dict[str, Any]:
        del continuation
        resumed = {
            **task,
            "request": (
                f"{task['request']}\n"
                f"Pytanie doprecyzowujące: {question}\n"
                f"Odpowiedź użytkownika: {answer}"
            ),
        }
        return await self.run_task(conversation, resumed, active_context)

    async def close(self) -> None:
        if self._owns_ollama_connection:
            await self._ollama_connection.close()


class HomeAssistantDomainToolSet(AgentCallableSet):
    def __init__(
        self,
        connection: HomeAssistantConnection,
        *,
        routed_task: DomainTask,
        active_context: dict[str, Any],
        logger_name: str | None = None,
    ) -> None:
        self._routed_task = routed_task
        self._active_context = active_context
        self._tools = HomeAssistantToolSet(connection, logger_name=logger_name)
        self._last_execution_result: dict[str, Any] | None = None
        self._logger = logging.getLogger(logger_name or f"{__name__}.{type(self).__name__}")

    @property
    def last_execution_result(self) -> dict[str, Any] | None:
        return self._last_execution_result

    def set_request_context(self, *, user_message: str, area: str | None) -> None:
        self._tools.set_request_context(user_message=user_message, area=area)

    @AgentCallableSet.tool(
        description=(
            "Validate and execute a concrete Home Assistant command interpreted from the routed request. "
            "Call this exactly once before returning final JSON."
        )
    )
    async def execute_home_assistant_task(
        self,
        command: Annotated[dict[str, Any], "Concrete command with selection and operation objects."],
    ) -> dict[str, Any]:
        interpreted_task = {
            **self._routed_task,
            "command": _validate_interpreted_command(command),
        }
        self._logger.info("execute_home_assistant_task started operation=%s", _operation_summary(interpreted_task))
        self._last_execution_result = await _execute_home_assistant_task(
            self._tools,
            interpreted_task,
            self._active_context,
        )
        self._logger.info("execute_home_assistant_task completed status=%s", self._last_execution_result.get("status"))
        self._logger.debug("execute_home_assistant_task result=%s", self._last_execution_result)
        return self._last_execution_result


def _validate_interpreted_command(raw_command: Any) -> dict[str, Any]:
    if not isinstance(raw_command, dict):
        raise ValueError("Home Assistant command must be an object")
    if set(raw_command) != {"selection", "operation"}:
        raise ValueError("Home Assistant command must contain only selection and operation")
    selection = raw_command["selection"]
    operation = raw_command["operation"]
    if not isinstance(selection, dict):
        raise ValueError("Home Assistant command selection must be an object")
    if not isinstance(operation, dict):
        raise ValueError("Home Assistant command operation must be an object")
    include = selection.get("include")
    if not isinstance(include, list) or not include or any(not isinstance(item, dict) for item in include):
        raise ValueError("Home Assistant command selection.include must be a non-empty list of objects")
    exclude = selection.get("exclude", [])
    if not isinstance(exclude, list) or any(not isinstance(item, dict) for item in exclude):
        raise ValueError("Home Assistant command selection.exclude must be a list of objects")
    intent = operation.get("intent")
    description = operation.get("description")
    parameters = operation.get("parameters", {})
    if not isinstance(intent, str) or not intent:
        raise ValueError("Home Assistant command operation.intent must be a non-empty string")
    if not isinstance(description, str) or not description:
        raise ValueError("Home Assistant command operation.description must be a non-empty string")
    if not isinstance(parameters, dict):
        raise ValueError("Home Assistant command operation.parameters must be an object")
    return {
        "selection": {"include": include, "exclude": exclude},
        "operation": {
            "intent": intent,
            "description": description,
            "parameters": parameters,
        },
    }


def _task_request_text(task: DomainTask) -> str:
    return task["request"]


def _deterministic_command(
    request: str,
    *,
    conversation_area: str | None,
    active_context: dict[str, Any],
) -> dict[str, Any] | None:
    normalized = _normalize_lookup(request)
    context_ref = _most_salient_context_ref(active_context, "climate")
    explicit_climate = any(marker in normalized for marker in ("klim", "air condition"))
    if not explicit_climate and (
        context_ref is None or not _is_climate_context_followup(normalized)
    ):
        return None

    area = _explicit_area(normalized)
    if area is None:
        area = _unrecognized_explicit_area(normalized)
    if area is None and context_ref is None:
        area = conversation_area
    scope = (
        "all"
        if any(marker in normalized for marker in ("wszystk", "calym domu", "ile ", "how many", "liczba "))
        else "single"
    )
    selector: dict[str, Any] = {"domain": "climate", "scope": scope}
    if area:
        selector["area"] = area

    parameters: dict[str, Any] = {}
    if any(marker in normalized for marker in ("ile ", "how many", "liczba ")):
        intent = "query_state"
        parameters["query_type"] = "count"
    elif "wentyl" in normalized or "fan only" in normalized:
        intent = "set_hvac_mode"
        parameters["hvac_mode"] = "fan_only"
    elif "tryb" in normalized and any(
        marker in normalized
        for marker in ("chlod", "cool", "grzan", "heat", "such", "dry", "auto")
    ):
        intent = "set_hvac_mode"
        mode_markers = (
            (("chlod", "cool"), "cool"),
            (("grzan", "heat"), "heat"),
            (("such", "dry"), "dry"),
            (("auto",), "auto"),
        )
        parameters["hvac_mode"] = next(
            mode
            for markers, mode in mode_markers
            if any(marker in normalized for marker in markers)
        )
    elif any(marker in normalized for marker in ("temperatur", " stopn", "ustaw")):
        intent = "set_temperature"
        temperature_match = re.search(r"(?<!\d)(\d{1,2})(?:[.,]\d+)?(?:\s*(?:stopn|°))", normalized)
        if temperature_match is None and "pytanie doprecyzowujace:" in normalized:
            temperature_match = re.search(
                r"odpowiedz uzytkownika:\s*(\d{1,2})(?:[.,]\d+)?\s*$",
                normalized,
            )
        if temperature_match is not None:
            parameters["temperature"] = int(temperature_match.group(1))
    elif any(marker in normalized for marker in ("wylacz", "wyłącz", "turn off")):
        intent = "turn_off"
    elif any(marker in normalized for marker in ("wlacz", "włącz", "turn on")):
        intent = "turn_on"
    else:
        return None

    return {
        "selection": {"include": [selector], "exclude": []},
        "operation": {
            "intent": intent,
            "description": request,
            "parameters": parameters,
        },
    }


def _is_climate_context_followup(normalized_request: str) -> bool:
    patterns = (
        r"(?:prosze\s+)?(?:ustaw|zmien|set)(?:\s+(?:ja|go|to|it))?\s+(?:na|to)\s+\d{1,2}(?:[.,]\d+)?\s*(?:stopn\w*|degrees?|°)",
        r"(?:prosze\s+)?\d{1,2}(?:[.,]\d+)?\s*(?:stopn\w*|degrees?|°)",
        r"(?:prosze\s+)?(?:wylacz|wlacz)(?:\s+(?:ja|go|to))?",
        r"(?:please\s+)?turn\s+(?:it\s+)?(?:off|on)",
        r"(?:prosze\s+)?(?:ustaw\s+)?(?:ja\s+)?(?:w\s+)?tryb(?:ie)?\s+(?:chlod\w*|grzan\w*|such\w*|auto|wentylator\w*)",
        r"(?:please\s+)?(?:set\s+)?(?:it\s+)?(?:to\s+)?(?:cool|heat|dry|auto|fan only)(?:\s+mode)?",
    )
    return any(re.fullmatch(pattern, normalized_request) for pattern in patterns)


def _explicit_area(normalized_request: str) -> str | None:
    aliases = (
        (("pracowni", "biurze", "office", "study"), "office"),
        (("salonie", "salon", "living room"), "living_room"),
        (("sypialni", "bedroom"), "bedroom"),
        (("kuchni", "kitchen"), "kitchen"),
        (("lazience", "łazience", "bathroom"), "bathroom"),
    )
    for markers, canonical_area in aliases:
        if any(marker in normalized_request for marker in markers):
            return canonical_area
    return None


def _unrecognized_explicit_area(normalized_request: str) -> str | None:
    match = re.search(r"\b(?:w|we)\s+([\w-]+(?:\s+[\w-]+)?)", normalized_request)
    if match is None:
        return None
    area = match.group(1).strip()
    non_area_prefixes = (
        "calym domu",
        "domu",
        "tej chwili",
        "tym momencie",
        "tryb",
        "trybie",
    )
    if area.startswith(non_area_prefixes):
        return None
    return area


def _operation_summary(task: dict[str, Any]) -> str:
    command = task.get("command")
    if not isinstance(command, dict):
        return "unknown"
    operation = command.get("operation")
    if not isinstance(operation, dict):
        return "unknown"
    intent = operation.get("intent")
    description = operation.get("description")
    intent_text = intent if isinstance(intent, str) and intent else "unknown"
    if not isinstance(description, str) or not description:
        return intent_text
    return f"{intent_text}:{_abbreviate(description, 80)}"


def _selection_include_count(task: dict[str, Any]) -> int:
    command = task.get("command")
    if not isinstance(command, dict):
        return 0
    selection = command.get("selection")
    if not isinstance(selection, dict):
        return 0
    include = selection.get("include")
    return len(include) if isinstance(include, list) else 0


def _token_total(prompt_tokens: int | None, completion_tokens: int | None) -> int | None:
    if prompt_tokens is None or completion_tokens is None:
        return None
    return prompt_tokens + completion_tokens


def _abbreviate(text: str, limit: int = 300) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


async def _execute_home_assistant_task(
    tools: HomeAssistantToolSet,
    task: dict[str, Any],
    active_context: dict[str, Any],
) -> dict[str, Any]:
    command = task.get("command")
    if not isinstance(command, dict):
        return _failed_result("Polecenie Home Assistant ma niepoprawny format.")

    selection = command.get("selection")
    operation = command.get("operation")
    if not isinstance(selection, dict) or not isinstance(operation, dict):
        return _failed_result("Polecenie Home Assistant ma niepoprawny format.")

    selected_devices = await _select_devices(tools, selection, active_context)
    if selected_devices["status"] != "ok":
        return selected_devices
    devices = selected_devices["devices"]
    if _is_query_intent(operation):
        return _query_devices_result(operation, devices)
    if not devices:
        return _clarification_result("Które urządzenie mam zmienić?")

    property_request = _property_request(operation, devices)
    if property_request["status"] != "ok":
        return property_request
    property_name = property_request["property_name"]
    value = property_request["value"]

    if _selection_scope(selection) == "all" or len(devices) > 1:
        return await _modify_multiple_devices(tools, operation, devices, property_name, value)
    return await _modify_single_device(tools, operation, devices[0], property_name, value)


async def _select_devices(
    tools: HomeAssistantToolSet,
    selection: dict[str, Any],
    active_context: dict[str, Any],
) -> dict[str, Any]:
    include = selection.get("include", [])
    selector = include[0] if isinstance(include, list) and include and isinstance(include[0], dict) else {}
    selected_domain = _string_or_empty(selector.get("domain"))
    selected_scope = _string_or_empty(selector.get("scope"))
    selected_area = _string_or_empty(selector.get("area"))
    selected_name = _string_or_empty(selector.get("name"))

    if not selected_area and not selected_name:
        context_ref = _most_salient_context_ref(active_context, selected_domain)
        if context_ref is not None:
            selected_domain = context_ref["domain"]
            selected_area = context_ref["area"]

    area_name = "" if selected_scope == "all" else selected_area
    query = selected_name if selected_name and "." not in selected_name else ""
    devices_result = await tools.find_devices(query=query, device_type=selected_domain, area_name=area_name)
    if isinstance(devices_result, dict):
        if selected_area and devices_result.get("error") in {"unknown_area", "ambiguous_area"}:
            return _clarification_result(f"Nie znam obszaru: {selected_area}. Który pokój masz na myśli?")
        return _failed_result("Nie mogę znaleźć urządzeń Home Assistant dla tego polecenia.")
    if not isinstance(devices_result, list):
        return _failed_result("Home Assistant zwrócił niepoprawną listę urządzeń.")

    devices = [_clean_device_mapping(device) for device in devices_result if isinstance(device, dict)]
    devices = [device for device in devices if device is not None]
    devices = _exclude_devices(devices, selection.get("exclude", []))

    if selected_scope != "all" and len(devices) > 1:
        return _clarification_result("Które dokładnie urządzenie mam zmienić?")
    return {"status": "ok", "devices": devices}


async def _modify_single_device(
    tools: HomeAssistantToolSet,
    operation: dict[str, Any],
    device: dict[str, str],
    property_name: str,
    value: Any,
) -> dict[str, Any]:
    properties_result = await tools.list_modifiable_properties(device["name"])
    if isinstance(properties_result, dict):
        return _failed_result("Nie mogę odczytać ustawień tego urządzenia.")
    value = _normalize_property_value(properties_result, property_name, value)
    if not _property_available(properties_result, property_name, value):
        return _clarification_result("To urządzenie nie obsługuje żądanej zmiany.")

    result = await tools.modify_device(device["name"], property_name, value)
    if not isinstance(result, dict) or result.get("status") not in {"ok", "partial"}:
        return _failed_result("Nie udało się zmienić urządzenia Home Assistant.")
    return _success_result(operation, [device], property_name, value, result.get("status", "ok"))


async def _modify_multiple_devices(
    tools: HomeAssistantToolSet,
    operation: dict[str, Any],
    devices: list[dict[str, str]],
    property_name: str,
    value: Any,
) -> dict[str, Any]:
    device_names = [device["name"] for device in devices]
    properties_result = await tools.list_common_modifiable_properties(device_names)
    if not isinstance(properties_result, dict):
        return _failed_result("Nie mogę odczytać wspólnych ustawień tych urządzeń.")
    properties = properties_result.get("common_properties", properties_result.get("properties", []))
    value = _normalize_property_value(properties, property_name, value)
    if not _property_available(properties, property_name, value):
        return _clarification_result("Te urządzenia nie obsługują wspólnej żądanej zmiany.")

    result = await tools.modify_devices(device_names, property_name, value)
    if not isinstance(result, dict) or result.get("status") not in {"ok", "partial"}:
        return _failed_result("Nie udało się zmienić urządzeń Home Assistant.")
    return _success_result(operation, devices, property_name, value, result.get("status", "ok"))


def _property_request(operation: dict[str, Any], devices: list[dict[str, str]]) -> dict[str, Any]:
    intent = _string_or_empty(operation.get("intent"))
    parameters = operation.get("parameters", {})
    parameters = parameters if isinstance(parameters, dict) else {}
    domain = devices[0].get("type", "")

    if intent == "set_temperature":
        temperature = parameters.get("temperature")
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
            return _clarification_result("Na jaką temperaturę mam ustawić urządzenie?")
        return {"status": "ok", "property_name": "target_temperature", "value": temperature}
    if intent == "set_hvac_mode":
        mode = parameters.get("hvac_mode") or parameters.get("mode")
        if not isinstance(mode, str) or not mode:
            return _clarification_result("Jaki tryb pracy mam ustawić?")
        return {"status": "ok", "property_name": "hvac_mode", "value": mode}
    if intent == "turn_off":
        if domain == "climate":
            return {"status": "ok", "property_name": "hvac_mode", "value": "off"}
        return {"status": "ok", "property_name": "on", "value": False}
    if intent == "turn_on":
        if domain == "climate":
            return {"status": "ok", "property_name": "hvac_mode", "value": parameters.get("hvac_mode", "cool")}
        return {"status": "ok", "property_name": "on", "value": True}
    if intent == "set_brightness_percent":
        brightness = parameters.get("brightness_percent", parameters.get("percent"))
        if not isinstance(brightness, (int, float)) or isinstance(brightness, bool):
            return _clarification_result("Na jaką jasność mam ustawić światło?")
        return {"status": "ok", "property_name": "brightness_percent", "value": brightness}
    return _clarification_result("Jaką dokładnie zmianę mam wykonać?")


def _is_query_intent(operation: dict[str, Any]) -> bool:
    return _string_or_empty(operation.get("intent")) in {"query_state", "query_configuration"}


def _query_devices_result(operation: dict[str, Any], devices: list[dict[str, str]]) -> dict[str, Any]:
    query_type = _query_type(operation)
    if query_type == "count":
        text = _device_count_text(devices)
    elif query_type == "list":
        text = _device_list_text(devices)
    else:
        text = _device_summary_text(devices)
    return {
        "status": "ok",
        "text": text,
        "needs_clarification": False,
        "entities": _entity_refs(devices),
        "final_reply_mode": "verbatim",
    }


def _query_type(operation: dict[str, Any]) -> str:
    parameters = operation.get("parameters", {})
    if isinstance(parameters, dict):
        query_type = _string_or_empty(parameters.get("query_type"))
        if query_type in {"count", "list", "state", "configuration"}:
            return query_type
    description = _normalize_lookup(_string_or_empty(operation.get("description")))
    if any(marker in description for marker in ("ile", "how many", "count")):
        return "count"
    if any(marker in description for marker in ("jakie", "ktore", "które", "lista", "list")):
        return "list"
    return "state"


def _device_count_text(devices: list[dict[str, str]]) -> str:
    if not devices:
        return "Nie znalazłem pasujących urządzeń w Home Assistant."
    device_type = devices[0].get("type", "")
    if device_type == "climate":
        return f"W domu są {_polish_count(len(devices), 'klimatyzator', 'klimatyzatory', 'klimatyzatorów')}."
    if device_type == "light":
        return f"Znalazłem {_polish_count(len(devices), 'światło', 'światła', 'świateł')}."
    return f"Znalazłem {len(devices)} pasujących urządzeń."


def _device_list_text(devices: list[dict[str, str]]) -> str:
    if not devices:
        return "Nie znalazłem pasujących urządzeń w Home Assistant."
    names = ", ".join(_device_display_name(device) for device in devices)
    return f"Znalazłem: {names}."


def _device_summary_text(devices: list[dict[str, str]]) -> str:
    if not devices:
        return "Nie znalazłem pasujących urządzeń w Home Assistant."
    if len(devices) == 1:
        return f"Znalazłem {_device_display_name(devices[0])}."
    return _device_list_text(devices)


def _device_display_name(device: dict[str, str]) -> str:
    name = device.get("name", "urządzenie")
    area_name = device.get("area_name", "")
    return f"{name} ({area_name})" if area_name else name


def _polish_count(count: int, singular: str, paucal: str, plural: str) -> str:
    if count == 1:
        return f"{count} {singular}"
    if count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        return f"{count} {paucal}"
    return f"{count} {plural}"


def _property_available(properties: Any, property_name: str, value: Any) -> bool:
    if not isinstance(properties, list):
        return False
    for property_info in properties:
        if not isinstance(property_info, dict) or property_info.get("property_name") != property_name:
            continue
        allowed_values = property_info.get("allowed_values")
        return not isinstance(allowed_values, list) or value in allowed_values
    return False


def _normalize_property_value(properties: Any, property_name: str, value: Any) -> Any:
    if not isinstance(properties, list) or not isinstance(value, str):
        return value
    for property_info in properties:
        if not isinstance(property_info, dict) or property_info.get("property_name") != property_name:
            continue
        return _normalize_value_with_property_info(property_info, value)
    return value


def _normalize_value_with_property_info(property_info: dict[str, Any], value: str) -> Any:
    normalized_value = _normalize_lookup(value)
    allowed_values = property_info.get("allowed_values")
    if isinstance(allowed_values, list):
        for allowed_value in allowed_values:
            if _normalize_lookup(str(allowed_value)) == normalized_value:
                return allowed_value

    value_aliases = property_info.get("value_aliases")
    if not isinstance(value_aliases, dict):
        return value

    for raw_value, raw_aliases in value_aliases.items():
        aliases = raw_aliases if isinstance(raw_aliases, list) else []
        candidates = [str(raw_value), *(alias for alias in aliases if isinstance(alias, str))]
        if normalized_value not in {_normalize_lookup(candidate) for candidate in candidates}:
            continue
        if isinstance(allowed_values, list):
            for allowed_value in allowed_values:
                if _normalize_lookup(str(allowed_value)) == _normalize_lookup(str(raw_value)):
                    return allowed_value
        return raw_value
    return value


def _normalize_lookup(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold()).replace("ł", "l")
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _success_result(
    operation: dict[str, Any],
    devices: list[dict[str, str]],
    property_name: str,
    value: Any,
    status: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "text": _success_text(operation, devices, property_name, value),
        "needs_clarification": False,
        "entities": _entity_refs(devices),
    }


def _success_text(operation: dict[str, Any], devices: list[dict[str, str]], property_name: str, value: Any) -> str:
    intent = _string_or_empty(operation.get("intent"))
    area_name = devices[0].get("area_name", "")
    target_text = _target_text(devices)
    if intent == "set_temperature":
        return f"Ustawiłem {target_text} na {value} stopni."
    if intent == "set_hvac_mode" and property_name == "hvac_mode" and value == "fan_only":
        return f"Ustawiłem {target_text} w trybie wentylacji."
    if intent == "turn_off":
        return f"Wyłączyłem {target_text}."
    if intent == "turn_on":
        return f"Włączyłem {target_text}."
    if area_name:
        return f"Wykonałem zmianę {target_text}."
    return "Wykonałem zmianę w Home Assistant."


def _target_text(devices: list[dict[str, str]]) -> str:
    if len(devices) > 1:
        device_type = devices[0].get("type", "urządzenia")
        if device_type == "climate":
            return "wszystkie klimatyzacje"
        if device_type == "light":
            return "wszystkie światła"
        return "wybrane urządzenia"
    device = devices[0]
    area_name = _area_display_name(device)
    device_type = device.get("type", "")
    if device_type == "climate" and area_name:
        return f"klimatyzację w {area_name}"
    if device_type == "light" and area_name:
        return f"światło w {area_name}"
    return device.get("name", "urządzenie")


def _area_display_name(device: dict[str, str]) -> str:
    area_id = device.get("area_id", "")
    if area_id == "office":
        return "biurze"
    if area_id == "living_room":
        return "salonie"
    if area_id == "bedroom":
        return "sypialni"
    return device.get("area_name", "")


def _entity_refs(devices: list[dict[str, str]]) -> list[str]:
    refs = []
    for device in devices:
        device_type = device.get("type")
        area_id = device.get("area_id")
        if device_type and area_id:
            refs.append(f"{device_type}.{area_id}")
    return refs


def _clean_device_mapping(device: dict[str, Any]) -> dict[str, str] | None:
    name = device.get("name")
    if not isinstance(name, str) or not name:
        return None
    return {
        "name": name,
        "type": _string_or_empty(device.get("type")),
        "area_id": _string_or_empty(device.get("area_id")),
        "area_name": _string_or_empty(device.get("area_name")),
    }


def _exclude_devices(devices: list[dict[str, str]], raw_excludes: Any) -> list[dict[str, str]]:
    if not isinstance(raw_excludes, list):
        return devices
    excluded = devices
    for raw_exclude in raw_excludes:
        if not isinstance(raw_exclude, dict):
            continue
        excluded = [device for device in excluded if not _device_matches_selector(device, raw_exclude)]
    return excluded


def _device_matches_selector(device: dict[str, str], selector: dict[str, Any]) -> bool:
    expected_name = _string_or_empty(selector.get("name"))
    expected_domain = _string_or_empty(selector.get("domain"))
    expected_area = _string_or_empty(selector.get("area"))
    if expected_name and _normalized(expected_name) in {_normalized(device["name"])}:
        return True
    if expected_domain and expected_domain != device.get("type"):
        return False
    if expected_area and _normalized(expected_area) not in {_normalized(device.get("area_id", "")), _normalized(device.get("area_name", ""))}:
        return False
    return bool(expected_domain or expected_area)


def _selection_scope(selection: dict[str, Any]) -> str:
    include = selection.get("include", [])
    selector = include[0] if isinstance(include, list) and include and isinstance(include[0], dict) else {}
    return _string_or_empty(selector.get("scope"))


def _most_salient_context_ref(
    active_context: dict[str, Any],
    domain: str,
) -> dict[str, str] | None:
    if not isinstance(active_context, dict):
        return None
    for entity in active_context.get("salient_entities", []):
        if not isinstance(entity, str) or entity.count(".") != 1:
            continue
        entity_domain, area = entity.split(".", 1)
        if not entity_domain or not area:
            continue
        if domain and entity_domain != domain:
            return None
        return {"domain": entity_domain, "area": area}
    return None


def _failed_result(text: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "text": text,
        "needs_clarification": False,
        "entities": [],
    }


def _clarification_result(question: str) -> dict[str, Any]:
    return {
        "status": "needs_clarification",
        "text": question,
        "needs_clarification": True,
        "clarification_question": question,
        "entities": [],
    }


def _string_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _normalized(value: str) -> str:
    return value.strip().casefold()


def _execution_hints(task: DomainTask, active_context: dict[str, Any]) -> list[str]:
    command = task.get("command")
    if not isinstance(command, dict):
        return []
    selection = command.get("selection")
    operation = command.get("operation")
    if not isinstance(selection, dict) or not isinstance(operation, dict):
        return []

    hints = []
    include = selection.get("include", [])
    selector = include[0] if isinstance(include, list) and include and isinstance(include[0], dict) else {}
    selected_domain = selector.get("domain")
    selected_scope = selector.get("scope")
    selected_area = selector.get("area")
    selected_name = selector.get("name")
    if isinstance(selected_scope, str) and selected_scope == "all":
        hints.append("Selection scope is all/global. Do not restrict to conversation.area or current area.")
        if isinstance(selected_domain, str) and selected_domain:
            hints.append(f"Find every matching device with device_type={selected_domain!r} and empty area_name.")
    elif isinstance(selected_area, str) and selected_area:
        hints.append(f"Selection targets area {selected_area!r}. Use area_name={selected_area!r} when finding devices.")
    elif isinstance(selected_name, str) and selected_name:
        hints.append(f"Selection targets device/entity/name {selected_name!r}. Resolve that name before modifying.")

    if isinstance(active_context, dict):
        for entity in active_context.get("salient_entities", []):
            if isinstance(entity, str) and entity.count(".") == 1:
                domain, area = entity.split(".", 1)
                if domain and area:
                    hints.append(
                        f"Context reference {entity!r} means device type {domain!r} in area {area!r}; "
                        "do not pass it directly as a device id."
                    )

    intent = operation.get("intent")
    parameters = operation.get("parameters", {})
    if intent == "set_temperature":
        temperature = parameters.get("temperature") if isinstance(parameters, dict) else None
        hints.append("For set_temperature, modify property_name='target_temperature'.")
        if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
            hints.append(f"Use value={temperature!r}.")
    if intent == "set_hvac_mode":
        hints.append("For set_hvac_mode, modify property_name='hvac_mode'.")
        if isinstance(parameters, dict):
            mode = parameters.get("hvac_mode") or parameters.get("mode")
            if isinstance(mode, str) and mode:
                hints.append(f"Use requested HVAC mode {mode!r}; for ventilation aliases prefer 'fan_only' when available.")
    if intent == "turn_off":
        hints.append("For turn_off, prefer property_name='hvac_mode' value='off' for climate devices when available; otherwise use property_name='on' value=false.")
    if intent == "turn_on":
        hints.append("For turn_on, prefer property_name='hvac_mode' value='cool' for climate devices when available; otherwise use property_name='on' value=true.")

    return hints


def _parse_domain_reply(reply_text: str) -> dict[str, Any]:
    try:
        raw_reply = json.loads(reply_text)
    except json.JSONDecodeError as exc:
        raise ValueError("Home Assistant DSA reply must be valid JSON") from exc
    if not isinstance(raw_reply, dict):
        raise ValueError("Home Assistant DSA reply must be a JSON object")

    status = raw_reply.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("Home Assistant DSA reply status must be a non-empty string")

    text = raw_reply.get("text", "")
    if not isinstance(text, str):
        raise ValueError("Home Assistant DSA reply text must be a string")

    needs_clarification = raw_reply.get("needs_clarification", status == "needs_clarification")
    if not isinstance(needs_clarification, bool):
        raise ValueError("Home Assistant DSA reply needs_clarification must be a boolean")

    clarification_question = raw_reply.get("clarification_question")
    if clarification_question is not None and not isinstance(clarification_question, str):
        raise ValueError("Home Assistant DSA reply clarification_question must be a string or null")

    entities = raw_reply.get("entities", [])
    if not isinstance(entities, list):
        raise ValueError("Home Assistant DSA reply entities must be a list")

    return {
        "status": status,
        "text": text,
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
        "entities": entities,
    }
