from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, TypeAlias


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class MessageBegin:
    pass


@dataclass(frozen=True)
class MessageFragment:
    text: str


@dataclass(frozen=True)
class MessageEnd:
    pass


MessageEvent: TypeAlias = MessageBegin | MessageFragment | MessageEnd


def user_message_from_json(payload: str) -> UserMessage:
    try:
        raw_message = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("message must be valid JSON") from exc

    if not isinstance(raw_message, dict):
        raise ValueError("message must be a JSON object")

    return user_message_from_mapping(raw_message)


def user_message_from_mapping(raw_message: dict[str, Any]) -> UserMessage:
    text = raw_message.get("text")
    if not isinstance(text, str):
        raise ValueError("message.text must be a string")

    return UserMessage(text=text)


def user_message_to_json(message: UserMessage) -> str:
    return json.dumps({"text": message.text}, ensure_ascii=False)


def user_message_to_events(message: UserMessage) -> tuple[MessageEvent, ...]:
    return (
        MessageBegin(),
        MessageFragment(text=message.text),
        MessageEnd(),
    )
