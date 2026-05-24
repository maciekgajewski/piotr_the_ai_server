from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession

from ai_server.ai_tools.interfaces import BaseTool
from ai_server.config import AgentConfig
from ai_server.interfaces import CommunicationEndpoint
from ai_server.messages import UserMessage
from ai_server.ollama import OllamaClient
from ai_server.streaming import send_user_message


HOME_ASSISTANT_LANGUAGE = "pl"
HOME_ASSISTANT_CONVERSATION_PATH = "/api/conversation/process"
HOME_ASSISTANT_FAILURE_REPLY = "Przepraszam, nie udało mi się połączyć z Home Assistant."


class HomeAssistantTool(BaseTool):
    name = "home_assistant"
    description = (
        "A tool for controlling smart home devices. Use this for any queries related to smart home control, "
        "air conditioning, lighting, etc."
    )

    def __init__(self, config: AgentConfig, ollama_client: OllamaClient) -> None:
        super().__init__(config, ollama_client)
        self._home_assistant = _parse_home_assistant_options(self._config.options)

    async def run(self, endpoint: CommunicationEndpoint, request: UserMessage) -> None:
        self._logger.info("incoming request text=%r", request.text)
        try:
            response_body = await _process_conversation(
                url=self._home_assistant.url,
                token=self._home_assistant.token,
                text=request.text,
                logger=self._logger,
            )
            reply = _extract_response_text(response_body)
        except Exception:
            self._logger.exception("Home Assistant conversation request failed")
            reply = HOME_ASSISTANT_FAILURE_REPLY

        await send_user_message(endpoint, UserMessage(text=reply))


@dataclass(frozen=True)
class HomeAssistantOptions:
    url: str
    token: str


def _parse_home_assistant_options(options: dict[str, Any]) -> HomeAssistantOptions:
    raw_options = options.get("home_assistant")
    if not isinstance(raw_options, dict):
        raise ValueError("agent.home_assistant must be a mapping")

    url = raw_options.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("agent.home_assistant.url must be a non-empty string")

    token = raw_options.get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("agent.home_assistant.token must be a non-empty string")

    return HomeAssistantOptions(url=url.rstrip("/"), token=token)


async def _process_conversation(url: str, token: str, text: str, logger) -> dict[str, Any]:
    payload = {
        "text": text,
        "language": HOME_ASSISTANT_LANGUAGE,
    }
    headers = {
        "Authorization": f"Bearer {token}",
    }

    async with ClientSession(headers=headers) as session:
        request_url = f"{url}{HOME_ASSISTANT_CONVERSATION_PATH}"
        logger.debug("Home Assistant HTTP request method=POST url=%s json=%s", request_url, payload)
        async with session.post(request_url, json=payload) as response:
            status = response.status
            body = await response.json()
            logger.debug("Home Assistant HTTP response status=%s json=%s", status, body)
            if response.status >= 400:
                raise ClientError(f"Home Assistant conversation failed with status {response.status}")

            if not isinstance(body, dict):
                raise ValueError("Home Assistant conversation response must be a JSON object")

            return body


def _extract_response_text(response_body: dict[str, Any]) -> str:
    response = response_body.get("response")
    if not isinstance(response, dict):
        raise ValueError("Home Assistant conversation response missing response object")

    speech = response.get("speech")
    if not isinstance(speech, dict):
        raise ValueError("Home Assistant conversation response missing speech object")

    plain = speech.get("plain")
    if isinstance(plain, dict):
        plain_speech = plain.get("speech")
        if isinstance(plain_speech, str) and plain_speech:
            return plain_speech

    ssml = speech.get("ssml")
    if isinstance(ssml, dict):
        ssml_speech = ssml.get("speech")
        if isinstance(ssml_speech, str) and ssml_speech:
            return ssml_speech

    raise ValueError("Home Assistant conversation response missing speech text")
