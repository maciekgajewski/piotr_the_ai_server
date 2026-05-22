import logging

from ai_server.server import THIRD_PARTY_LOGGERS, configure_logging


def test_configure_logging_keeps_third_party_loggers_at_info() -> None:
    configure_logging("DEBUG")

    for logger_name in THIRD_PARTY_LOGGERS:
        assert logging.getLogger(logger_name).level == logging.INFO

    assert logging.getLogger("ai_server").getEffectiveLevel() == logging.DEBUG
