import pytest

from ai_server.messages import MessageBegin, MessageEnd, MessageFragment, UserMessage
from ai_server.messages import user_message_from_json, user_message_to_events, user_message_to_json


def test_user_message_from_json() -> None:
    assert user_message_from_json('{"text": "hello"}') == UserMessage(text="hello")


def test_user_message_to_json() -> None:
    assert user_message_to_json(UserMessage(text="hello")) == '{"text": "hello"}'


def test_user_message_to_events() -> None:
    assert user_message_to_events(UserMessage(text="hello")) == (
        MessageBegin(),
        MessageFragment(text="hello"),
        MessageEnd(),
    )


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ("not json", "message must be valid JSON"),
        ("[]", "message must be a JSON object"),
        ('{"text": 123}', "message.text must be a string"),
        ("{}", "message.text must be a string"),
    ],
)
def test_user_message_from_json_rejects_invalid_payloads(payload: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        user_message_from_json(payload)
