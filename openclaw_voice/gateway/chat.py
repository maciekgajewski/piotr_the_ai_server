from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from openclaw_voice.gateway.client import GatewayClient
from openclaw_voice.gateway.messages import GatewayCapacityExceeded, GatewayChatBusy, GatewayChatFailed
from openclaw_voice.gateway.messages import GatewayChatReply, GatewayChatUncertain, GatewayDisconnected
from openclaw_voice.gateway.messages import GatewayError, GatewayEvent, GatewayProtocolError, GatewayRejected
from openclaw_voice.gateway.settings import GatewayChatSettings


@dataclass
class _PendingTurn:
    session_key: str
    run_id: str
    outcome: asyncio.Queue[GatewayEvent | GatewayError] = field(default_factory=lambda: asyncio.Queue(maxsize=1))


class GatewayChat:
    """One operator connection's correlated, bounded spoken chat turns.

    The caller owns connection supervision and persists quarantined room routes
    across connection generations. A lost run is never submitted again.
    """

    def __init__(
        self,
        gateway: GatewayClient,
        settings: GatewayChatSettings,
        *,
        quarantined_sessions: frozenset[str] = frozenset(),
    ) -> None:
        self._gateway = gateway
        self._settings = settings
        self._pending: dict[str, _PendingTurn] = {}
        self._active_sessions: set[str] = set()
        self._quarantined_sessions = set(quarantined_sessions)
        self._turn_tasks: set[asyncio.Task[Any]] = set()
        self._reader: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._logger = logging.getLogger(f"{__name__}.GatewayChat")

    @property
    def quarantined_sessions(self) -> frozenset[str]:
        return frozenset(self._quarantined_sessions)

    def start(self) -> None:
        if self._reader is not None or self._closed:
            raise AssertionError("GatewayChat can be started only once")
        if not self._gateway.connected or not self._gateway.observes_event("chat"):
            raise GatewayProtocolError("connected operator must observe chat events")
        if self._gateway.hello.role != "operator" or "chat" not in self._gateway.hello.events:
            raise GatewayProtocolError("Gateway chat requires an operator with chat events")
        for method in ("chat.send", "chat.abort"):
            if method not in self._gateway.hello.methods:
                raise GatewayProtocolError(f"operator Gateway did not advertise {method}")
        self._reader = asyncio.create_task(self._read_events(), name="openclaw-chat-events")

    async def exchange(self, session_key: str, message: str) -> GatewayChatReply:
        if self._reader is None or self._closed or self._reader.done():
            raise GatewayDisconnected("Gateway chat event reader is unavailable")
        if not isinstance(session_key, str) or not session_key or len(session_key) > 512:
            raise ValueError("chat session key must be non-empty and at most 512 characters")
        if not isinstance(message, str) or not message.strip() or len(message) > self._settings.max_message_characters:
            raise ValueError("chat message must be non-empty and within its configured bound")
        if session_key in self._quarantined_sessions or session_key in self._active_sessions:
            raise GatewayChatBusy("chat room is active or has unresolved remote work")
        if len(self._pending) + len(self._quarantined_sessions) >= self._settings.max_active_turns:
            raise GatewayCapacityExceeded("chat turn capacity reached")

        turn_task = asyncio.current_task()
        assert turn_task is not None
        run_id = uuid.uuid4().hex
        pending = _PendingTurn(session_key, run_id)
        self._pending[run_id] = pending
        self._active_sessions.add(session_key)
        self._turn_tasks.add(turn_task)
        uncertain = False
        started = time.monotonic()
        self._logger.info("chat request submitted session=%s run=%s chars=%s", session_key, run_id, len(message))
        try:
            async with asyncio.timeout(self._settings.turn_timeout_seconds):
                response = await self._gateway.request("chat.send", {
                    "sessionKey": session_key,
                    "message": message,
                    "idempotencyKey": run_id,
                    "deliver": False,
                })
                if not isinstance(response, dict) or response.get("runId") != run_id or response.get("status") != "started":
                    raise GatewayProtocolError("chat.send returned an unexpected run acknowledgement")
                if self._closed:
                    raise GatewayChatUncertain("chat client closed before reply authority was granted")
                event = await pending.outcome.get()
                if isinstance(event, GatewayError):
                    raise event
                reply = self._terminal_reply(event, pending)
                if self._closed:
                    raise GatewayChatUncertain("chat client closed before reply completion")
                self._logger.info(
                    "chat reply completed session=%s run=%s chars=%s duration_seconds=%.3f",
                    session_key, run_id, len(reply.text), time.monotonic() - started,
                )
                return reply
        except asyncio.CancelledError:
            uncertain = True
            await self._abort_best_effort(pending)
            raise
        except GatewayRejected:
            self._logger.info("chat request rejected session=%s run=%s", session_key, run_id)
            raise
        except GatewayChatFailed:
            self._logger.info("chat run failed session=%s run=%s", session_key, run_id)
            raise
        except GatewayCapacityExceeded:
            self._logger.debug("chat request rejected locally before dispatch session=%s run=%s", session_key, run_id)
            raise
        except (TimeoutError, GatewayError) as exc:
            uncertain = True
            await self._abort_best_effort(pending)
            raise GatewayChatUncertain("chat run did not settle; room route is quarantined") from exc
        finally:
            self._pending.pop(run_id, None)
            self._active_sessions.remove(session_key)
            self._turn_tasks.discard(turn_task)
            if uncertain:
                self._quarantined_sessions.add(session_key)
                self._logger.warning("chat room quarantined session=%s run=%s", session_key, run_id)

    async def close(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._finish_close(), name="openclaw-chat-close")
        await asyncio.shield(self._close_task)

    async def _finish_close(self) -> None:
        self._release_pending(GatewayChatUncertain("chat client closed before remote settlement"), replace=True)
        active = tuple(self._turn_tasks)
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass

    async def _read_events(self) -> None:
        try:
            while True:
                event = await self._gateway.receive_event()
                if event.name != "chat" or not isinstance(event.payload, dict):
                    continue
                run_id = event.payload.get("runId")
                pending = self._pending.get(run_id) if isinstance(run_id, str) else None
                if pending is None or not pending.outcome.empty():
                    continue
                if event.payload.get("sessionKey") != pending.session_key:
                    pending.outcome.put_nowait(GatewayProtocolError("chat event session key mismatched its run"))
                    continue
                state = event.payload.get("state")
                if not isinstance(state, str) or state not in {"status", "delta", "final", "error", "aborted"}:
                    pending.outcome.put_nowait(GatewayProtocolError("chat event has an invalid state"))
                elif state in {"final", "error", "aborted"}:
                    pending.outcome.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except GatewayError as exc:
            self._logger.warning("chat event reader failed type=%s", type(exc).__name__)
            self._release_pending(GatewayChatUncertain("chat event stream disconnected before remote settlement"))

    def _release_pending(self, error: GatewayError, *, replace: bool = False) -> None:
        for pending in self._pending.values():
            if replace and not pending.outcome.empty():
                pending.outcome.get_nowait()
            if pending.outcome.empty():
                pending.outcome.put_nowait(error)

    def _terminal_reply(self, event: GatewayEvent, pending: _PendingTurn) -> GatewayChatReply:
        payload = event.payload
        assert isinstance(payload, dict)
        state = payload["state"]
        if state != "final":
            raise GatewayChatFailed(f"chat run ended with {state}")
        if payload.get("yielded") is True:
            raise GatewayChatFailed("chat run yielded without a direct spoken reply")
        message = payload.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise GatewayChatFailed("chat run completed without an assistant message")
        content = message.get("content")
        if not isinstance(content, list) or not content:
            raise GatewayChatFailed("chat run completed without speakable text")
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise GatewayChatFailed("chat reply includes unsupported content")
            chunks.append(block["text"])
        text = "\n".join(chunks).strip()
        if not text or len(text) > self._settings.max_reply_characters:
            raise GatewayChatFailed("chat reply has no text or exceeds the configured bound")
        return GatewayChatReply(pending.run_id, pending.session_key, text)

    async def _abort_best_effort(self, pending: _PendingTurn) -> None:
        try:
            async with asyncio.timeout(self._settings.abort_timeout_seconds):
                await self._gateway.request("chat.abort", {
                    "sessionKey": pending.session_key,
                    "runId": pending.run_id,
                })
        except (GatewayError, TimeoutError, asyncio.CancelledError):
            self._logger.debug("chat abort did not prove remote settlement run=%s", pending.run_id)
