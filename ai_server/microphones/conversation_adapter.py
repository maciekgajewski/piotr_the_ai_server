from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AbstractAsyncContextManager
from enum import Enum
from typing import TYPE_CHECKING

from ai_server.conversations.contexts import ConversationMedium, InputConversationContext, InputSessionContext
from ai_server.conversations.id_factory import new_id
from ai_server.conversations.interfaces import AssistantOutputSink, InputConversation, InputSession
from ai_server.conversations.messages import AssistantAbortReason, AssistantSinkStarted, AssistantSinkTerminalResult
from ai_server.conversations.messages import AssistantTextAccepted, ConversationEnded, FollowUpRequestCommitted
from ai_server.conversations.messages import FollowUpTimedOut, InputControlEvent, InputConversationFailed
from ai_server.conversations.messages import InputSessionClosed, UserMessage
from ai_server.microphones.interfaces import Microphone
from ai_server.microphones.interfaces import MicrophoneUnavailable
from ai_server.microphones.messages import CueType, ListeningMode, StartListening, VisualState


if TYPE_CHECKING:
    from ai_server.microphones.manager import CapturedUtterance, MicrophoneManager


class VoiceSessionState(Enum):
    IDLE = "idle"
    ACCEPTING = "accepting"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class VoiceInputAdapter:
    def __init__(self, session: "VoiceInputSession") -> None:
        self._session = session

    def open_session(self) -> AbstractAsyncContextManager[InputSession]:
        return _VoiceSessionScope(self._session)


class _VoiceSessionScope(AbstractAsyncContextManager[InputSession]):
    def __init__(self, session: "VoiceInputSession") -> None:
        self._session = session

    async def __aenter__(self) -> InputSession:
        return self._session

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self._session.close()


class VoiceInputSession(InputSession):
    def __init__(
        self,
        *,
        manager: "MicrophoneManager",
        microphone: Microphone,
        assistant_text_buffer_characters: int,
    ) -> None:
        session_id = new_id(f"mic-{microphone.context.name}")
        self._manager = manager
        self._microphone = microphone
        self._context = InputSessionContext(
            input_session_id=session_id,
            medium=ConversationMedium.VOICE,
            area=microphone.context.area,
        )
        self._assistant_text_buffer_characters = assistant_text_buffer_characters
        self._state = VoiceSessionState.IDLE
        self._active: VoiceInputConversation | None = None
        self._accept_task: asyncio.Task[InputConversation] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._active_scope_exited = asyncio.Event()
        self._active_scope_exited.set()
        self._logger = logging.getLogger(
            f"{__name__}.MicrophoneInputSession[{microphone.context.name}:{session_id}]"
        )
        self._unavailable: MicrophoneUnavailable | None = None

    @property
    def context(self) -> InputSessionContext:
        return self._context

    @property
    def closed(self) -> bool:
        return self._state is VoiceSessionState.CLOSED

    @property
    def unavailable(self) -> MicrophoneUnavailable | None:
        return self._unavailable

    def accept_conversation(self) -> AbstractAsyncContextManager[InputConversation]:
        return _VoiceAcceptConversation(self)

    async def close(self) -> None:
        if self._close_task is not None:
            await self._close_task
            return
        if self._state is VoiceSessionState.CLOSED:
            return
        self._transition(VoiceSessionState.CLOSING, "close")
        if self._active is not None:
            self._active.publish_session_closed()
        self._close_task = asyncio.create_task(self._finish_close())
        await self._close_task

    async def _finish_close(self) -> None:
        if self._accept_task is not None and not self._accept_task.done():
            self._accept_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._accept_task
        if self._active is not None:
            await self._active_scope_exited.wait()
        self._transition(VoiceSessionState.CLOSED, "close_complete")

    def mark_unavailable(self, error: MicrophoneUnavailable) -> None:
        if self._unavailable is None:
            self._unavailable = error
        if self._state not in (VoiceSessionState.CLOSING, VoiceSessionState.CLOSED):
            self._transition(VoiceSessionState.CLOSING, "microphone_unavailable")
        if self._active is not None:
            self._active.publish_session_closed(str(error))
        if self._close_task is None and self._state is not VoiceSessionState.CLOSED:
            self._close_task = asyncio.create_task(self._finish_close())

    def _transition(self, state: VoiceSessionState, cause: str) -> None:
        old = self._state
        self._state = state
        self._logger.debug("state transition old=%s cause=%s new=%s", old.value, cause, state.value)


