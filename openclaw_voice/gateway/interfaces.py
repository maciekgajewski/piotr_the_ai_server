from __future__ import annotations

from typing import Protocol

from openclaw_voice.gateway.messages import GatewayNotification


class NotificationPresenter(Protocol):
    async def present(self, notification: GatewayNotification, *, deadline: float) -> None: ...
