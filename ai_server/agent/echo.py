from __future__ import annotations

import logging

from ai_server.conversations.agent_runtime import AgentChannel, ConversationAgent
from ai_server.conversations.contexts import ConversationContext


class EchoAgent(ConversationAgent):
    async def run_agent_conversation(self, context: ConversationContext, channel: AgentChannel) -> None:
        logger = logging.getLogger(f"{__name__}.EchoAgent[{context.conversation_id}]")
        message = await channel.receive_user_message()
        logger.debug("echoing message")
        await channel.send_message(message.text)
        await channel.end_conversation()

    async def close(self) -> None:
        pass
