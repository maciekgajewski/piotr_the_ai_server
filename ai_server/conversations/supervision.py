from __future__ import annotations

import logging

from ai_server.conversations.bridge import BridgeSettings, FatalTerminationController, bridge_conversation
from ai_server.conversations.context_provider import ContextProvider
from ai_server.conversations.interfaces import Agent, InputAdapter
from ai_server.conversations.messages import InputSessionClosed


async def supervise_input(
    *,
    input_adapter: InputAdapter,
    agent: Agent,
    context_provider: ContextProvider,
    bridge_settings: BridgeSettings,
    fatal_termination: FatalTerminationController | None = None,
) -> None:
    logger = logging.getLogger(f"{__name__}.InputSupervisor")
    async with input_adapter.open_session() as input_session:
        while not input_session.closed:
            try:
                async with input_session.accept_conversation() as input_conversation:
                    logger.info(
                        "conversation accepted conversation_id=%s input_session_id=%s medium=%s user=%r area=%r",
                        input_conversation.context.conversation_id,
                        input_conversation.context.input_session_id,
                        input_conversation.context.medium.value,
                        input_conversation.context.user,
                        input_conversation.context.area,
                    )
                    await bridge_conversation(
                        input_conversation=input_conversation,
                        agent=agent,
                        context_provider=context_provider,
                        settings=bridge_settings,
                        fatal_termination=fatal_termination,
                    )
            except InputSessionClosed:
                break
