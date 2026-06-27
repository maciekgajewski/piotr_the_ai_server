from __future__ import annotations

import asyncio
from typing import Any

from ai_server.agent_loop import AgentLoop, AgentLoopConfig
from ai_server.ai_tools.interfaces import BaseTool
from ai_server.config import AgentConfig
from ai_server.home_assistant import HomeAssistantConnection, parse_home_assistant_options
from ai_server.home_assistant.toolset import HomeAssistantToolSet
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import ProcessingUpdate, RequestFollowUp, TextMessage


class HomeAssistantTool(BaseTool, HomeAssistantToolSet):
    name = "home_assistant"
    description = (
        "A tool for controlling smart home devices. Use this for any queries related to smart home control, "
        "air conditioning, lighting, etc."
    )

    def __init__(
        self,
        config: AgentConfig,
        connection: HomeAssistantConnection | None = None,
        processing_update_interval_seconds: float = 5.0,
    ) -> None:
        BaseTool.__init__(self, config)
        self._owns_connection = connection is None
        connection = connection or HomeAssistantConnection(parse_home_assistant_options(config.options))
        HomeAssistantToolSet.__init__(self, connection, logger_name=f"{self.__module__}.{type(self).__name__}[{self.name}]")
        self._agent_loop_model = _parse_agent_loop_model(config.options)
        self._agent_loop_fallback_model = _parse_agent_loop_fallback_model(config.options)
        self._agent_loop_fallback_backoff_seconds = _parse_agent_loop_fallback_backoff_seconds(config.options)
        self._ollama_url = _parse_ollama_url(config.options)
        self._processing_update_interval_seconds = processing_update_interval_seconds
        self._start_task: asyncio.Task[None] | None = None
        self._logger.debug(
            "configured HomeAssistantTool agent_loop_cloud_model=%s agent_loop_local_model=%s ollama_url=%s owns_connection=%s",
            self._agent_loop_model,
            self._agent_loop_fallback_model,
            self._ollama_url,
            self._owns_connection,
        )
        self._start_owned_connection()

    async def run(self, conversation: Conversation, endpoint: ConversationEndpoint, request: TextMessage) -> None:
        self._start_owned_connection()
        system_prompt = self._connection.system_prompt_context(user=conversation.user, area=conversation.area)
        self._logger.debug(
            "starting Home Assistant conversation conversation_id=%s user=%s area=%s initial_request=%r inventory_ready=%s system_prompt=%r",
            conversation.conversation_id,
            conversation.user,
            conversation.area,
            request.text,
            self._connection.inventory is not None,
            system_prompt,
        )
        loop_config = AgentLoopConfig(
            model=self._agent_loop_model,
            ollama_url=self._ollama_url,
            fallback_model=self._agent_loop_fallback_model,
            fallback_backoff_seconds=self._agent_loop_fallback_backoff_seconds,
        )

        async with AgentLoop(
            config=loop_config,
            system_prompt=system_prompt,
            tools=self,
            processing_update_callback=lambda: endpoint.send(ProcessingUpdate()),
            processing_update_interval_seconds=self._processing_update_interval_seconds,
        ) as loop:
            self.set_request_context(user_message=request.text, area=conversation.area)
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

            await endpoint.send(RequestFollowUp())
            async for follow_up in endpoint.messages():
                self._logger.debug(
                    "received Home Assistant follow-up conversation_id=%s message=%r",
                    conversation.conversation_id,
                    follow_up.text,
                )
                self.set_request_context(user_message=follow_up.text, area=conversation.area)
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
                await endpoint.send(RequestFollowUp())
        self._logger.debug("Home Assistant conversation input stream ended conversation_id=%s", conversation.conversation_id)

    async def close(self) -> None:
        if self._start_task is not None:
            try:
                await self._start_task
            except asyncio.CancelledError:
                pass
        if self._owns_connection:
            await self._connection.close()

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


def _parse_agent_loop_fallback_model(options: dict[str, Any]) -> str | None:
    fallback_model = options.get("fallback_model")
    if fallback_model is None:
        return None
    if not isinstance(fallback_model, str) or not fallback_model:
        raise ValueError("agent.fallback_model must be a non-empty string for HomeAssistantTool when provided")
    return fallback_model


def _parse_agent_loop_fallback_backoff_seconds(options: dict[str, Any]) -> float:
    fallback_backoff_seconds = options.get("fallback_backoff_seconds", 300.0)
    if not isinstance(fallback_backoff_seconds, (int, float)) or isinstance(fallback_backoff_seconds, bool) or fallback_backoff_seconds <= 0:
        raise ValueError("agent.fallback_backoff_seconds must be a positive number for HomeAssistantTool")
    return float(fallback_backoff_seconds)


def _parse_ollama_url(options: dict[str, Any]) -> str:
    ollama_url = options.get("ollama_url")
    if not isinstance(ollama_url, str) or not ollama_url:
        raise ValueError("agent.ollama_url must be a non-empty string for HomeAssistantTool")
    return ollama_url
