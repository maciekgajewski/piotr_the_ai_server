from __future__ import annotations

import logging
import time

from aiohttp import ClientSession

from ai_server.interfaces import CommunicationEndpoint
from ai_server.messages import UserMessage
from ai_server.ollama import OLLAMA_BASE_URL, OllamaClient, OllamaError
from ai_server.streaming import receive_user_message, send_user_message


POLITE_REPLY_PROMPT = (
    "Jesteś pijanym wrednym chamem, masz na imię Ryszard. Odpowiedz uyżytkownikowi w najbardziej chamski sposób, użyj wielu wulgaryzmów. Zawrzyj część pytania w swojej odpiwedzi.\n"
    "Użytkownik: {user_input}\n"
    "Ryszard:"
)
GENERATION_OPTIONS = {
    "num_predict": 48,
    "temperature": 0,
    "stop": ["\n"],
}
GENERATION_FAILURE_MESSAGE = "Przepraszam, nie mogę teraz odpowiedzieć."


class PoliteReplyAgent:
    def __init__(
        self,
        model: str,
        base_url: str = OLLAMA_BASE_URL,
        session: ClientSession | None = None,
        ollama_client: OllamaClient | None = None,
        owns_ollama_client: bool = True,
    ) -> None:
        self._model = model
        self._ollama = ollama_client or OllamaClient(base_url=base_url, session=session)
        self._owns_ollama = owns_ollama_client
        self._logger = logging.getLogger(f"{__name__}.PoliteReplyAgent[{model}]")

    async def preload(self) -> None:
        try:
            await self._ollama.generate(
                {
                    "model": self._model,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": -1,
                }
            )
        except Exception as exc:
            raise OllamaError(f"failed to preload Ollama model {self._model}") from exc

    async def run(self, endpoint: CommunicationEndpoint, session_id: str) -> None:
        logger = logging.getLogger(f"{__name__}.PoliteReplyAgent[{session_id}]")
        while True:
            message = await receive_user_message(endpoint)
            started_at = time.perf_counter()

            try:
                reply = await self._generate_reply(message.text)
            except Exception:
                elapsed_ms = _elapsed_ms(started_at)
                logger.exception(
                    "generation failed request_len=%s duration_ms=%s",
                    len(message.text),
                    elapsed_ms,
                )
                await send_user_message(endpoint, UserMessage(text=GENERATION_FAILURE_MESSAGE))
                continue

            elapsed_ms = _elapsed_ms(started_at)
            logger.debug(
                "request_len=%s reply_len=%s duration_ms=%s",
                len(message.text),
                len(reply),
                elapsed_ms,
            )
            await send_user_message(endpoint, UserMessage(text=reply))

    async def close(self) -> None:
        if self._owns_ollama:
            await self._ollama.close()

    async def _generate_reply(self, user_input: str) -> str:
        response = await self._ollama.generate(
            {
                "model": self._model,
                "raw": True,
                "prompt": POLITE_REPLY_PROMPT.format(user_input=user_input),
                "stream": False,
                "options": GENERATION_OPTIONS,
            }
        )

        reply = response.get("response")
        if not isinstance(reply, str):
            raise OllamaError("Ollama response missing string response field")

        return _strip_thinking(reply)


def _elapsed_ms(started_at: float) -> int:
    return round((time.perf_counter() - started_at) * 1000)


def _strip_thinking(reply: str) -> str:
    end_tag = "</think>"
    if end_tag not in reply:
        return reply.strip()

    return reply.split(end_tag, maxsplit=1)[1].strip()
