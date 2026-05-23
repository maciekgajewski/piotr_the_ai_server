from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from ai_server.agent import Agent
from ai_server.interfaces import CommunicationEndpoint, EndpointClosed


@dataclass
class Session:
    session_id: str
    endpoint: CommunicationEndpoint

    def __post_init__(self) -> None:
        self._logger = logging.getLogger(f"{__name__}.Session[{self.session_id}]")

    async def run(self, agent: Agent) -> None:
        try:
            await agent.run(self.endpoint, self.session_id)
        except EndpointClosed:
            self._logger.debug("endpoint closed")


class SessionManager:
    def __init__(self, agent: Agent) -> None:
        self._logger = logging.getLogger(f"{__name__}.SessionManager")
        self._agent = agent
        self._sessions: dict[str, Session] = {}

    async def run_session(self, endpoint: CommunicationEndpoint) -> None:
        session = Session(session_id=str(uuid.uuid4()), endpoint=endpoint)
        self._sessions[session.session_id] = session
        session._logger.info("new session")

        try:
            await session.run(self._agent)
        finally:
            self._sessions.pop(session.session_id, None)
            session._logger.info("ended")

    @property
    def session_count(self) -> int:
        return len(self._sessions)
