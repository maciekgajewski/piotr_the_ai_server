from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AbstractAsyncContextManager
from enum import Enum

from aiohttp import ClientConnectionResetError, WSCloseCode, WSMsgType, web

from ai_server.config import Config, WebsocketConfig
from ai_server.conversations.bridge import BridgeSettings, FatalTerminationController, bridge_conversation
from ai_server.conversations.context_provider import ConfigContextProvider, ContextProvider
from ai_server.conversations.contexts import ConversationMedium, InputConversationContext, InputSessionContext
from ai_server.conversations.id_factory import new_id
from ai_server.conversations.interfaces import Agent, AssistantOutputSink, InputConversation, InputSession
from ai_server.conversations.messages import AssistantAbortReason, AssistantSinkStarted, AssistantSinkTerminalResult
from ai_server.conversations.messages import AssistantTextAccepted, ConversationCancelled, ConversationEnded
from ai_server.conversations.messages import FollowUpRequestCommitted, FollowUpTimedOut, InputControlEvent
from ai_server.conversations.messages import InputSessionClosed, UserMessage
from ai_server.websocket_messages import AssistantMessageAborted as WsAssistantMessageAborted
from ai_server.websocket_messages import AssistantMessageCompleted as WsAssistantMessageCompleted
from ai_server.websocket_messages import AssistantMessageStarted as WsAssistantMessageStarted
from ai_server.websocket_messages import AssistantTextChunk as WsAssistantTextChunk
from ai_server.websocket_messages import CancelConversation, ClientEvent, ConversationEnded as WsConversationEnded
from ai_server.websocket_messages import ConversationReady, ConversationStarted, FollowUpMessage
from ai_server.websocket_messages import FollowUpRequested, FollowUpTimedOut as WsFollowUpTimedOut
from ai_server.websocket_messages import InvalidEvent, InvalidJson, ProcessingUpdate as WsProcessingUpdate
from ai_server.websocket_messages import ProtocolRejected, ProtocolRejectionCode, ServerEvent
from ai_server.websocket_messages import SessionAccepted, SessionStart, StartConversation
from ai_server.websocket_messages import client_event_from_json, server_event_to_json


class WebsocketState(Enum):
    HANDSHAKE = "handshake"
    IDLE = "idle"
    ACCEPTING = "accepting"
    ACTIVE = "active"
    FOLLOW_UP_COMMITTING = "follow_up_committing"
    AWAITING_FOLLOW_UP = "awaiting_follow_up"
    CLOSING = "closing"
    CLOSED = "closed"


class _AdmissionController:
    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._active = 0
        self._accepting = True

    def reserve(self) -> bool:
        if not self._accepting or self._active >= self._capacity:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        assert self._active > 0
        self._active -= 1

    def close(self) -> None:
        self._accepting = False

    @property
    def active(self) -> int:
        return self._active


class _InputSessionRegistry:
    def __init__(self) -> None:
        self._sessions: set[WebsocketInputSession] = set()
        self._accepting = True

    def register(self, session: "WebsocketInputSession") -> None:
        if not self._accepting:
            raise InputSessionClosed("server shutting down")
        self._sessions.add(session)

    def deregister(self, session: "WebsocketInputSession") -> None:
        self._sessions.discard(session)

    async def close_all(self) -> None:
        self._accepting = False
        await asyncio.gather(*(session.close("server shutting down") for session in tuple(self._sessions)))

    @property
    def count(self) -> int:
        return len(self._sessions)


