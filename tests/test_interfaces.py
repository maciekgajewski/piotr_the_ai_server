import pytest

from ai_server.interfaces import Conversation, ConversationMedium


def test_conversation_medium_returns_enum() -> None:
    conversation = Conversation(conversation_id="c1", attributes={"medium": "voice"})

    assert conversation.medium is ConversationMedium.VOICE


@pytest.mark.parametrize("attributes", [{}, {"medium": "phone"}])
def test_conversation_requires_valid_medium(attributes) -> None:
    with pytest.raises(AssertionError, match="conversation.medium must be one of"):
        Conversation(conversation_id="c1", attributes=attributes)
