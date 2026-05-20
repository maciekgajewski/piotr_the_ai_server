#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import quote

from box3_common import DEFAULT_HOST, local_ip_for, make_client, media_player_key


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


async def run(host: str, audio_file: Path, port: int, wait: float) -> None:
    audio_file = audio_file.resolve()
    if not audio_file.exists():
        raise FileNotFoundError(audio_file)

    local_ip = local_ip_for(host)
    directory = str(audio_file.parent)
    handler = partial(QuietHandler, directory=directory)
    server = ThreadingHTTPServer((local_ip, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    url = f"http://{local_ip}:{server.server_port}/{quote(audio_file.name)}"
    client = make_client("piotr-box3-play-audio", host)
    await client.connect(login=True)
    try:
        key = await media_player_key(client)
        print(f"playing {url}")
        client.media_player_command(key, media_url=url, announcement=True)
        await asyncio.sleep(wait)
    finally:
        await client.disconnect()
        server.shutdown()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve an audio file from Piotr and ask the Box to play it.")
    parser.add_argument("file", type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--wait", type=float, default=8.0)
    args = parser.parse_args()
    asyncio.run(run(args.host, args.file, args.port, args.wait))


if __name__ == "__main__":
    main()
