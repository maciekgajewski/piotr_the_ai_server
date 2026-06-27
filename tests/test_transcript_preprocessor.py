import logging

from ai_server.speech_to_text.transcript_preprocessor import TranscriptPreprocessor


def test_transcript_preprocessor_applies_regex_replacement(caplog) -> None:
    preprocessor = TranscriptPreprocessor("test")

    with caplog.at_level(logging.INFO, logger="ai_server.speech_to_text.transcript_preprocessor"):
        processed = preprocessor.preprocess("włącz tryb ventilacji")

    assert processed == "włącz tryb wentylacji"
    assert "applied transcript replacement pattern='\\\\bventilacji\\\\b'" in caplog.text
    assert "włącz tryb ventilacji" not in caplog.text
    assert "włącz tryb wentylacji" not in caplog.text


def test_transcript_preprocessor_uses_word_boundaries() -> None:
    preprocessor = TranscriptPreprocessor("test")

    assert preprocessor.preprocess("superventilacji") == "superventilacji"
