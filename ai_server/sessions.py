from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import ClassVar

from ai_server.agent.echo import run_echo_agent
from ai_server.endpoint import CommunicationEndpoint, EndpointClosed


@dataclass
class Session:
    _logger: ClassVar[logging.Logger] = logging.getLogger(f"{__name__}.Session")

    session_id: str
    endpoint: CommunicationEndpoint

    @property
    def log_context(self) -> str:
        return f"Session[{self.session_id}]"

    async def run(self) -> None:
        try:
            await run_echo_agent(self.endpoint, self.session_id)
        except EndpointClosed:
            self._logger.debug("%s endpoint closed", self.log_context)


class SessionManager:
    _logger: ClassVar[logging.Logger] = logging.getLogger(f"{__name__}.SessionManager")

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def run_session(self, endpoint: CommunicationEndpoint) -> None:
        session = Session(session_id=str(uuid.uuid4()), endpoint=endpoint)
        self._sessions[session.session_id] = session
        self._logger.info("%s New session", session.log_context)

        try:
            await session.run()
        finally:
            self._sessions.pop(session.session_id, None)
            self._logger.info("%s ended", session.log_context)

    @property
    def session_count(self) -> int:
        return len(self._sessions)