class WebsocketInputSession(InputSession):
    def __init__(self, websocket: web.WebSocketResponse, peer: str, config: WebsocketConfig) -> None:
        self._websocket = websocket
        self._config = config
        self._state = WebsocketState.HANDSHAKE
        self._handshake_committed = False
        self._session_context: InputSessionContext | None = None
        self._incoming: asyncio.Queue[SessionStart | _WebsocketInputConversation | InputSessionClosed] = (
            asyncio.Queue(config.ingress_queue_capacity)
        )
        self._control: asyncio.Queue[InputControlEvent] | None = None
        self._writer_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(self._reader_loop(), name=f"websocket-reader-{peer}")
        self._close_task: asyncio.Task[None] | None = None
        self._active_conversation: _WebsocketInputConversation | None = None
        self._active_scope_exited = asyncio.Event()
        self._active_scope_exited.set()
        self._retained_follow_up: UserMessage | FollowUpTimedOut | None = None
        self._follow_up_outcome_committed = False
        self._follow_up_token: FollowUpRequestCommitted | None = None
        self._follow_up_lease: asyncio.Task[None] | None = None
        self._follow_up_deadline: float | None = None
        self._follow_up_arbiter = asyncio.Lock()
        self._retired_tasks: set[asyncio.Task[None]] = set()
        self._logger = logging.getLogger(f"{__name__}.WebsocketInputSession[{peer}:pending]")

    async def start(self) -> None:
        try:
            event = await asyncio.wait_for(
                self._incoming.get(),
                timeout=self._config.handshake_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await self._close_transport(WSCloseCode.POLICY_VIOLATION, "session start timed out")
            raise InputSessionClosed("session start timed out") from exc
        if isinstance(event, InputSessionClosed):
            raise event
        if not isinstance(event, SessionStart):
            raise AssertionError("reader admitted non-handshake event")
        session_id = new_id("ws")
        self._session_context = InputSessionContext(
            input_session_id=session_id,
            medium=ConversationMedium.TEXT,
            user=event.user,
            area=event.area,
        )
        peer = self._logger.name.split("[")[-1].split(":pending]")[0]
        self._logger = logging.getLogger(f"{__name__}.WebsocketInputSession[{peer}:{session_id}]")
        await self._send(SessionAccepted())
        self._transition(WebsocketState.IDLE, "handshake_accepted")
        self._logger.info("session accepted user=%r area=%r", event.user, event.area)

    @property
    def context(self) -> InputSessionContext:
        if self._session_context is None:
            raise RuntimeError("websocket session handshake is incomplete")
        return self._session_context

    @property
    def closed(self) -> bool:
        return self._state is WebsocketState.CLOSED

    @property
    def closing(self) -> bool:
        return self._state in (WebsocketState.CLOSING, WebsocketState.CLOSED)

    def accept_conversation(self) -> AbstractAsyncContextManager[InputConversation]:
        return _WebsocketAcceptConversation(self)

    async def close(self, detail: str = "input session closed") -> None:
        if self._close_task is not None:
            await self._close_task
            return
        if self._state is WebsocketState.CLOSED:
            self._close_task = asyncio.create_task(self._finish_close(detail))
            await self._close_task
            return
        if self._state is not WebsocketState.CLOSING:
            self._transition(WebsocketState.CLOSING, detail)
        self._publish_close(detail)
        self._close_task = asyncio.create_task(self._finish_close(detail))
        await self._close_task

    async def _finish_close(self, detail: str) -> None:
        self._cancel_lease()
        if not self._reader_task.done():
            self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader_task
        await self._join_retired_tasks()
        if self._active_conversation is not None:
            await self._active_scope_exited.wait()
        if self._state is WebsocketState.CLOSED:
            return
        await self._close_transport(WSCloseCode.GOING_AWAY, detail)
        self._transition(WebsocketState.CLOSED, detail)

    async def _reader_loop(self) -> None:
        try:
            while True:
                message = await self._websocket.receive()
                if message.type == WSMsgType.TEXT:
                    payload = message.data
                    if len(payload.encode("utf-8")) > self._config.max_frame_bytes:
                        await self._reject(ProtocolRejectionCode.MESSAGE_TOO_LARGE, "message too large", 1009)
                        return
                    try:
                        event = client_event_from_json(payload)
                    except InvalidJson as exc:
                        await self._reject(ProtocolRejectionCode.INVALID_JSON, str(exc), 1002)
                        return
                    except InvalidEvent as exc:
                        await self._reject(ProtocolRejectionCode.INVALID_EVENT, str(exc), 1002)
                        return
                    self._logger.debug("validated client event type=%s state=%s", type(event).__name__, self._state.value)
                    committed_at = asyncio.get_running_loop().time()
                    if not await self._commit_client_event(event, committed_at):
                        return
                    continue
                if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
                    self._commit_transport_closed("transport closed")
                    return
                if message.type == WSMsgType.ERROR:
                    self._commit_transport_closed("transport error")
                    return
                await self._reject(ProtocolRejectionCode.INVALID_EVENT, "non-text websocket frame", 1002)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.warning("unexpected websocket reader failure", exc_info=True)
            if self._state not in (WebsocketState.CLOSING, WebsocketState.CLOSED):
                self._transition(WebsocketState.CLOSING, "internal failure")
            self._publish_close(str(exc))
            await self._close_transport(1011, "internal failure")

    async def _commit_client_event(self, event: ClientEvent, committed_at: float) -> bool:
        if isinstance(event, SessionStart):
            if self._state is not WebsocketState.HANDSHAKE or self._handshake_committed:
                return await self._invalid_state("duplicate session_start")
            if not await self._enqueue_ingress(event):
                return False
            self._handshake_committed = True
            return True
        if isinstance(event, StartConversation):
            if self._state is not WebsocketState.ACCEPTING:
                return await self._invalid_state("start_conversation outside accepting")
            return await self._commit_start_conversation(event)
        if isinstance(event, CancelConversation):
            if self._state not in (
                WebsocketState.ACTIVE,
                WebsocketState.FOLLOW_UP_COMMITTING,
                WebsocketState.AWAITING_FOLLOW_UP,
            ):
                return await self._invalid_state("cancel_conversation without active conversation")
            self._retained_follow_up = None
            self._follow_up_outcome_committed = False
            self._cancel_lease()
            return await self._enqueue_control(ConversationCancelled())
        if isinstance(event, (FollowUpMessage, WsFollowUpTimedOut)):
            return await self._commit_follow_up_outcome(event, committed_at)
        return await self._invalid_state("unsupported client event")

    async def _commit_follow_up_outcome(
        self,
        event: FollowUpMessage | WsFollowUpTimedOut,
        committed_at: float,
    ) -> bool:
        async with self._follow_up_arbiter:
            if self._follow_up_outcome_committed:
                await self._reject(
                    ProtocolRejectionCode.DUPLICATE_FOLLOW_UP_OUTCOME,
                    "duplicate follow-up outcome",
                    1002,
                )
                return False
            if self._state not in (WebsocketState.FOLLOW_UP_COMMITTING, WebsocketState.AWAITING_FOLLOW_UP):
                return await self._invalid_state("follow-up outcome outside committed interval")
            if self._follow_up_deadline is None:
                raise AssertionError("follow-up state has no resource-lease deadline")
            if committed_at > self._follow_up_deadline:
                await self._expire_follow_up_lease()
                return False
            outcome: UserMessage | FollowUpTimedOut
            outcome = UserMessage(event.message) if isinstance(event, FollowUpMessage) else FollowUpTimedOut()
            self._follow_up_outcome_committed = True
            self._cancel_lease()
            if self._state is WebsocketState.FOLLOW_UP_COMMITTING:
                self._retained_follow_up = outcome
                return True
            self._transition(WebsocketState.ACTIVE, "follow_up_outcome")
            return await self._enqueue_control(outcome)

    async def _commit_start_conversation(self, event: StartConversation) -> bool:
        context = InputConversationContext(
            conversation_id=new_id(),
            input_session_id=self.context.input_session_id,
            medium=ConversationMedium.TEXT,
            user=self.context.user,
            area=self.context.area,
        )
        self._control = asyncio.Queue(maxsize=1)
        conversation = _WebsocketInputConversation(self, context, UserMessage(event.message))
        self._active_conversation = conversation
        self._active_scope_exited.clear()
        if not await self._enqueue_ingress(conversation):
            self._active_conversation = None
            self._active_scope_exited.set()
            self._control = None
            return False
        self._transition(WebsocketState.ACTIVE, "start_conversation_committed")
        return True

    async def _enqueue_ingress(
        self,
        event: SessionStart | _WebsocketInputConversation,
    ) -> bool:
        try:
            self._incoming.put_nowait(event)
            return True
        except asyncio.QueueFull:
            await self._reject(ProtocolRejectionCode.INGRESS_OVERFLOW, "ingress overflow", 1008)
            return False

    async def _enqueue_control(self, event: InputControlEvent) -> bool:
        if self._control is None:
            raise AssertionError("active websocket conversation has no control queue")
        try:
            self._control.put_nowait(event)
            return True
        except asyncio.QueueFull:
            await self._reject(ProtocolRejectionCode.INGRESS_OVERFLOW, "ingress overflow", 1008)
            return False

    async def _invalid_state(self, detail: str) -> bool:
        await self._reject(ProtocolRejectionCode.INVALID_STATE, detail, 1002)
        return False

    async def _reject(self, code: ProtocolRejectionCode, detail: str, close_code: int) -> None:
        if self._state in (WebsocketState.CLOSING, WebsocketState.CLOSED):
            return
        self._transition(WebsocketState.CLOSING, code.value)
        self._cancel_lease()
        self._publish_close(detail)
        self._logger.warning("protocol rejected code=%s detail=%s", code.value, detail)
        with contextlib.suppress(Exception):
            await self._send(ProtocolRejected(code, detail))
        await self._close_transport(close_code, code.value if close_code == 1002 else detail)

    def _commit_transport_closed(self, detail: str) -> None:
        if self._state not in (WebsocketState.CLOSING, WebsocketState.CLOSED):
            self._transition(WebsocketState.CLOSING, detail)
        self._cancel_lease()
        self._publish_close(detail)

    def _publish_close(self, detail: str) -> None:
        closed = InputSessionClosed(detail)
        if self._control is not None:
            self._discard_queued(self._control)
            self._control.put_nowait(closed)
        if self._active_conversation is None:
            self._discard_queued(self._incoming)
            self._incoming.put_nowait(closed)

    @staticmethod
    def _discard_queued(queue: asyncio.Queue[object]) -> None:
        while not queue.empty():
            queue.get_nowait()

    async def _send(self, event: ServerEvent, on_handoff=None) -> None:
        payload = server_event_to_json(event)
        async with self._writer_lock:
            self._logger.debug("write handoff event=%s", type(event).__name__)

            async def write() -> None:
                if on_handoff is not None:
                    on_handoff()
                await self._websocket.send_str(payload)

            task = asyncio.create_task(write())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    await task
                except ClientConnectionResetError as exc:
                    self._commit_transport_closed("transport reset")
                    raise InputSessionClosed("transport reset") from exc
                # Once handed to the transport writer, cancellation cannot
                # roll back the externally visible operation.
            except ClientConnectionResetError as exc:
                self._commit_transport_closed("transport reset")
                raise InputSessionClosed("transport reset") from exc
            self._logger.debug("write drain completed event=%s", type(event).__name__)

    async def _close_transport(self, code: int, reason: str) -> None:
        if not self._websocket.closed:
            await self._websocket.close(code=code, message=reason.encode("utf-8")[:123])

    def _start_lease(self) -> None:
        self._cancel_lease()
        deadline = asyncio.get_running_loop().time() + self._config.follow_up_idle_lease_seconds
        self._follow_up_deadline = deadline
        self._follow_up_lease = asyncio.create_task(self._lease_timer(deadline))

    async def _lease_timer(self, deadline: float) -> None:
        try:
            await asyncio.sleep(max(0.0, deadline - asyncio.get_running_loop().time()))
        except asyncio.CancelledError:
            raise
        async with self._follow_up_arbiter:
            if self._state not in (WebsocketState.FOLLOW_UP_COMMITTING, WebsocketState.AWAITING_FOLLOW_UP):
                return
            if self._follow_up_deadline != deadline or self._follow_up_outcome_committed:
                return
            await self._expire_follow_up_lease()

    async def _expire_follow_up_lease(self) -> None:
        if self._state not in (WebsocketState.FOLLOW_UP_COMMITTING, WebsocketState.AWAITING_FOLLOW_UP):
            return
        self._logger.warning("follow-up resource lease expired")
        self._follow_up_deadline = None
        self._transition(WebsocketState.CLOSING, "follow-up resource lease expired")
        self._publish_close("follow-up resource lease expired")
        await self._close_transport(1013, "follow-up resource lease expired")

    def _cancel_lease(self) -> None:
        if self._follow_up_lease is not None and not self._follow_up_lease.done():
            self._follow_up_lease.cancel()
            self._retired_tasks.add(self._follow_up_lease)
        self._follow_up_lease = None
        self._follow_up_deadline = None

    async def _join_retired_tasks(self) -> None:
        tasks = tuple(self._retired_tasks)
        self._retired_tasks.clear()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _transition(self, state: WebsocketState, cause: str) -> None:
        old = self._state
        self._state = state
        self._logger.debug("state transition old=%s cause=%s new=%s", old.value, cause, state.value)


class _WebsocketAcceptConversation(AbstractAsyncContextManager[InputConversation]):
    def __init__(self, session: WebsocketInputSession) -> None:
        self._session = session
        self._conversation: _WebsocketInputConversation | None = None

    async def __aenter__(self) -> InputConversation:
        if self._session._state is not WebsocketState.IDLE:
            raise AssertionError("accept_conversation is legal only in IDLE")
        self._session._transition(WebsocketState.ACCEPTING, "accept_conversation")
        await self._session._send(ConversationReady())
        event = await self._session._incoming.get()
        if isinstance(event, InputSessionClosed):
            raise event
        if not isinstance(event, _WebsocketInputConversation):
            raise AssertionError("reader admitted invalid acceptance event")
        self._conversation = event
        try:
            await self._session._send(ConversationStarted(event.context.conversation_id))
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        self._session._logger.info(
            "conversation started conversation_id=%s",
            event.context.conversation_id,
        )
        return self._conversation

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        try:
            if self._conversation is not None:
                await self._conversation.cleanup()
            self._session._retained_follow_up = None
            self._session._follow_up_outcome_committed = False
            self._session._follow_up_token = None
            self._session._cancel_lease()
            await self._session._join_retired_tasks()
        finally:
            self._session._active_conversation = None
            self._session._control = None
            self._session._active_scope_exited.set()
            if self._session._state not in (WebsocketState.CLOSING, WebsocketState.CLOSED):
                self._session._transition(WebsocketState.IDLE, "conversation_scope_exit")


class _WebsocketInputConversation(InputConversation):
    def __init__(self, session: WebsocketInputSession, context: InputConversationContext, initial_message: UserMessage) -> None:
        self._session = session
        self._context = context
        self._initial_message = initial_message
        self._sink = _WebsocketAssistantSink(session)
        self._ended = False

    @property
    def context(self) -> InputConversationContext:
        return self._context

    @property
    def initial_message(self) -> UserMessage:
        return self._initial_message

    @property
    def assistant_output(self) -> AssistantOutputSink:
        return self._sink

    async def receive_control(self) -> InputControlEvent:
        if self._session._control is None:
            raise InputSessionClosed("conversation control closed")
        return await self._session._control.get()

    async def processing_update(self) -> None:
        await self._session._send(WsProcessingUpdate())

    async def request_follow_up(self) -> FollowUpRequestCommitted:
        if self._session._state is not WebsocketState.ACTIVE:
            raise AssertionError("follow-up request outside active state")
        token = FollowUpRequestCommitted(new_id())

        def commit() -> None:
            self._session._follow_up_token = token
            self._session._retained_follow_up = None
            self._session._follow_up_outcome_committed = False
            self._session._transition(WebsocketState.FOLLOW_UP_COMMITTING, "follow_up_handoff")
            self._session._start_lease()

        await self._session._send(FollowUpRequested(), on_handoff=commit)
        return token

    def acknowledge_follow_up_ready(self, token: FollowUpRequestCommitted) -> None:
        if self._session._state is not WebsocketState.FOLLOW_UP_COMMITTING:
            raise AssertionError("follow-up acknowledgement outside committing state")
        if token != self._session._follow_up_token:
            raise AssertionError("follow-up token mismatch")
        self._session._transition(WebsocketState.AWAITING_FOLLOW_UP, "follow_up_acknowledged")
        retained = self._session._retained_follow_up
        if retained is not None:
            self._session._retained_follow_up = None
            self._session._transition(WebsocketState.ACTIVE, "retained_follow_up_released")
            assert self._session._control is not None
            self._session._control.put_nowait(retained)

    async def end_conversation(self, event: ConversationEnded) -> None:
        if self._ended:
            raise AssertionError("conversation ended twice")
        self._ended = True
        self._session._cancel_lease()
        await self._session._send(
            WsConversationEnded(
                reason=event.reason.value,
                context_rejection_code=(
                    event.context_rejection_code.value if event.context_rejection_code is not None else None
                ),
                detail=event.detail,
            )
        )
        self._session._logger.info(
            "conversation ended conversation_id=%s reason=%s",
            self._context.conversation_id,
            event.reason.value,
        )

    async def cleanup(self) -> None:
        if self._sink.open:
            await self._sink.abort(AssistantAbortReason.INTERNAL_FAILURE, "conversation scope exited with open sink")


class _WebsocketAssistantSink(AssistantOutputSink):
    def __init__(self, session: WebsocketInputSession) -> None:
        self._session = session
        self._state = "not_started"
        self._message_id: str | None = None

    @property
    def open(self) -> bool:
        return self._state == "open"

    async def start(self) -> AssistantSinkStarted:
        if self._state != "not_started":
            raise AssertionError("assistant sink start in invalid state")
        self._message_id = new_id()
        await self._session._send(WsAssistantMessageStarted(self._message_id))
        self._state = "open"
        return AssistantSinkStarted()

    async def send_text(self, chunk: str) -> AssistantTextAccepted:
        if self._state != "open" or self._message_id is None:
            raise AssertionError("assistant text outside open sink")
        if not chunk:
            raise ValueError("assistant text chunk must be non-empty")
        await self._session._send(WsAssistantTextChunk(self._message_id, chunk))
        return AssistantTextAccepted()

    async def complete(self) -> AssistantSinkTerminalResult:
        if self._state == "completed":
            return AssistantSinkTerminalResult.COMPLETED
        if self._state == "aborted":
            return AssistantSinkTerminalResult.ABORTED
        if self._state != "open" or self._message_id is None:
            raise AssertionError("assistant completion outside open sink")
        await self._session._send(WsAssistantMessageCompleted(self._message_id))
        self._state = "completed"
        return AssistantSinkTerminalResult.COMPLETED

    async def abort(self, reason: AssistantAbortReason, detail: str | None = None) -> AssistantSinkTerminalResult:
        if self._state == "completed":
            return AssistantSinkTerminalResult.COMPLETED
        if self._state == "aborted":
            return AssistantSinkTerminalResult.ABORTED
        if self._state == "not_started":
            self._state = "aborted"
            return AssistantSinkTerminalResult.ABORTED
        assert self._message_id is not None
        await self._session._send(WsAssistantMessageAborted(self._message_id, reason.value, detail))
        self._state = "aborted"
        return AssistantSinkTerminalResult.ABORTED


def create_app(
    config: Config,
    agent: Agent,
    context_provider: ContextProvider | None = None,
    fatal_termination: FatalTerminationController | None = None,
) -> web.Application:
    provider = context_provider or ConfigContextProvider(config.users)
    bridge_settings = BridgeSettings(
        agent_cancellation_deadline_seconds=config.conversation.agent_cancellation_deadline_seconds,
        fatal_notification_seconds=config.conversation.fatal_notification_seconds,
    )
    admission = _AdmissionController(config.websocket.max_connections)
    registry = _InputSessionRegistry()
    admission_logger = logging.getLogger(f"{__name__}.WebsocketAdmission")
    app = web.Application()

    async def websocket_handler(request: web.Request) -> web.StreamResponse:
        peer = _format_peer(request)
        if not admission.reserve():
            admission_logger.info(
                "connection rejected peer=%s active=%s capacity=%s",
                peer,
                admission.active,
                config.websocket.max_connections,
            )
            return web.Response(
                status=503,
                headers={"Retry-After": str(config.websocket.capacity_retry_after_seconds)},
                text="websocket capacity exhausted",
            )
        admission_logger.debug(
            "slot reserved peer=%s active=%s capacity=%s",
            peer,
            admission.active,
            config.websocket.max_connections,
        )
        websocket: web.WebSocketResponse | None = None
        input_session: WebsocketInputSession | None = None
        try:
            websocket = web.WebSocketResponse(heartbeat=config.websocket.heartbeat_seconds)
            await websocket.prepare(request)
            input_session = WebsocketInputSession(websocket, peer, config.websocket)
            registry.register(input_session)
            await input_session.start()
            async with _OpenedWebsocketSession(input_session) as session:
                while not session.closing:
                    try:
                        async with session.accept_conversation() as conversation:
                            await bridge_conversation(
                                input_conversation=conversation,
                                agent=agent,
                                context_provider=provider,
                                settings=bridge_settings,
                                fatal_termination=fatal_termination,
                            )
                    except InputSessionClosed:
                        break
            return websocket
        finally:
            if input_session is not None:
                await input_session.close()
                registry.deregister(input_session)
            admission.release()
            admission_logger.debug(
                "slot released peer=%s active=%s capacity=%s",
                peer,
                admission.active,
                config.websocket.max_connections,
            )

    async def shutdown(_app: web.Application) -> None:
        admission.close()
        await registry.close_all()

    async def status_handler(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "websocket": {
                    "host": config.websocket.host,
                    "port": config.websocket.port,
                    "path": config.websocket.path,
                    "active_connections": admission.active,
                    "active_sessions": registry.count,
                },
            }
        )

    app.router.add_get(config.websocket.path, websocket_handler)
    app.router.add_get("/api/status", status_handler)
    app.on_shutdown.append(shutdown)
    return app


class _OpenedWebsocketSession(AbstractAsyncContextManager[InputSession]):
    def __init__(self, session: WebsocketInputSession) -> None:
        self._session = session

    async def __aenter__(self) -> InputSession:
        return self._session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self._session.close()


def _format_peer(request: web.Request) -> str:
    peername = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peername, tuple) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"
    return request.remote or "unknown"
