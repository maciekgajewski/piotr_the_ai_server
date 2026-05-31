import logging

from ai_server.config import AgentConfig, Config, WebsocketConfig
from ai_server.server import QUIET_THIRD_PARTY_LOGGERS, THIRD_PARTY_LOGGERS, configure_logging
from ai_server.server import create_home_assistant_connection


def test_configure_logging_keeps_third_party_loggers_at_info() -> None:
    configure_logging("DEBUG")

    for logger_name in THIRD_PARTY_LOGGERS:
        expected_level = logging.ERROR if logger_name in QUIET_THIRD_PARTY_LOGGERS else logging.INFO
        assert logging.getLogger(logger_name).level == expected_level
    for logger_name in QUIET_THIRD_PARTY_LOGGERS:
        assert logging.getLogger(logger_name).level == logging.ERROR

    assert logging.getLogger("ai_server").getEffectiveLevel() == logging.DEBUG


def test_create_home_assistant_connection_uses_server_config() -> None:
    config = Config(
        agent=AgentConfig(
            type="assistant",
            options={
                "intent_router_model": "llama3.2:3b",
                "model": "qwen3:8b",
                "home_assistant": {
                    "url": "http://ha.local:8123/",
                    "token": "secret-token",
                    "inventory_refresh_seconds": 12,
                },
            },
        ),
        websocket=WebsocketConfig(port=2137),
    )

    connection = create_home_assistant_connection(config)

    assert connection is not None
    assert connection._options.url == "http://ha.local:8123"
    assert connection._options.inventory_refresh_seconds == 12.0
