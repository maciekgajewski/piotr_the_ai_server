import logging

from ai_server.microphones.transcript_preprocessor import TranscriptPreprocessor


def test_transcript_preprocessor_applies_regex_replacement(caplog) -> None:
    preprocessor = TranscriptPreprocessor("test")

    with caplog.at_level(logging.INFO, logger="ai_server.microphones.transcript_preprocessor"):
        processed = preprocessor.preprocess("włącz tryb ventilacji")

    assert processed == "włącz tryb wentylacji"
    assert "applied transcript replacement pattern='\\\\bventilacji\\\\b'" in caplog.text
    assert "before='włącz tryb ventilacji'" in caplog.text
    assert "after='włącz tryb wentylacji'" in caplog.text


def test_transcript_preprocessor_uses_word_boundaries() -> None:
    preprocessor = TranscriptPreprocessor("test")

    assert preprocessor.preprocess("superventilacji") == "superventilacji"
