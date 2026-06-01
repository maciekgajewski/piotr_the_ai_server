from __future__ import annotations

import unicodedata


def normalize_text(value: str) -> str:
    without_punctuation = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in value.lower()
    )
    return " ".join(without_punctuation.split())
