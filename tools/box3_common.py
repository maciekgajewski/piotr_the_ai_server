from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import aioesphomeapi
import yaml


DEFAULT_HOST = "piotr-box3-01-cbfaA8.local"
EXPECTED_NAME = "piotr-box3-01-cbfaa8"
API_PORT = 6053
SECRETS_PATH = Path("firmware/esphome/secrets.yaml")


def load_secrets() -> dict[str, Any]:
    return yaml.safe_load(SECRETS_PATH.read_text())


def make_client(client_info: str, host: str = DEFAULT_HOST) -> aioesphomeapi.APIClient:
    secrets = load_secrets()
    return aioesphomeapi.APIClient(
        host,
        API_PORT,
        password=None,
        client_info=client_info,
        noise_psk=secrets["box3_01_api_key"],
        expected_name=EXPECTED_NAME,
    )


def local_ip_for(host: str, port: int = API_PORT) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, port))
        return sock.getsockname()[0]


async def media_player_key(client: aioesphomeapi.APIClient) -> int:
    entities, _ = await client.list_entities_services()
    for entity in entities:
        if type(entity).__name__ == "MediaPlayerInfo":
            return entity.key
    raise RuntimeError("No media player entity exposed by the Box")
