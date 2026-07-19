import pytest

from ai_server.conversations.contexts import ConversationMedium, InputConversationContext, InputSessionContext
from ai_server.conversations.id_factory import new_id
from ai_server.conversations.messages import AgentCancellationAcknowledged, AgentCancellationReason
from ai_server.conversations.messages import AssistantAbortReason, ContextRejectionCode, ConversationEnded
from ai_server.conversations.messages import ConversationEndReason, TurnDisposition, TurnDispositionKind, UserMessage
from ai_server.websocket_messages import AssistantMessageAborted, AssistantMessageCompleted, AssistantMessageStarted
from ai_server.websocket_messages import AssistantTextChunk, CancelConversation, ConversationEnded as WsConversationEnded
from ai_server.websocket_messages import ConversationReady, ConversationStarted, FollowUpMessage, FollowUpRequested
from ai_server.websocket_messages import FollowUpTimedOut, InvalidEvent, InvalidJson, ProcessingUpdate
from ai_server.websocket_messages import ProtocolRejected, ProtocolRejectionCode, SessionAccepted, SessionStart
from ai_server.websocket_messages import StartConversation, client_event_from_json, client_event_to_json
from ai_server.websocket_messages import server_event_from_json, server_event_to_json


def test_conversation_terminal_code_invariant() -> None:
    assert ConversationEnded(
        ConversationEndReason.CONTEXT_REJECTED,
        ContextRejectionCode.UNKNOWN_USER,
    ).context_rejection_code is ContextRejectionCode.UNKNOWN_USER
    with pytest.raises(ValueError, match="required exactly"):
        ConversationEnded(ConversationEndReason.CONTEXT_REJECTED)
    with pytest.raises(ValueError, match="required exactly"):
        ConversationEnded(ConversationEndReason.COMPLETED, ContextRejectionCode.UNKNOWN_USER)


def test_user_message_rejects_empty_text() -> None:
    with pytest.raises(ValueError, match="non-whitespace"):
        UserMessage("  ")


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: TurnDisposition("end_conversation"),
        lambda: AgentCancellationAcknowledged("input_cancelled"),
        lambda: ConversationEnded("completed"),
        lambda: ConversationEnded(ConversationEndReason.CONTEXT_REJECTED, "unknown_user"),
        lambda: InputSessionContext("session-1", "text"),
        lambda: InputConversationContext("conversation-1", "session-1", "text"),
    ],
)
def test_closed_internal_enums_are_structurally_validated(constructor) -> None:
    with pytest.raises(ValueError):
        constructor()


def test_valid_closed_internal_enums_remain_constructible() -> None:
    assert TurnDisposition(TurnDispositionKind.END_CONVERSATION).kind is TurnDispositionKind.END_CONVERSATION
    assert AgentCancellationAcknowledged(
        AgentCancellationReason.INPUT_CANCELLED
    ).reason is AgentCancellationReason.INPUT_CANCELLED
    assert InputSessionContext("session-1", ConversationMedium.TEXT).medium is ConversationMedium.TEXT


def test_client_json_round_trip_omits_optional_none() -> None:
    payload = client_event_to_json(SessionStart())
    assert payload == '{"type":"session_start"}'
    assert client_event_from_json(payload) == SessionStart()
    assert client_event_from_json('{"type":"start_conversation","message":"hello"}') == StartConversation("hello")


@pytest.mark.parametrize(
    "event",
    [
        SessionStart(),
        SessionStart(user="Maciek", area="office"),
        StartConversation("hello"),
        FollowUpMessage("more"),
        FollowUpTimedOut(),
        CancelConversation(),
    ],
)
def test_every_client_event_has_a_strict_round_trip(event) -> None:
    assert client_event_from_json(client_event_to_json(event)) == event


@pytest.mark.parametrize(
    "event",
    [
        SessionAccepted(),
        ConversationReady(),
        ConversationStarted("conversation-1"),
        ProcessingUpdate(),
        AssistantMessageStarted("message-1"),
        AssistantTextChunk("message-1", "hello"),
        AssistantMessageCompleted("message-1"),
        AssistantMessageAborted("message-1", AssistantAbortReason.AGENT_FAILED.value),
        AssistantMessageAborted(
            "message-1",
            AssistantAbortReason.INPUT_FAILED.value,
            "diagnostic",
        ),
        FollowUpRequested(),
        WsConversationEnded(ConversationEndReason.COMPLETED.value),
        WsConversationEnded(
            ConversationEndReason.CONTEXT_REJECTED.value,
            ContextRejectionCode.NOT_AUTHORIZED.value,
            "diagnostic",
        ),
        ProtocolRejected(ProtocolRejectionCode.INVALID_STATE, "bad state"),
    ],
)
def test_every_server_event_has_a_strict_round_trip(event) -> None:
    assert server_event_from_json(server_event_to_json(event)) == event


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"session_start","user":null}',
        '{"type":"session_start","unknown":"x"}',
        '{"type":"session_start","type":"session_start"}',
        '{"type":"start_conversation","message":" "}',
        '{"type":"message_begin"}',
        '[]',
        'null',
        '{"type":null}',
        '{"type":1}',
        '{"type":"start_conversation"}',
        '{"type":"start_conversation","message":null}',
        '{"type":"start_conversation","message":1}',
        '{"type":"follow_up_message","message":""}',
        '{"type":"follow_up_timed_out","extra":true}',
        '{"type":"cancel_conversation","extra":true}',
    ],
)
def test_client_schema_is_strict(payload: str) -> None:
    with pytest.raises(InvalidEvent):
        client_event_from_json(payload)


def test_invalid_json_is_distinct() -> None:
    with pytest.raises(InvalidJson):
        client_event_from_json("{")


def test_protocol_rejection_round_trip() -> None:
    event = ProtocolRejected(ProtocolRejectionCode.INVALID_STATE, "bad state")
    assert server_event_from_json(server_event_to_json(event)) == event


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"session_accepted","extra":1}',
        '{"type":"conversation_started","conversation_id":null}',
        '{"type":"assistant_text_chunk","message_id":"m1","text":""}',
        '{"type":"assistant_message_aborted","message_id":"m1","reason":"unknown"}',
        '{"type":"conversation_ended","reason":"completed","context_rejection_code":"unknown_user"}',
        '{"type":"conversation_ended","reason":"context_rejected"}',
        '{"type":"conversation_ended","reason":"unknown"}',
        '{"type":"protocol_rejected","code":"unknown"}',
        '{"type":"conversation_ready","type":"conversation_ready"}',
    ],
)
def test_server_schema_rejects_unknown_null_duplicate_and_wrong_enum_shapes(payload: str) -> None:
    with pytest.raises(InvalidEvent):
        server_event_from_json(payload)


@pytest.mark.parametrize("reason", list(ConversationEndReason))
def test_websocket_terminal_shape_matrix(reason: ConversationEndReason) -> None:
    if reason is ConversationEndReason.CONTEXT_REJECTED:
        for code in ContextRejectionCode:
            event = WsConversationEnded(reason.value, code.value)
            assert server_event_from_json(server_event_to_json(event)) == event
        return
    event = WsConversationEnded(reason.value)
    assert server_event_from_json(server_event_to_json(event)) == event


def test_common_process_id_factory_never_reuses_ids_across_protocol_scopes() -> None:
    ids = {
        *(new_id() for _ in range(1000)),
        *(new_id("session") for _ in range(1000)),
        *(new_id("message") for _ in range(1000)),
    }
    assert len(ids) == 3000
    assert all(ids)
