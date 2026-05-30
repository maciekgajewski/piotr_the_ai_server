import pytest

from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, NewConversation, SessionAttributes
from ai_server.messages import TextMessage, WaitForNewConversation, endpoint_event_from_json, endpoint_event_to_json
from ai_server.messages import session_event_from_json, session_event_to_json, text_message_to_events


def test_endpoint_event_json_roundtrip_preserves_unicode() -> None:
    event = MessageFragment(text="która godzina?")

    payload = endpoint_event_to_json(event)

    assert payload == '{"type": "message_fragment", "text": "która godzina?"}'
    assert endpoint_event_from_json(payload) == event


def test_session_event_json_roundtrip() -> None:
    event = WaitForNewConversation()

    payload = session_event_to_json(event)

    assert payload == '{"type": "wait_for_new_conversation"}'
    assert session_event_from_json(payload) == event


def test_session_attributes_parse_arbitrary_non_empty_string_attributes() -> None:
    assert endpoint_event_from_json('{"type":"session_attributes","attributes":{"user":"Maciek"}}') == (
        SessionAttributes(attributes={"user": "Maciek"})
    )


def test_new_conversation_parse_arbitrary_non_empty_string_attributes() -> None:
    assert endpoint_event_from_json('{"type":"new_conversation","attributes":{"area":"office"}}') == (
        NewConversation(attributes={"area": "office"})
    )


def test_text_message_to_events() -> None:
    assert text_message_to_events(TextMessage(text="hello")) == (
        MessageBegin(),
        MessageFragment(text="hello"),
        MessageEnd(),
    )


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ("not json", "event must be valid JSON"),
        ("[]", "event must be a JSON object"),
        ('{"type":123}', "event.type must be a string"),
        ('{"type":"message_fragment","text":123}', "message_fragment.text must be a string"),
        ('{"type":"session_attributes","attributes":{"user":""}}', "must be a non-empty string"),
        ('{"type":"session_attributes","attributes":{"user":123}}', "must be a non-empty string"),
        ('{"type":"message_begin","extra":true}', "unsupported event fields"),
    ],
)
def test_endpoint_event_from_json_rejects_invalid_payloads(payload: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        endpoint_event_from_json(payload)
