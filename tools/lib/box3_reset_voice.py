#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio

import aioesphomeapi

from box3_common import DEFAULT_HOST, make_client


async def run(host: str) -> None:
    client = make_client("piotr-box3-reset-voice", host)

    async def handle_start(*_args: object) -> int:
        return 0

    async def handle_stop(_aborted: bool) -> None:
        return None

    await client.connect(login=True)
    try:
        unsubscribe = client.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_stop=handle_stop,
        )
        try:
            client.send_voice_assistant_event(
                aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END,
                None,
            )
            await asyncio.sleep(0.5)
            client.send_voice_assistant_event(
                aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END,
                None,
            )
            await asyncio.sleep(0.5)
        finally:
            unsubscribe()
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send stop/end events to reset the Box voice assistant state.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    args = parser.parse_args()
    asyncio.run(run(args.host))


if __name__ == "__main__":
    main()
