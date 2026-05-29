from __future__ import annotations

import asyncio
from typing import Annotated, Any

from ai_server.agent_loop import AgentCallableSet, AgentLoop, AgentLoopConfig
from ai_server.ai_tools.interfaces import BaseTool
from ai_server.config import AgentConfig
from ai_server.home_assistant import HomeAssistantConnection, JsonScalar, parse_home_assistant_options
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import TextMessage


class HomeAssistantTool(BaseTool, AgentCallableSet):
    name = "home_assistant"
    description = (
        "A tool for controlling smart home devices. Use this for any queries related to smart home control, "
        "air conditioning, lighting, etc."
    )

    def __init__(self, config: AgentConfig, connection: HomeAssistantConnection | None = None) -> None:
        super().__init__(config)
        self._connection = connection or HomeAssistantConnection(parse_home_assistant_options(config.options))
        self._owns_connection = connection is None
        self._agent_loop_model = _parse_agent_loop_model(config.options)
        self._ollama_url = _parse_ollama_url(config.options)
        self._start_task: asyncio.Task[None] | None = None
        self._current_user_message = ""
        self._current_location: str | None = None
        self._logger.debug(
            "configured HomeAssistantTool agent_loop_model=%s ollama_url=%s owns_connection=%s",
            self._agent_loop_model,
            self._ollama_url,
            self._owns_connection,
        )
        self._start_owned_connection()

    async def run(self, conversation: Conversation, endpoint: ConversationEndpoint, request: TextMessage) -> None:
        self._start_owned_connection()
        system_prompt = self._connection.system_prompt_context(user=conversation.user, location=conversation.location)
        self._logger.debug(
            "starting Home Assistant conversation conversation_id=%s user=%s location=%s initial_request=%r inventory_ready=%s system_prompt=%r",
            conversation.conversation_id,
            conversation.user,
            conversation.location,
            request.text,
            self._connection.inventory is not None,
            system_prompt,
        )
        loop_config = AgentLoopConfig(
            model=self._agent_loop_model,
            ollama_url=self._ollama_url,
        )

        async with AgentLoop(config=loop_config, system_prompt=system_prompt, tools=self) as loop:
            self.set_request_context(user_message=request.text, location=conversation.location)
            reply = await loop.send_user_message(request.text)
            self._logger.debug(
                "sending Home Assistant reply conversation_id=%s reply=%r end_conversation=%s loop_eval_count=%s",
                conversation.conversation_id,
                reply.reply_text,
                reply.end_conversation,
                loop.eval_count,
            )
            await endpoint.send_message(TextMessage(text=reply.reply_text))
            if reply.end_conversation:
                self._logger.debug("ending Home Assistant conversation after initial reply conversation_id=%s", conversation.conversation_id)
                return

            async for follow_up in endpoint.messages():
                self._logger.debug(
                    "received Home Assistant follow-up conversation_id=%s message=%r",
                    conversation.conversation_id,
                    follow_up.text,
                )
                self.set_request_context(user_message=follow_up.text, location=conversation.location)
                reply = await loop.send_user_message(follow_up.text)
                self._logger.debug(
                    "sending Home Assistant follow-up reply conversation_id=%s reply=%r end_conversation=%s loop_eval_count=%s",
                    conversation.conversation_id,
                    reply.reply_text,
                    reply.end_conversation,
                    loop.eval_count,
                )
                await endpoint.send_message(TextMessage(text=reply.reply_text))
                if reply.end_conversation:
                    self._logger.debug("ending Home Assistant conversation after follow-up conversation_id=%s", conversation.conversation_id)
                    return
        self._logger.debug("Home Assistant conversation input stream ended conversation_id=%s", conversation.conversation_id)

    async def close(self) -> None:
        if self._start_task is not None:
            try:
                await self._start_task
            except asyncio.CancelledError:
                pass
        if self._owns_connection:
            await self._connection.close()

    def set_request_context(self, *, user_message: str, location: str | None) -> None:
        self._current_user_message = user_message
        self._current_location = location

    @AgentCallableSet.tool(
        description=(
            "Inspect only: list controllable Home Assistant devices in an area or room. "
            "This does not modify anything. For an action request, call modify_device after selecting the device."
        )
    )
    async def list_devices(
        self,
        area_name: Annotated[str, "Area id, room name, or any Home Assistant-provided room alias."],
    ) -> list[dict[str, Any]] | dict[str, Any]:
        self._logger.debug("list_devices called area_name=%r", area_name)
        return await self._connection.list_devices(area_name)

    @AgentCallableSet.tool(
        description="Find controllable Home Assistant devices by type, alias, area, or query. Omit area to search globally."
    )
    async def find_devices(
        self,
        query: Annotated[str, "Optional search text matched against device, entity, alias, type, and area names."] = "",
        device_type: Annotated[str, "Optional Home Assistant device type/domain such as climate, light, switch, fan, or cover."] = "",
        area_name: Annotated[str, "Optional area id, room name, or room alias."] = "",
    ) -> list[dict[str, Any]] | dict[str, Any]:
        self._logger.debug("find_devices called query=%r device_type=%r area_name=%r", query, device_type, area_name)
        return await self._connection.find_devices(query=query, device_type=device_type, area_name=area_name)

    @AgentCallableSet.tool(
        description=(
            "Inspect only: list modifiable properties for a Home Assistant device. "
            "This does not modify anything. If the requested property and value are known, the next step must be modify_device."
        )
    )
    async def list_modifiable_properties(
        self,
        device: Annotated[str, "Device id, entity id, device name, entity name, or any entity alias."],
    ) -> list[dict[str, Any]] | dict[str, Any]:
        self._logger.debug("list_modifiable_properties called device=%r", device)
        return await self._connection.list_modifiable_properties(device)

    @AgentCallableSet.tool(description="Inspect common modifiable properties shared by multiple devices.")
    async def list_common_modifiable_properties(
        self,
        devices: Annotated[list[str], "Device ids, entity ids, device names, entity names, or aliases."],
    ) -> dict[str, Any]:
        self._logger.debug("list_common_modifiable_properties called devices=%r", devices)
        return await self._connection.list_common_modifiable_properties(devices)

    @AgentCallableSet.tool(description="Modify one Home Assistant device property. This is the only tool that changes devices.")
    async def modify_device(
        self,
        device: Annotated[str, "Device id, entity id, device name, entity name, or any entity alias."],
        property_name: Annotated[str, "Property name returned by list_modifiable_properties."],
        value: Annotated[JsonScalar, "Desired property value. Common aliases are accepted."],
    ) -> dict[str, Any]:
        self._logger.debug("modify_device called device=%r property_name=%r value=%r", device, property_name, value)
        return await self._connection.modify_device(device, property_name, value)

    @AgentCallableSet.tool(
        description=(
            "Modify the same property on multiple Home Assistant devices. "
            "Use only when the user explicitly asks for all/every matching devices or all target devices are in the requested room."
        )
    )
    async def modify_devices(
        self,
        devices: Annotated[list[str], "Device ids, entity ids, device names, entity names, or aliases."],
        property_name: Annotated[str, "Property name shared by the target devices."],
        value: Annotated[JsonScalar, "Desired property value. Common aliases are accepted."],
    ) -> dict[str, Any]:
        self._logger.debug("modify_devices called devices=%r property_name=%r value=%r", devices, property_name, value)
        result = await self._connection.modify_devices(
            devices,
            property_name,
            value,
            user_message=self._current_user_message,
            current_location=self._current_location,
        )
        if result.get("status") == "rejected":
            raise ValueError(_batch_rejection_message(result, property_name, value))
        return result

    def _start_owned_connection(self) -> None:
        if not self._owns_connection:
            return
        if self._start_task is not None and not self._start_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._start_task = asyncio.create_task(self._connection.start())


