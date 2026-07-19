from __future__ import annotations

import logging
from ai_server.conversations.agent_runtime import AgentChannel, ConversationAgent
from ai_server.conversations.contexts import ConversationContext


class InterrogatorAgent(ConversationAgent):
    async def run_agent_conversation(self, context: ConversationContext, channel: AgentChannel) -> None:
        logger = logging.getLogger(f"{__name__}.InterrogatorAgent[{context.conversation_id}]")
        message_number = 0
        while True:
            message = await channel.receive_user_message()
            message_number += 1
            logger.debug("replying to message_number=%s", message_number)
            if message.text == "koniec":
                await _send_streamed_text(
                    channel,
                    f"Koniec konwersacji, wysłałeś {message_number} wiadomości.",
                )
                await channel.end_conversation()
                return

            await _send_streamed_text(
                channel,
                f"Twoja wiadomość numer {message_number} to: {message.text}",
            )
            await channel.request_follow_up()

    async def close(self) -> None:
        pass


async def _send_streamed_text(channel: AgentChannel, text: str) -> None:
    await channel.start_assistant_message()
    midpoint = len(text) // 2
    await channel.send_text(text[:midpoint])
    await channel.send_text(text[midpoint:])
    await channel.complete_assistant_message()
