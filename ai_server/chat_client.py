from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession

from ai_server.websocket_client.aiohttp_transport import AiohttpTransport
from ai_server.websocket_client.connection import ConnectionInterrupted, connect
from ai_server.websocket_client.clock import SystemClock
from ai_server.websocket_client.engine import ClientEngine
from ai_server.websocket_client.messages import ExitKind, ExitResult
from ai_server.websocket_client.options import DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS, DEFAULT_WEBSOCKET_URL, WEBSOCKET_HEARTBEAT_SECONDS
from ai_server.websocket_client.options import positive_finite_timeout
from ai_server.websocket_client.terminal_presenter import TerminalPresenter


@dataclass(frozen=True)
class ChatClientOptions:
    url: str
    user: str | None
    area: str | None
    follow_up_timeout_seconds: float


def _timeout_argument(value: str) -> float:
    try:
        return positive_finite_timeout(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with the AI server over websocket.")
    parser.add_argument("--user")
    parser.add_argument("--area")
    parser.add_argument("--follow-up-timeout-seconds", default=DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS, type=_timeout_argument)
    parser.add_argument("url", nargs="?", default=DEFAULT_WEBSOCKET_URL)
    return parser.parse_args(argv)


async def run_chat(options: ChatClientOptions) -> ExitResult:
    if not TerminalPresenter.validate_tty():
        return ExitResult(ExitKind.USAGE_ERROR, "interactive client requires TTY stdin and stdout; use tools/batch-ws-client.sh")
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    clock = SystemClock()
    presenter = TerminalPresenter(clock)
    try:
        await presenter.start()
        presenter.system(f"Connecting to {options.url} ...")
    except Exception as exc:
        try:
            presenter.system(f"local failure: {exc}")
        except Exception:
            pass
        try:
            await presenter.close()
        except Exception:
            pass
        return ExitResult(ExitKind.LOCAL_FAILURE, str(exc))
    result: ExitResult
    try:
        async with ClientSession() as session:
            websocket = await connect(session, options.url, WEBSOCKET_HEARTBEAT_SECONDS, stop_event)
            async with websocket:
                result = await ClientEngine(AiohttpTransport(websocket), presenter, user=options.user, area=options.area,
                                            follow_up_timeout_seconds=options.follow_up_timeout_seconds, stop_event=stop_event,
                                            clock=clock).run()
                if result.detail and result.kind not in (ExitKind.COMPLETED, ExitKind.EXITED, ExitKind.INTERRUPTED):
                    try:
                        presenter.system(f"{result.kind.value.replace('_', ' ')}: {result.detail}")
                    except Exception as exc:
                        result = ExitResult(ExitKind.LOCAL_FAILURE, str(exc))
                pass
    except (asyncio.TimeoutError, ClientError, OSError) as exc:
        try:
            presenter.system(f"connection failed: {exc}")
        except Exception:
            result = ExitResult(ExitKind.LOCAL_FAILURE, str(exc))
        else:
            result = ExitResult(ExitKind.CONNECTION_FAILED, str(exc))
    except ConnectionInterrupted:
        result = ExitResult(ExitKind.INTERRUPTED)
    except Exception as exc:
        try:
            presenter.system(f"local failure: {exc}")
        except Exception:
            pass
        result = ExitResult(ExitKind.LOCAL_FAILURE, str(exc))
    finally:
        try:
            await presenter.close()
        except Exception as exc:
            result = ExitResult(ExitKind.LOCAL_FAILURE, str(exc))
    return result


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signame), stop_event.set)
        except NotImplementedError:
            pass


def main(argv: list[str] | None = None) -> int:
    result = asyncio.run(run_chat(ChatClientOptions(**vars(parse_args(argv)))))
    if result.detail and result.kind is ExitKind.USAGE_ERROR:
        print(f"{result.kind.value.replace('_', ' ')}: {result.detail}", file=sys.stderr, flush=True)
    return result.code


if __name__ == "__main__":
    raise SystemExit(main())