def _parse_agent_loop_model(options: dict[str, Any]) -> str:
    agent_loop_model = options.get("model")
    if not isinstance(agent_loop_model, str) or not agent_loop_model:
        raise ValueError("agent.model must be a non-empty string for HomeAssistantTool")
    return agent_loop_model


def _parse_ollama_url(options: dict[str, Any]) -> str:
    ollama_url = options.get("ollama_url")
    if not isinstance(ollama_url, str) or not ollama_url:
        raise ValueError("agent.ollama_url must be a non-empty string for HomeAssistantTool")
    return ollama_url


def _batch_rejection_message(result: dict[str, Any], property_name: str, value: JsonScalar) -> str:
    base_message = result.get("message")
    if not isinstance(base_message, str) or not base_message:
        base_message = "Batch modification rejected."

    current_area = result.get("current_area")
    current_area_id = current_area.get("area_id") if isinstance(current_area, dict) else None
    rejected_devices = result.get("rejected_devices")
    if isinstance(current_area_id, str) and isinstance(rejected_devices, list):
        matching_devices = [
            device
            for device in rejected_devices
            if isinstance(device, dict) and device.get("area_id") == current_area_id and isinstance(device.get("name"), str)
        ]
        if len(matching_devices) == 1:
            device_name = matching_devices[0]["name"]
            return (
                f"{base_message} Your next response must be a tool call, not text: "
                f'call modify_device(device="{device_name}", property_name="{property_name}", value={value!r}).'
            )

    return f"{base_message} Your next response must be a narrower modify_device tool call or a clarification question, not a success message."
