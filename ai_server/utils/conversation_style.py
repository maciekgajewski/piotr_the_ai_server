from __future__ import annotations

from ai_server.conversations.contexts import ConversationMedium


def reply_style_instruction(medium: ConversationMedium) -> str:
    if medium is ConversationMedium.VOICE:
        return (
            "Reply style for conversation.medium=voice: write brief, speech-friendly Polish. "
            "Do not use Markdown tables, bullets, or numbered lists. Prefer writing numbers and dates as Polish words, "
            "but keep exact years, coordinates, identifiers, URLs, and source values as digits when precision matters."
        )
    if medium is ConversationMedium.TEXT:
        return (
            "Reply style for conversation.medium=text: write clear Polish for reading. "
            "Use Markdown bullets, numbered lists, tables, digits, and normal date formats when they improve clarity."
        )
    raise AssertionError(f"unsupported conversation medium: {medium!r}")


def system_prompt_with_reply_style(system_prompt: str, medium: ConversationMedium) -> str:
    return f"{system_prompt.strip()}\n\n{reply_style_instruction(medium)}"
