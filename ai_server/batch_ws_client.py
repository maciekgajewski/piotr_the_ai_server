from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession

from ai_server.websocket_client.aiohttp_transport import AiohttpTransport
from ai_server.websocket_client.connection import ConnectionInterrupted, connect
from ai_server.websocket_client.batch_presenter import BatchPresenter
from ai_server.websocket_client.clock import SystemClock
from ai_server.websocket_client.engine import ClientEngine
from ai_server.websocket_client.messages import ExitKind, ExitResult
from ai_server.websocket_client.options import DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS, DEFAULT_WEBSOCKET_URL, WEBSOCKET_HEARTBEAT_SECONDS
from ai_server.websocket_client.options import positive_finite_timeout


@dataclass(frozen=True)
class BatchWsClientOptions:
    url: str
    user: str | None
    area: str | None
    messages: tuple[str, ...]
    follow_up_timeout_seconds: float


def _timeout_argument(value: str) -> float:
    try:
        return positive_finite_timeout(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _message_argument(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("message must not be empty or whitespace")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send batch messages to the AI server over websocket.")
    parser.add_argument("--user")
    parser.add_argument("--area")
    parser.add_argument("--follow-up-timeout-seconds", default=DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS, type=_timeout_argument)
    parser.add_argument("--message", action="append", default=[], type=_message_argument)
    parser.add_argument("url", nargs="?", default=DEFAULT_WEBSOCKET_URL)
    return parser.parse_args(argv)


async def run_batch_ws_client(options: BatchWsClientOptions) -> ExitResult:
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    clock = SystemClock()
    presenter = BatchPresenter(options.messages, clock)
    try:
        async with ClientSession() as session:
            websocket = await connect(session, options.url, WEBSOCKET_HEARTBEAT_SECONDS, stop_event)
            async with websocket:
                return await ClientEngine(AiohttpTransport(websocket), presenter, user=options.user, area=options.area,
                                          follow_up_timeout_seconds=options.follow_up_timeout_seconds, stop_event=stop_event,
                                          clock=clock).run()
    except (asyncio.TimeoutError, ClientError, OSError) as exc:
        return ExitResult(ExitKind.CONNECTION_FAILED, str(exc))
    except ConnectionInterrupted:
        return ExitResult(ExitKind.INTERRUPTED)
    except Exception as exc:
        return ExitResult(ExitKind.LOCAL_FAILURE, str(exc))


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        with suppress(NotImplementedError):
            loop.add_signal_handler(getattr(signal, signame), stop_event.set)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = asyncio.run(run_batch_ws_client(BatchWsClientOptions(args.url, args.user, args.area, tuple(args.message), args.follow_up_timeout_seconds)))
    if result.detail:
        print(f"{result.kind.value.replace('_', ' ')}: {result.detail}", file=sys.stderr, flush=True)
    return result.code


if __name__ == "__main__":
    raise SystemExit(main())
