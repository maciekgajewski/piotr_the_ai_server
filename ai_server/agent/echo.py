from __future__ import annotations

import logging

from ai_server.interfaces import CommunicationEndpoint
from ai_server.streaming import forward_one_message


class EchoAgent:
    async def run(self, endpoint: CommunicationEndpoint, session_id: str) -> None:
        logger = logging.getLogger(f"{__name__}.EchoAgent[{session_id}]")
        while True:
            logger.debug("echoing streaming message")
            await forward_one_message(endpoint, endpoint)

    async def close(self) -> None:
        pass