class _VoiceAcceptConversation(AbstractAsyncContextManager[InputConversation]):
    def __init__(self, session: VoiceInputSession) -> None:
        self._session = session

    async def __aenter__(self) -> InputConversation:
        if self._session._state is not VoiceSessionState.IDLE:
            raise AssertionError("voice accept_conversation is legal only in IDLE")
        self._session._transition(VoiceSessionState.ACCEPTING, "accept_conversation")
        self._session._accept_task = asyncio.create_task(self._accept())
        try:
            conversation = await self._session._accept_task
        except asyncio.CancelledError as exc:
            raise InputSessionClosed("voice input session closed during acceptance") from exc
        if self._session._state is not VoiceSessionState.ACCEPTING:
            await conversation.cleanup()
            raise InputSessionClosed("voice input session closed during acceptance")
        self._session._active_scope_exited.clear()
        self._session._active = conversation
        self._session._transition(VoiceSessionState.ACTIVE, "voice_accepted")
        return conversation

    async def _accept(self) -> "VoiceInputConversation":
        manager = self._session._manager
        microphone = self._session._microphone
        logger = self._session._logger
        while True:
            listen = await manager._begin_new_conversation_listening(microphone, logger)
            if listen.mode is ListeningMode.OPEN_MIC:
                captured = await manager._capture_open_mic_utterance(microphone, logger, listen.listen_id)
            else:
                captured = await manager._capture_utterance(
                    microphone=microphone,
                    logger=logger,
                    starts_new_conversation=True,
                    timeout_seconds=None,
                    listen_id=listen.listen_id,
                )
            if not captured.captured:
                logger.debug("new-conversation capture rejected; accepting again")
                continue
            text = "".join(captured.text_fragments)
            if not text.strip():
                continue
            user = None
            if captured.speaker_result is not None:
                user = captured.speaker_result.recognized_user
            context = InputConversationContext(
                conversation_id=new_id(),
                input_session_id=self._session.context.input_session_id,
                medium=ConversationMedium.VOICE,
                user=user,
                area=microphone.context.area,
            )
            logger.info("voice conversation accepted conversation_id=%s", context.conversation_id)
            return VoiceInputConversation(
                session=self._session,
                manager=manager,
                microphone=microphone,
                context=context,
                initial_message=UserMessage(text),
                assistant_text_buffer_characters=self._session._assistant_text_buffer_characters,
            )

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        active = self._session._active
        try:
            if active is not None:
                await active.cleanup()
        finally:
            self._session._active = None
            self._session._accept_task = None
            self._session._active_scope_exited.set()
            if self._session._state is VoiceSessionState.ACTIVE:
                self._session._transition(VoiceSessionState.IDLE, "conversation_scope_exit")


