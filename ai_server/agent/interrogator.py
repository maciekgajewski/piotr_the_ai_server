from __future__ import annotations

import logging

from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import MessageBegin, MessageEnd, MessageFragment


class InterrogatorAgent:
    async def run_conversation(self, conversation: Conversation, endpoint: ConversationEndpoint) -> None:
        logger = logging.getLogger(f"{__name__}.InterrogatorAgent[{conversation.conversation_id}]")
        message_number = 0
        async for message in endpoint.messages():
            message_number += 1
            logger.debug("replying to message_number=%s", message_number)
            if message.text == "koniec":
                await _send_streamed_text(
                    endpoint,
                    f"Koniec konwersacji, wysłałeś {message_number} wiadomości.",
                )
                return

            await _send_streamed_text(
                endpoint,
                f"Twoja wiadomość numer {message_number} to: {message.text}",
            )

    async def close(self) -> None:
        pass


async def _send_streamed_text(endpoint: ConversationEndpoint, text: str) -> None:
    await endpoint.send(MessageBegin())
    midpoint = len(text) // 2
    await endpoint.send(MessageFragment(text=text[:midpoint]))
    await endpoint.send(MessageFragment(text=text[midpoint:]))
    await endpoint.send(MessageEnd())
