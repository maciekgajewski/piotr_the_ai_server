from ai_server.utils.text import normalize_text


def test_normalize_text_removes_punctuation_and_lowercases() -> None:
    assert normalize_text("Która godzina?") == "która godzina"


def test_normalize_text_collapses_whitespace_after_punctuation() -> None:
    assert normalize_text("  Która---   godzina???  ") == "która godzina"


def test_normalize_text_preserves_diacritics() -> None:
    assert normalize_text("Żółć!") == "żółć"