class VoiceInputConversation(InputConversation):
    def __init__(
        self,
        *,
        session: VoiceInputSession,
        manager: "MicrophoneManager",
        microphone: Microphone,
        context: InputConversationContext,
        initial_message: UserMessage,
        assistant_text_buffer_characters: int,
    ) -> None:
        self._session = session
        self._manager = manager
        self._microphone = microphone
        self._context = context
        self._initial_message = initial_message
        self._logger = logging.getLogger(
            f"{__name__}.MicrophoneInputConversation[{context.conversation_id}]"
        )
        self._control: asyncio.Queue[InputControlEvent] = asyncio.Queue(maxsize=1)
        self._sink = VoiceAssistantSink(
            manager=manager,
            microphone=microphone,
            logger=self._logger,
            buffer_characters=assistant_text_buffer_characters,
            unavailable_callback=self._mark_unavailable,
            failure_callback=self._fail_input,
        )
        self._follow_up_token: FollowUpRequestCommitted | None = None
        self._follow_up_acknowledged = asyncio.Event()
        self._follow_up_task: asyncio.Task[None] | None = None
        self._follow_up_listen_id: str | None = None
        self._ended = False
        self._terminal_published = False
        self._terminal_event: ConversationEnded | None = None

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
        return await self._control.get()

    async def processing_update(self) -> None:
        if self._sink.open:
            raise AssertionError("processing update while voice assistant sink is open")
        try:
            await self._manager._speak_processing_update(self._microphone, self._logger)
        except MicrophoneUnavailable as exc:
            self._mark_unavailable(exc)
            raise InputSessionClosed(str(exc)) from exc
        except Exception as exc:
            await self._fail_input(exc)

    async def request_follow_up(self) -> FollowUpRequestCommitted:
        if self._follow_up_task is not None:
            raise AssertionError("duplicate voice follow-up request")
        try:
            await self._manager._set_visual_state(
                self._microphone,
                VisualState.LISTENING,
                "follow_up_requested",
                self._logger,
            )
            await self._manager._play_cue(self._microphone, CueType.FOLLOW_UP_READY, self._logger)
            listen, presented_at = await self._manager._present_follow_up_listening(
                self._microphone,
                self._logger,
            )
        except MicrophoneUnavailable as exc:
            self._mark_unavailable(exc)
            raise InputSessionClosed(str(exc)) from exc
        except Exception as exc:
            await self._fail_input(exc)
        token = FollowUpRequestCommitted(new_id())
        self._follow_up_token = token
        self._follow_up_listen_id = listen.listen_id
        deadline = presented_at + self._manager._follow_up_timeout_for(self._microphone)
        self._follow_up_task = asyncio.create_task(self._collect_follow_up(listen, deadline))
        return token

    def acknowledge_follow_up_ready(self, token: FollowUpRequestCommitted) -> None:
        if token != self._follow_up_token:
            raise AssertionError("voice follow-up token mismatch")
        self._follow_up_acknowledged.set()

    async def _collect_follow_up(self, listen: StartListening, deadline: float) -> None:
        try:
            captured = await self._manager._capture_utterance(
                microphone=self._microphone,
                logger=self._logger,
                starts_new_conversation=False,
                timeout_seconds=self._manager._follow_up_timeout_for(self._microphone),
                listen_id=listen.listen_id,
                speech_start_deadline=deadline,
            )
            if captured.captured:
                outcome: InputControlEvent = UserMessage("".join(captured.text_fragments))
            else:
                await self._manager._play_cue(self._microphone, CueType.FOLLOW_UP_TIMEOUT, self._logger)
                await self._manager._set_visual_state(
                    self._microphone,
                    VisualState.IDLE,
                    "follow_up_timeout",
                    self._logger,
                )
                outcome = FollowUpTimedOut()
            await self._follow_up_acknowledged.wait()
            if self._terminal_published:
                return
            self._control.put_nowait(outcome)
        except asyncio.CancelledError:
            await self._manager._stop_listening_if_active(
                self._microphone,
                listen.listen_id,
                "conversation_scope_exit",
                self._logger,
            )
            raise
        except MicrophoneUnavailable as exc:
            self._logger.warning("voice follow-up lost microphone availability error=%s", exc)
            self._mark_unavailable(exc)
        except Exception as exc:
            self._logger.exception("voice follow-up failed")
            try:
                await self._manager._recover_microphone_boundary(
                    self._microphone,
                    self._logger,
                    exc,
                )
            except MicrophoneUnavailable as unavailable:
                self._mark_unavailable(unavailable)
                return
            self._publish_terminal(InputConversationFailed(str(exc)))

    async def end_conversation(self, event: ConversationEnded) -> None:
        if self._ended:
            raise AssertionError("voice conversation ended twice")
        self._ended = True
        self._terminal_event = event
        self._logger.info(
            "conversation ended reason=%s context_rejection_code=%s detail=%r",
            event.reason.value,
            event.context_rejection_code.value if event.context_rejection_code is not None else None,
            event.detail,
        )

    def publish_session_closed(self, detail: str = "voice input session closed") -> None:
        self._publish_terminal(InputSessionClosed(detail))

    def _mark_unavailable(self, error: MicrophoneUnavailable) -> None:
        self._session.mark_unavailable(error)

    async def _fail_input(self, error: Exception) -> None:
        if isinstance(error, AssertionError):
            raise error
        self._logger.exception("recoverable voice input operation failed", exc_info=error)
        try:
            await self._manager._recover_microphone_boundary(
                self._microphone,
                self._logger,
                error,
            )
        except MicrophoneUnavailable as unavailable:
            self._mark_unavailable(unavailable)
            raise InputSessionClosed(str(unavailable)) from unavailable
        self._publish_terminal(InputConversationFailed(str(error)))
        await asyncio.Event().wait()

    def _publish_terminal(self, event: InputControlEvent) -> None:
        self._terminal_published = True
        existing = None
        while not self._control.empty():
            existing = self._control.get_nowait()
        if isinstance(existing, InputSessionClosed) and not isinstance(event, InputSessionClosed):
            event = existing
        self._control.put_nowait(event)

    async def cleanup(self) -> None:
        if self._follow_up_task is not None and not self._follow_up_task.done():
            self._follow_up_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._follow_up_task
        if self._follow_up_listen_id is not None:
            await self._manager._stop_listening_if_active(
                self._microphone,
                self._follow_up_listen_id,
                "conversation_scope_exit",
                self._logger,
            )
        if self._sink.open:
            await self._sink.abort(AssistantAbortReason.INTERNAL_FAILURE, "conversation scope exited")
        try:
            await self._manager._set_visual_state(
                self._microphone,
                VisualState.IDLE,
                "conversation_scope_exit",
                self._logger,
            )
        except MicrophoneUnavailable as exc:
            self._mark_unavailable(exc)


