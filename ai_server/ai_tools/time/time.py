from __future__ import annotations

import datetime as dt
import locale

from ai_server.ai_tools.interfaces import BaseTool
from ai_server.interfaces import Conversation, ConversationEndpoint
from ai_server.messages import TextMessage
from ai_server.ollama_client import OllamaClient

GENERATION_OPTIONS = {
    "num_predict": 32,
    "temperature": 0,
    "num_ctx": 1024,
}


class TimeTool(BaseTool):
    name = "time"
    description = "A tool for providing the current time, date or day of week. Use this for any time-related queries."

    def __init__(self, config) -> None:
        super().__init__(config)
        ollama_url = self._config.options.get("ollama_url")
        if not isinstance(ollama_url, str) or not ollama_url:
            raise ValueError("agent.ollama_url must be a non-empty string for TimeTool")
        self._ollama_url = ollama_url
        self._ollama: OllamaClient | None = None

    async def run(self, conversation: Conversation, endpoint: ConversationEndpoint, request: TextMessage) -> None:
        current_locale = locale.setlocale(locale.LC_TIME)

        try:
            try:
                locale.setlocale(locale.LC_TIME, "pl_PL.utf8")
            except locale.Error:
                self._logger.error("failed to set locale pl_PL.utf8")

            current_time = dt.datetime.now().strftime("%A, %d. %B %Y %H:%M")

            prompt = (
                f"Jest {current_time}. Odpoiwiedz krótko, po Polsku na pytanie uwzględniając aktualny czas. "
                f"Jeśli pytanie nie jest związane z czasem, odpowiedz 'Nie wiem'. "
                f"Pytanie: {request.text}"
            )

            response = await self._ollama_client().chat(
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

            await endpoint.send_message(TextMessage(text=content))
        finally:
            locale.setlocale(locale.LC_TIME, current_locale)

    async def close(self) -> None:
        if self._ollama is not None:
            await self._ollama.close()
            self._ollama = None

    def _ollama_client(self) -> OllamaClient:
        if self._ollama is None:
            self._ollama = OllamaClient(base_url=self._ollama_url)
        return self._ollama
