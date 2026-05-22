from __future__ import annotations

import logging
from typing import ClassVar

from ai_server.endpoint import CommunicationEndpoint


class EchoAgent:
    _logger: ClassVar[logging.Logger] = logging.getLogger(f"{__name__}.EchoAgent")

    def __init__(self, endpoint: CommunicationEndpoint, log_context: str) -> None:
        self._endpoint = endpoint
        self._log_prefix = f"EchoAgent[{log_context}]"

    async def run(self) -> None:
        while True:
            message = await self._endpoint.receive()
            self._logger.debug("%s Echoing message: %s", self._log_prefix, message.text)
            await self._endpoint.send(message)


async def run_echo_agent(endpoint: CommunicationEndpoint, log_context: str) -> None:
    await EchoAgent(endpoint, log_context).run()
