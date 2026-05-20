#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio

from box3_common import DEFAULT_HOST, make_client


async def run(host: str) -> None:
    client = make_client("piotr-box3-api-probe", host)
    await client.connect(login=True)
    try:
        info = await client.device_info()
        entities, services = await client.list_entities_services()
        print(f"connected name={info.name} mac={info.mac_address} version={info.esphome_version} model={info.model}")
        print(f"entities={len(entities)} services={len(services)}")
        for entity in entities:
            object_id = getattr(entity, "object_id", "")
            name = getattr(entity, "name", "")
            key = getattr(entity, "key", "")
            print(f"{type(entity).__name__} object_id={object_id} name={name} key={key}")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe the ESP32-S3-BOX-3 ESPHome API.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    args = parser.parse_args()
    asyncio.run(run(args.host))


if __name__ == "__main__":
    main()
