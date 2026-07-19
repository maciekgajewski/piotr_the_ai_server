import logging

from ai_server.config import AgentConfig, Config, ConversationConfig, ShutdownConfig, WebsocketConfig
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
        websocket=WebsocketConfig(
            port=2137,
            max_connections=8,
            capacity_retry_after_seconds=3,
            follow_up_idle_lease_seconds=120.0,
            max_frame_bytes=65536,
            ingress_queue_capacity=16,
            heartbeat_seconds=30.0,
            handshake_timeout_seconds=10.0,
        ),
        conversation=ConversationConfig(5.0, 1.0),
        shutdown=ShutdownConfig(15.0),
    )

    connection = create_home_assistant_connection(config)

    assert connection is not None
    assert connection._options.url == "http://ha.local:8123"
    assert connection._options.inventory_refresh_seconds == 12.0