class VoiceAssistantSink(AssistantOutputSink):
    def __init__(
        self,
        *,
        manager: "MicrophoneManager",
        microphone: Microphone,
        logger: logging.Logger,
        buffer_characters: int,
        unavailable_callback,
        failure_callback,
    ) -> None:
        if buffer_characters <= 0:
            raise ValueError("voice assistant text buffer must be positive")
        self._manager = manager
        self._microphone = microphone
        self._logger = logger
        self._bound = buffer_characters
        self._unavailable_callback = unavailable_callback
        self._failure_callback = failure_callback
        self._buffer = ""
        self._state = "not_started"
        self._render_task: asyncio.Task[None] | None = None
        self._render_committed = False

    @property
    def open(self) -> bool:
        return self._state in ("open", "completing")

    async def start(self) -> AssistantSinkStarted:
        if self._state != "not_started":
            raise AssertionError("voice sink already started")
        try:
            await self._manager._set_visual_state(
                self._microphone,
                VisualState.PROCESSING,
                "assistant_message",
                self._logger,
            )
        except MicrophoneUnavailable as exc:
            self._unavailable_callback(exc)
            raise InputSessionClosed(str(exc)) from exc
        except Exception as exc:
            await self._failure_callback(exc)
            raise AssertionError("voice failure callback returned")
        self._state = "open"
        return AssistantSinkStarted()

    async def send_text(self, chunk: str) -> AssistantTextAccepted:
        if self._state != "open":
            raise AssertionError("voice text outside open sink")
        if not chunk:
            raise ValueError("voice assistant chunk must be non-empty")
        remaining = chunk
        while remaining:
            available = self._bound - len(self._buffer)
            self._buffer += remaining[:available]
            remaining = remaining[available:]
            if len(self._buffer) >= self._bound or self._ends_phrase(self._buffer):
                await self._flush()
        return AssistantTextAccepted()

    async def complete(self) -> AssistantSinkTerminalResult:
        if self._state == "completed":
            return AssistantSinkTerminalResult.COMPLETED
        if self._state == "aborted":
            return AssistantSinkTerminalResult.ABORTED
        if self._state != "open":
            raise AssertionError("voice sink complete outside open state")
        self._state = "completing"
        await self._flush()
        self._state = "completed"
        return AssistantSinkTerminalResult.COMPLETED

    async def abort(self, reason: AssistantAbortReason, detail: str | None = None) -> AssistantSinkTerminalResult:
        del reason, detail
        if self._state == "completed":
            return AssistantSinkTerminalResult.COMPLETED
        if self._render_task is not None and not self._render_task.done():
            if self._render_committed:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._render_task)
            else:
                self._render_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._render_task
        self._render_task = None
        self._buffer = ""
        self._state = "aborted"
        return AssistantSinkTerminalResult.ABORTED

    async def _flush(self) -> None:
        if not self._buffer:
            return
        text = self._buffer
        self._buffer = ""
        self._render_committed = False

        def playback_committed() -> None:
            self._render_committed = True
            self._logger.debug("voice render playback committed chars=%s", len(text))

        operation = asyncio.create_task(
            self._manager._speak_reply_text(
                self._microphone,
                text,
                self._logger,
                on_playback_commit=playback_committed,
            )
        )
        self._render_task = operation
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError:
            if self._render_committed:
                with contextlib.suppress(Exception):
                    await asyncio.shield(operation)
            else:
                operation.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await operation
            raise
        except MicrophoneUnavailable as exc:
            self._unavailable_callback(exc)
            raise InputSessionClosed(str(exc)) from exc
        except Exception as exc:
            await self._failure_callback(exc)
            raise AssertionError("voice failure callback returned")
        finally:
            if operation.done():
                self._render_task = None

    @staticmethod
    def _ends_phrase(text: str) -> bool:
        return text.rstrip().endswith((".", "!", "?", ";", ":", "\n"))
