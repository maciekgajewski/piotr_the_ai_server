from __future__ import annotations

import asyncio
import time

from ai_server.websocket_messages import AssistantMessageAborted, AssistantMessageCompleted, AssistantMessageStarted
from ai_server.websocket_messages import AssistantTextChunk, ConversationEnded, ConversationReady, ConversationStarted
from ai_server.websocket_messages import FollowUpMessage, FollowUpRequested, FollowUpTimedOut, ProcessingUpdate
from ai_server.websocket_messages import ProtocolRejected, SessionAccepted, SessionStart, StartConversation
from ai_server.websocket_client.aiohttp_transport import ClientProtocolError, TransportDisconnected
from ai_server.websocket_client.interfaces import Clock, Presenter, Transport
from ai_server.websocket_client.messages import ExitKind, ExitResult, InputKind, InputResultKind
from ai_server.websocket_client.state import ClientState, StreamState


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


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
        self._input_task: asyncio.Task | None = None
        self._deadline: float | None = None

    async def run(self) -> ExitResult:
        try:
            await self._transport.send(SessionStart(user=self._user, area=self._area))
            self._state = ClientState.HANDSHAKING
            while self._state is not ClientState.CLOSED:
                result = await self._next_result()
                if isinstance(result, ExitResult):
                    return result
        except asyncio.CancelledError:
            return ExitResult(ExitKind.INTERRUPTED)
        except ClientProtocolError as exc:
            return ExitResult(ExitKind.PROTOCOL_ERROR, str(exc))
        except TransportDisconnected as exc:
            return ExitResult(ExitKind.CONNECTION_LOST, str(exc))
        except Exception as exc:
            return ExitResult(ExitKind.LOCAL_FAILURE, str(exc))
        finally:
            await self._cancel_input()
            await self._transport.close()
        return ExitResult(ExitKind.COMPLETED)

    async def _next_result(self) -> ExitResult | None:
        receive_task = asyncio.create_task(self._transport.receive())
        stop_task = asyncio.create_task(self._stop_event.wait())
        tasks = {receive_task, stop_task}
        deadline_task = None
        if self._deadline is not None:
            deadline_task = asyncio.create_task(asyncio.sleep(max(0, self._deadline - self._clock.monotonic())))
            tasks.add(deadline_task)
        if self._input_task is not None:
            tasks.add(self._input_task)
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        # Terminal local interruption wins every uncommitted result.
        if stop_task in done:
            await self._cancel_tasks(pending | {task for task in done if task is not stop_task})
            return ExitResult(ExitKind.INTERRUPTED)
        if receive_task in done:
            try:
                event = receive_task.result()
            finally:
                await self._cancel_tasks(pending - {self._input_task} if self._input_task else pending)
            return await self._handle_server(event)
        if self._input_task is not None and self._input_task in done:
            input_result = self._input_task.result()
            self._input_task = None
            await self._cancel_tasks(pending)
            return await self._handle_input(input_result)
        assert deadline_task is not None and deadline_task in done
        await self._cancel_tasks(pending)
        self._deadline = None
        await self._cancel_input()
        await self._transport.send(FollowUpTimedOut())
        self._state = ClientState.WAITING_FOR_CONVERSATION_END
        return None

    async def _handle_server(self, event) -> ExitResult | None:
        if isinstance(event, ProtocolRejected):
            return ExitResult(ExitKind.REJECTED, f"{event.code.value}{': ' + event.detail if event.detail else ''}")
        if isinstance(event, SessionAccepted):
            self._require(ClientState.HANDSHAKING, event)
            self._state = ClientState.WAITING_FOR_READY
            return None
        if isinstance(event, ConversationReady):
            self._require(ClientState.WAITING_FOR_READY, event)
            await self._offer(InputKind.START)
            return None
        if isinstance(event, ConversationStarted):
            self._require(ClientState.WAITING_FOR_CONVERSATION_START, event)
            self._conversation_id = event.conversation_id
            self._stream, self._stream_id = StreamState.BEFORE_STREAM, None
            self._state = ClientState.WAITING_FOR_AGENT
            return None
        if isinstance(event, ProcessingUpdate):
            self._require_agent_before_stream(event)
            self._presenter.system("processing...")
            return None
        if isinstance(event, AssistantMessageStarted):
            self._require_agent_before_stream(event)
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
            if self._state not in (ClientState.WAITING_FOR_AGENT, ClientState.WAITING_FOR_CONVERSATION_END):
                raise ClientProtocolError(f"ConversationEnded is invalid in {self._state.value}")
            await self._cancel_input()
            self._conversation_id, self._stream_id = None, None
            self._stream = StreamState.BEFORE_STREAM
            if self._presenter.is_batch:
                return ExitResult(ExitKind.COMPLETED)
            self._presenter.system("Conversation ended; waiting for a new conversation.")
            self._state = ClientState.WAITING_FOR_READY
            return None
        raise ClientProtocolError(f"unsupported server event: {type(event).__name__}")

    async def _offer(self, kind: InputKind) -> None:
        presented_at = await self._presenter.present_input(kind)
        self._state = ClientState.AWAITING_START_INPUT if kind is InputKind.START else ClientState.AWAITING_FOLLOW_UP_INPUT
        self._deadline = presented_at + self._timeout if kind is InputKind.FOLLOW_UP else None
        self._input_task = asyncio.create_task(self._presenter.read_input(kind))

    async def _handle_input(self, result) -> ExitResult | None:
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
            await self._transport.send(FollowUpTimedOut())
            self._deadline = None
            self._state = ClientState.WAITING_FOR_CONVERSATION_END
            return None
        event = StartConversation(result.text) if self._state is ClientState.AWAITING_START_INPUT else FollowUpMessage(result.text)
        await self._transport.send(event)
        self._deadline = None
        self._stream, self._stream_id = StreamState.BEFORE_STREAM, None
        self._state = ClientState.WAITING_FOR_CONVERSATION_START if isinstance(event, StartConversation) else ClientState.WAITING_FOR_AGENT
        return None

    async def _reopen_input(self) -> None:
        kind = InputKind.START if self._state is ClientState.AWAITING_START_INPUT else InputKind.FOLLOW_UP
        await self._presenter.present_input(kind)
        self._input_task = asyncio.create_task(self._presenter.read_input(kind))

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
        if self._input_task is not None:
            await self._cancel_tasks({self._input_task})
            self._input_task = None
        await self._presenter.cancel_input()

    async def _cancel_tasks(self, tasks) -> None:
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        for task in tasks:
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
