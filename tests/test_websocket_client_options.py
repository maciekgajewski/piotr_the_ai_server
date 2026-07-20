from __future__ import annotations

import pytest

from ai_server import batch_ws_client, chat_client
from ai_server.websocket_client.options import DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS


def test_clients_share_default_and_valid_follow_up_timeout() -> None:
    assert chat_client.parse_args([]).follow_up_timeout_seconds == DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS
    assert batch_ws_client.parse_args([]).follow_up_timeout_seconds == DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS
    assert chat_client.parse_args(["--follow-up-timeout-seconds", "2.5"]).follow_up_timeout_seconds == 2.5
    assert batch_ws_client.parse_args(["--follow-up-timeout-seconds", "2.5"]).follow_up_timeout_seconds == 2.5


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_clients_reject_invalid_follow_up_timeout_as_usage_error(value: str) -> None:
    with pytest.raises(SystemExit) as chat_error:
        chat_client.parse_args(["--follow-up-timeout-seconds", value])
    with pytest.raises(SystemExit) as batch_error:
        batch_ws_client.parse_args(["--follow-up-timeout-seconds", value])
    assert chat_error.value.code == 2
    assert batch_error.value.code == 2


@pytest.mark.parametrize("message", ["", "   ", "\t"])
def test_batch_rejects_blank_messages_before_connection(message: str) -> None:
    with pytest.raises(SystemExit) as error:
        batch_ws_client.parse_args(["--message", message])
    assert error.value.code == 2
