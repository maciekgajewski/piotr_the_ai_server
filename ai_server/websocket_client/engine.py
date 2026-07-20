from __future__ import annotations

import asyncio
import logging

from ai_server.websocket_messages import AssistantMessageAborted, AssistantMessageCompleted, AssistantMessageStarted
from ai_server.websocket_messages import AssistantTextChunk, ConversationEnded, ConversationReady, ConversationStarted
from ai_server.websocket_messages import FollowUpMessage, FollowUpRequested, FollowUpTimedOut, ProcessingUpdate
from ai_server.websocket_messages import ProtocolRejected, SessionAccepted, SessionStart, StartConversation
from ai_server.websocket_client.aiohttp_transport import ClientProtocolError, TransportDisconnected
from ai_server.websocket_client.clock import SystemClock
from ai_server.websocket_client.interfaces import Clock, Presenter, Transport
from ai_server.websocket_client.messages import ExitKind, ExitResult, InputKind, InputResultKind
from ai_server.websocket_client.state import ClientState, StreamState


class ClientEngine:
    """The one repository-client state machine; presenters never parse wire events."""

    def __init__(self, transport: Transport, presenter: Presenter, *, user: str | None, area: str | None,
                 follow_up_timeout_seconds: float, stop_event: asyncio.Event, clock: Clock | None = None) -> None:
        self._transport = transport
        self._presenter = presenter
        self._user = user
        self._area = area
        self._timeout = follow_up_timeout_seconds
        self._stop_event = stop_event
        self._clock = clock or SystemClock()
        self._state = ClientState.CONNECTING
        self._stream = StreamState.BEFORE_STREAM
        self._stream_id: str | None = None
        self._conversation_id: str | None = None
        self._seen_conversation_ids: set[str] = set()
        self._input_task: asyncio.Task | None = None
        self._presentation_task: asyncio.Task | None = None
        self._presentation_kind: InputKind | None = None
        self._deadline: float | None = None
        self._seen_stream_ids: set[str] = set()
        self._logger = logging.getLogger(f"ai_server.websocket_client.engine.{id(self):x}")

    async def run(self) -> ExitResult:
        try:
            self._logger.info("WebsocketClient[%x] session start", id(self))
            if self._stop_event.is_set():
                return ExitResult(ExitKind.INTERRUPTED)
            send_task = self._commit_send(SessionStart(user=self._user, area=self._area))
            self._transition(ClientState.HANDSHAKING, "session_start_committed")
            await self._join_send(send_task, SessionStart)
            while self._state is not ClientState.CLOSED:
                result = await self._next_result()
                if isinstance(result, ExitResult):
                    return result
        except asyncio.CancelledError:
            return ExitResult(ExitKind.INTERRUPTED)
        except ClientProtocolError as exc:
            self._logger.info("WebsocketClient[%x] protocol error: %s", id(self), exc)
            return ExitResult(ExitKind.PROTOCOL_ERROR, str(exc))
        except TransportDisconnected as exc:
            self._logger.info("WebsocketClient[%x] disconnected: %s", id(self), exc)
            return ExitResult(ExitKind.CONNECTION_LOST, str(exc))
        except KnownUnsentSendFailure as exc:
            return ExitResult(ExitKind.KNOWN_UNSENT, str(exc))
        except DeliveryUncertainSendFailure as exc:
            return ExitResult(ExitKind.DELIVERY_UNCERTAIN, str(exc))
        except Exception as exc:
            return ExitResult(ExitKind.LOCAL_FAILURE, str(exc))
        finally:
            self._transition(ClientState.CLOSING, "terminal_result_selected")
            await self._cancel_input()
            await self._transport.close()
            self._transition(ClientState.CLOSED, "cleanup_completed")
            self._logger.debug("WebsocketClient[%x] cleanup complete", id(self))
        return ExitResult(ExitKind.COMPLETED)

    async def _next_result(self) -> ExitResult | None:
        receive_task = asyncio.create_task(self._transport.receive())
        stop_task = asyncio.create_task(self._stop_event.wait())
        tasks = {receive_task, stop_task}
        deadline_task = None
        if self._deadline is not None:
            deadline_task = asyncio.create_task(self._clock.sleep_until(self._deadline))
            tasks.add(deadline_task)
        if self._input_task is not None:
            tasks.add(self._input_task)
        if self._presentation_task is not None:
            tasks.add(self._presentation_task)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Terminal local interruption wins every uncommitted result.
        if stop_task in done:
            await self._cancel_tasks(pending | {task for task in done if task is not stop_task})
            return ExitResult(ExitKind.INTERRUPTED)
        input_task = self._input_task
        input_result = None
        if input_task is not None and input_task in done:
            try:
                input_result = input_task.result()
            except (asyncio.CancelledError, Exception):
                if receive_task not in done:
                    raise
                self._input_task = None
        if input_result is not None and input_result.kind in (InputResultKind.EXIT, InputResultKind.EOF):
            self._input_task = None
            await self._cancel_tasks(pending | {task for task in done if task is not input_task}, suppress_errors=True)
            return await self._handle_input(input_result)
        if receive_task in done:
            try:
                event = receive_task.result()
            finally:
                await self._cancel_tasks(pending - {self._input_task} if self._input_task else pending)
                if self._presentation_task is not None and self._presentation_task.done():
                    self._presentation_task = None
                    self._presentation_kind = None
            return await self._handle_server(event)
        if self._presentation_task is not None and self._presentation_task in done:
            presented_at = self._presentation_task.result()
            kind = self._presentation_kind
            assert kind is not None
            self._presentation_task = None
            self._presentation_kind = None
            self._transition(ClientState.AWAITING_START_INPUT if kind is InputKind.START else ClientState.AWAITING_FOLLOW_UP_INPUT, "input_presented")
            self._deadline = presented_at + self._timeout if kind is InputKind.FOLLOW_UP else None
            self._input_task = asyncio.create_task(self._presenter.read_input(kind))
            await self._cancel_tasks(pending)
            return None
        if deadline_task is not None and deadline_task in done and self._deadline_wins_over_input(input_result):
            await self._cancel_tasks(pending)
            self._input_task = None
            return await self._send_follow_up_timeout()
        if self._input_task is not None and self._input_task in done:
            assert input_result is not None
            self._input_task = None
            if input_result.kind in (InputResultKind.EMPTY, InputResultKind.HELP, InputResultKind.UNKNOWN_COMMAND):
                return await self._redraw_with_arbitration(input_result, receive_task, stop_task, deadline_task, pending)
            await self._cancel_tasks(pending)
            result = await self._handle_input(input_result)
            if self._state is ClientState.AWAITING_FOLLOW_UP_INPUT and self._deadline is not None and self._clock.monotonic() >= self._deadline:
                return await self._send_follow_up_timeout()
            return result
        assert deadline_task is not None and deadline_task in done
        await self._cancel_tasks(pending)
        return await self._send_follow_up_timeout()

    async def _redraw_with_arbitration(self, result, receive_task, stop_task, deadline_task, pending) -> ExitResult | None:
        if result.kind is InputResultKind.HELP:
            self._presenter.system("Commands:\n  /help  Show this help.\n  /exit  Exit the chat client.")
        elif result.kind is InputResultKind.UNKNOWN_COMMAND:
            self._presenter.system(f"Unknown command: {result.text}. Type /help for available commands.")
        kind = InputKind.START if self._state is ClientState.AWAITING_START_INPUT else InputKind.FOLLOW_UP
        redraw_task = asyncio.create_task(self._presenter.present_input(kind))
        redraw_done, redraw_pending = await asyncio.wait(pending | {redraw_task}, return_when=asyncio.FIRST_COMPLETED)
        if stop_task in redraw_done:
            await self._cancel_tasks(redraw_pending | {task for task in redraw_done if task is not stop_task})
            return ExitResult(ExitKind.INTERRUPTED)
        if receive_task in redraw_done:
            try:
                event = receive_task.result()
            finally:
                await self._cancel_tasks(redraw_pending)
            return await self._handle_server(event)
        if deadline_task is not None and deadline_task in redraw_done:
            await self._cancel_tasks(redraw_pending)
            return await self._send_follow_up_timeout()
        assert redraw_task in redraw_done
        redraw_task.result()
        if self._deadline is not None and self._clock.monotonic() >= self._deadline:
            await self._cancel_tasks(redraw_pending)
            return await self._send_follow_up_timeout()
        await self._cancel_tasks(redraw_pending)
        self._input_task = asyncio.create_task(self._presenter.read_input(kind))
        return None

    async def _handle_server(self, event) -> ExitResult | None:
        self._logger.debug("WebsocketClient[%x] received event=%s state=%s", id(self), type(event).__name__, self._state.value)
        if isinstance(event, ProtocolRejected):
            return ExitResult(ExitKind.REJECTED, f"{event.code.value}{': ' + event.detail if event.detail else ''}")
        if isinstance(event, SessionAccepted):
            self._require(ClientState.HANDSHAKING, event)
            self._transition(ClientState.WAITING_FOR_READY, "session_accepted")
            return None
        if isinstance(event, ConversationReady):
            self._require(ClientState.WAITING_FOR_READY, event)
            await self._offer(InputKind.START)
            return None
        if isinstance(event, ConversationStarted):
            self._require(ClientState.WAITING_FOR_CONVERSATION_START, event)
            if event.conversation_id in self._seen_conversation_ids:
                raise ClientProtocolError("conversation ID was reused")
            self._seen_conversation_ids.add(event.conversation_id)
            self._conversation_id = event.conversation_id
            self._stream, self._stream_id = StreamState.BEFORE_STREAM, None
            self._transition(ClientState.WAITING_FOR_AGENT, "conversation_started")
            return None
        if isinstance(event, ProcessingUpdate):
            self._require_agent_before_stream(event)
            self._presenter.system("processing...")
            return None
        if isinstance(event, AssistantMessageStarted):
            self._require_agent_before_stream(event)
            if event.message_id in self._seen_stream_ids:
                raise ClientProtocolError("assistant stream ID was reused")
            self._seen_stream_ids.add(event.message_id)
            self._stream, self._stream_id = StreamState.OPEN, event.message_id
            self._presenter.assistant_started()
            return None
        if isinstance(event, AssistantTextChunk):
            self._require_open_stream(event.message_id, event)
            self._presenter.assistant_text(event.text)
            return None
        if isinstance(event, AssistantMessageCompleted):
            self._require_open_stream(event.message_id, event)
            self._stream = StreamState.FINISHED
            self._presenter.assistant_finished()
            return None
        if isinstance(event, AssistantMessageAborted):
            self._require_open_stream(event.message_id, event)
            self._stream = StreamState.FINISHED
            self._presenter.assistant_finished(event.reason)
            return None
        if isinstance(event, FollowUpRequested):
            if self._state is not ClientState.WAITING_FOR_AGENT or self._stream not in (StreamState.BEFORE_STREAM, StreamState.FINISHED):
                raise ClientProtocolError(f"{type(event).__name__} is invalid in {self._state.value}")
            await self._offer(InputKind.FOLLOW_UP)
            return None
        if isinstance(event, ConversationEnded):
            if self._state is ClientState.WAITING_FOR_AGENT and self._stream is StreamState.OPEN:
                raise ClientProtocolError("ConversationEnded is invalid while assistant stream is open")
            if self._state not in (
                ClientState.WAITING_FOR_AGENT,
                ClientState.PRESENTING_FOLLOW_UP_INPUT,
                ClientState.AWAITING_FOLLOW_UP_INPUT,
                ClientState.WAITING_FOR_CONVERSATION_END,
            ):
                raise ClientProtocolError(f"ConversationEnded is invalid in {self._state.value}")
            await self._cancel_input()
            self._conversation_id, self._stream_id = None, None
            self._stream = StreamState.BEFORE_STREAM
            if self._presenter.is_batch and not self._presenter.has_pending_messages:
                return ExitResult(ExitKind.COMPLETED)
            if not self._presenter.is_batch:
                self._presenter.system("Conversation ended; waiting for a new conversation.")
            self._transition(ClientState.WAITING_FOR_READY, "conversation_ended")
            return None
        raise ClientProtocolError(f"unsupported server event: {type(event).__name__}")

    async def _offer(self, kind: InputKind) -> None:
        self._transition(ClientState.PRESENTING_START_INPUT if kind is InputKind.START else ClientState.PRESENTING_FOLLOW_UP_INPUT, "input_offer_received")
        self._presentation_kind = kind
        self._presentation_task = asyncio.create_task(self._presenter.present_input(kind))
        self._logger.debug("WebsocketClient[%x] presenting input offer kind=%s", id(self), kind.value)

    async def _handle_input(self, result) -> ExitResult | None:
        if result.kind is InputResultKind.BATCH_COMPLETE:
            return ExitResult(ExitKind.COMPLETED)
        if result.kind in (InputResultKind.EXIT, InputResultKind.EOF):
            return ExitResult(ExitKind.EXITED)
        if result.kind is InputResultKind.EMPTY:
            await self._reopen_input()
            return None
        if result.kind is InputResultKind.HELP:
            self._presenter.system("Commands:\n  /help  Show this help.\n  /exit  Exit the chat client.")
            await self._reopen_input()
            return None
        if result.kind is InputResultKind.UNKNOWN_COMMAND:
            self._presenter.system(f"Unknown command: {result.text}. Type /help for available commands.")
            await self._reopen_input()
            return None
        assert result.kind is InputResultKind.TEXT and result.text is not None
        if self._state is ClientState.AWAITING_FOLLOW_UP_INPUT and (self._deadline is None or result.submitted_at > self._deadline):
            send_task = self._commit_send(FollowUpTimedOut())
            self._deadline = None
            self._transition(ClientState.WAITING_FOR_CONVERSATION_END, "late_follow_up_submission")
            await self._join_send(send_task, FollowUpTimedOut)
            return None
        event = StartConversation(result.text) if self._state is ClientState.AWAITING_START_INPUT else FollowUpMessage(result.text)
        send_task = self._commit_send(event)
        self._deadline = None
        if self._presentation_task is not None:
            await self._cancel_tasks({self._presentation_task})
            self._presentation_task = None
            self._presentation_kind = None
        self._stream, self._stream_id = StreamState.BEFORE_STREAM, None
        self._transition(ClientState.WAITING_FOR_CONVERSATION_START if isinstance(event, StartConversation) else ClientState.WAITING_FOR_AGENT, "user_message_committed")
        await self._join_send(send_task, type(event))
        return None

    async def _reopen_input(self) -> None:
        kind = InputKind.START if self._state is ClientState.AWAITING_START_INPUT else InputKind.FOLLOW_UP
        await self._presenter.present_input(kind)
        self._input_task = asyncio.create_task(self._presenter.read_input(kind))

    def _deadline_wins_over_input(self, input_result) -> bool:
        if input_result is None:
            return True
        if input_result.kind in (InputResultKind.EXIT, InputResultKind.EOF):
            return False
        if input_result.kind is not InputResultKind.TEXT:
            return True
        assert self._deadline is not None
        return input_result.submitted_at > self._deadline

    async def _send_follow_up_timeout(self) -> None:
        self._deadline = None
        await self._cancel_input()
        send_task = self._commit_send(FollowUpTimedOut())
        self._transition(ClientState.WAITING_FOR_CONVERSATION_END, "follow_up_timeout_committed")
        await self._join_send(send_task, FollowUpTimedOut)
        return None

    def _require(self, state: ClientState, event) -> None:
        if self._state is not state:
            raise ClientProtocolError(f"{type(event).__name__} is invalid in {self._state.value}")

    def _require_agent_before_stream(self, event) -> None:
        if self._state is not ClientState.WAITING_FOR_AGENT or self._stream is not StreamState.BEFORE_STREAM:
            raise ClientProtocolError(f"{type(event).__name__} is invalid in {self._state.value}")

    def _require_open_stream(self, message_id: str, event) -> None:
        if self._state is not ClientState.WAITING_FOR_AGENT or self._stream is not StreamState.OPEN or message_id != self._stream_id:
            raise ClientProtocolError(f"{type(event).__name__} does not match the open assistant stream")

    async def _cancel_input(self) -> None:
        self._deadline = None
        if self._presentation_task is not None:
            await self._cancel_tasks({self._presentation_task})
            self._presentation_task = None
            self._presentation_kind = None
        if self._input_task is not None:
            await self._cancel_tasks({self._input_task})
            self._input_task = None
        await self._presenter.cancel_input()

    async def _cancel_tasks(self, tasks, *, suppress_errors: bool = False) -> None:
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        for task in tasks:
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    if not suppress_errors:
                        raise

    def _commit_send(self, event):
        """Synchronously commit one selected semantic event to the transport."""
        try:
            send_task = self._transport.commit_send(event)
            self._logger.debug("WebsocketClient[%x] send committed event=%s state=%s", id(self), type(event).__name__, self._state.value)
        except Exception as exc:
            raise KnownUnsentSendFailure(str(exc)) from exc
        return send_task

    async def _join_send(self, send_task, event_type) -> None:
        """A committed frame is shielded and joined; it is never retried."""
        try:
            await asyncio.shield(send_task)
            self._logger.debug("WebsocketClient[%x] send drained event=%s", id(self), event_type.__name__)
        except Exception as exc:
            raise DeliveryUncertainSendFailure(str(exc)) from exc
        except asyncio.CancelledError:
            await asyncio.shield(send_task)
            raise

    def _transition(self, state: ClientState, cause: str) -> None:
        previous = self._state
        self._state = state
        self._logger.debug("WebsocketClient[%x] state old=%s cause=%s new=%s", id(self), previous.value, cause, state.value)


class KnownUnsentSendFailure(Exception):
    pass


class DeliveryUncertainSendFailure(Exception):
    pass
