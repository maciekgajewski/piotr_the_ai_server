from __future__ import annotations

import logging

from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import ConversationEnded


class EchoAgent:
    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        logger = logging.getLogger(f"{__name__}.EchoAgent[{conversation.conversation_id}]")
        while True:
            logger.debug("echoing streaming message")
            try:
                event = await endpoint.receive()
            except ConversationEnded:
                return
            await endpoint.send(event)

    async def close(self) -> None:
        pass
