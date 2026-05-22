from __future__ import annotations

import logging
from typing import ClassVar

from ai_server.endpoint import CommunicationEndpoint


class EchoAgent:
    _logger: ClassVar[logging.Logger] = logging.getLogger(f"{__name__}.EchoAgent")

    async def run(self, endpoint: CommunicationEndpoint, session_id: str) -> None:
        log_prefix = f"EchoAgent[{session_id}]"
        while True:
            message = await endpoint.receive()
            self._logger.debug("%s Echoing message: %s", log_prefix, message.text)
            await endpoint.send(message)

    async def close(self) -> None:
        pass
