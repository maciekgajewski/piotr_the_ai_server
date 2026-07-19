import pytest

from ai_server.conversations.contexts import ConversationContext, ConversationMedium, InputConversationContext


def test_conversation_context_is_typed_and_deeply_immutable() -> None:
    source = {"media": {"aliases": ["work"]}}
    context = ConversationContext(
        conversation_id="c1",
        input_session_id="s1",
        medium=ConversationMedium.VOICE,
        user="Maciek",
        area="office",
        user_settings=source,
    )
    source["media"]["aliases"].append("changed")
    assert context.user_settings["media"]["aliases"] == ("work",)
    with pytest.raises(TypeError):
        context.user_settings["x"] = 1
    with pytest.raises(TypeError):
        context.user_settings["media"]["x"] = 1


def test_input_context_has_no_generic_attributes_or_mutable_state() -> None:
    context = InputConversationContext("c1", "s1", ConversationMedium.TEXT, area="office")
    assert not hasattr(context, "attributes")
    assert not hasattr(context, "state")
    with pytest.raises(AttributeError):
        context.area = "bedroom"
