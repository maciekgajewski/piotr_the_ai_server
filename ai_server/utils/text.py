from __future__ import annotations

import unicodedata


def normalize_text(value: str) -> str:
    without_punctuation = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in value.lower()
    )
    return " ".join(without_punctuation.split())


def ascii_fold(value: str) -> str:
    value = value.translate(str.maketrans({"ł": "l", "Ł": "L"}))
    folded = unicodedata.normalize("NFKD", value)
    return "".join(character for character in folded if not unicodedata.combining(character))
