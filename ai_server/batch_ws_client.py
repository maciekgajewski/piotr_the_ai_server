from __future__ import annotations

import argparse
import asyncio
import signal
from dataclasses import dataclass

from aiohttp import ClientSession

from ai_server.ws_client_common import DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS, DEFAULT_WEBSOCKET_URL
from ai_server.ws_client_common import INTERRUPTED_EXIT_CODE, WebsocketDisconnected
from ai_server.ws_client_common import ConversationTerminated
from ai_server.ws_client_common import WEBSOCKET_HEARTBEAT_SECONDS
from ai_server.ws_client_common import WebsocketSessionRejected
from ai_server.ws_client_common import WsClientInterrupted
from ai_server.ws_client_common import handle_websocket_message, receive_websocket_message, send_session_start
from ai_server.ws_client_common import send_follow_up_timed_out, send_user_text, validate_follow_up_timeout


@dataclass(frozen=True)
class BatchWsClientOptions:
    url: str
    user: str | None
    area: str | None
    messages: tuple[str, ...]
    follow_up_timeout_seconds: float


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send batch messages to the AI server over websocket.")
    parser.add_argument("--user", help="Optional session user attribute sent in the websocket handshake.")
    parser.add_argument("--area", help="Optional Home Assistant area attribute sent in the websocket handshake.")
    parser.add_argument(
        "--follow-up-timeout-seconds",
        default=DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
        type=float,
        help=f"Follow-up timeout in seconds. Defaults to {DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS:g}.",
    )
    parser.add_argument(
        "--message",
        action="append",
        default=[],
        help="Message to send. Can be repeated. The client exits on the next wait-state event.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_WEBSOCKET_URL,
        help=f"Websocket URL. Defaults to {DEFAULT_WEBSOCKET_URL}.",
    )
    return parser.parse_args(argv)


async def run_batch_ws_client(options: BatchWsClientOptions) -> None:
    stop_event = asyncio.Event()
    _install_stop_handlers(stop_event)
    async with ClientSession() as session:
        async with session.ws_connect(options.url, heartbeat=WEBSOCKET_HEARTBEAT_SECONDS) as websocket:
            await send_session_start(websocket, options.user, options.area)
            messages = list(options.messages)
            sent_all_messages = not messages
            pending_message = None

            try:
                while True:
                    if pending_message is None:
                        message = await receive_websocket_message(websocket, stop_event)
                    else:
                        message = pending_message
                        pending_message = None
                    wait_state = handle_websocket_message(
                        websocket,
                        message,
                        follow_up_timeout_seconds=options.follow_up_timeout_seconds,
                    )
                    if isinstance(wait_state, ConversationTerminated):
                        continue
                    if wait_state is None:
                        continue

                    if messages:
                        text = messages.pop(0)
                        sent_all_messages = not messages
                        _print_client_message(f"> {text}")
                        await send_user_text(
                            websocket,
                            text,
                            starts_new_conversation=wait_state.starts_new_conversation,
                        )
                        continue

                    if sent_all_messages:
                        if wait_state.follow_up_requested:
                            assert wait_state.timeout_seconds is not None
                            pending_message = await _wait_for_server_or_follow_up_timeout(
                                websocket,
                                stop_event,
                                wait_state.timeout_seconds,
                            )
                            continue
                        return
            except WebsocketSessionRejected as exc:
                _print_client_message(f"Connection rejected: {exc}.")
            except WebsocketDisconnected as exc:
                _print_client_message(f"Connection lost: {exc}.")


async def _wait_for_server_or_follow_up_timeout(
    websocket,
    stop_event: asyncio.Event,
    timeout_seconds: float,
):
    receive_task = asyncio.create_task(receive_websocket_message(websocket, stop_event))
    timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
    try:
        done, _ = await asyncio.wait(
            (receive_task, timeout_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Terminal input/disconnect wins an equal-ready boundary and cancels
        # the semantic timer before any stale outcome can be sent.
        if receive_task in done:
            return receive_task.result()
        await send_follow_up_timed_out(websocket)
        return None
    finally:
        for task in (receive_task, timeout_task):
            if not task.done():
                task.cancel()
        for task in (receive_task, timeout_task):
            if task.done():
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass


def _print_client_message(text: str) -> None:
    print(text, flush=True)


def _install_stop_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, signame), stop_event.set)
        except NotImplementedError:
            pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    options = BatchWsClientOptions(
        url=args.url,
        user=args.user,
        area=args.area,
        messages=tuple(args.message),
        follow_up_timeout_seconds=validate_follow_up_timeout(args.follow_up_timeout_seconds),
    )
    try:
        asyncio.run(run_batch_ws_client(options))
    except (WsClientInterrupted, KeyboardInterrupt):
        return INTERRUPTED_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
