from __future__ import annotations

import datetime as dt
import locale

from ai_server.ai_tools.interfaces import BaseTool
from ai_server.interfaces import CommunicationEndpoint
from ai_server.messages import UserMessage
from ai_server.streaming import send_user_message

GENERATION_OPTIONS = {
    "num_predict": 32,
    "temperature": 0,
    "num_ctx": 1024,
}


class TimeTool(BaseTool):
    name = "time"
    description = "A tool for providing the current time, date or day of week. Use this for any time-related queries."

    async def run(self, endpoint: CommunicationEndpoint, request: UserMessage) -> None:
        current_locale = locale.setlocale(locale.LC_TIME)

        try:
            try:
                locale.setlocale(locale.LC_TIME, "pl_PL.utf8")
            except locale.Error:
                pass

            current_time = dt.datetime.now().strftime("%A, %d. %B %Y %H:%M")

            prompt = (
                f"Jest {current_time}. Odpoiwiedz krótko, po Polsku na pytanie uwzględniając aktualny czas. "
                f"Jeśli pytanie nie jest związane z czasem, odpowiedz 'Nie wiem'. "
                f"Pytanie: {request.text}"
            )

            response = await self._ollama.chat(
                {
                    "model": self._config.options["intent_router_model"],
                    "raw": False,
                    "think": False,
                    "stream": False,
                    "keep_alive": "1h",
                    "options": GENERATION_OPTIONS,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                }
            )

            reply = response["message"]
            assert reply["role"] == "assistant"
            content = reply["content"]

            await send_user_message(endpoint, UserMessage(text=content))
        finally:
            locale.setlocale(locale.LC_TIME, current_locale)
