from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from aiohttp import web

from ai_server.agent import create_agent
from ai_server.config import Config, LOG_LEVELS, load_config_from_yaml
from ai_server.websocket_server import create_app

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AI server.")
    parser.add_argument("--config", required=True, help="Path to the YAML config file.")
    parser.add_argument(
        "--log-level",
        choices=sorted(LOG_LEVELS),
        help="Python logging level. Overrides log_level from config.",
    )
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_URL,
        help="Ollama base URL. Deployment entrypoints should set this.",
    )
    return parser.parse_args(argv)


def configure_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def run_server(config: Config, ollama_url: str) -> None:
    logger = logging.getLogger(f"{__name__}.server")
    agent = None
    runner = None

    try:
        agent = await create_agent(config.agent, ollama_url)
        app = create_app(config, agent)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, config.websocket.host, config.websocket.port)
        await site.start()

        logger.info(
            "AI server listening on ws://%s:%s%s",
            config.websocket.host,
            config.websocket.port,
            config.websocket.path,
        )

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signame in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(getattr(signal, signame), stop_event.set)
            except NotImplementedError:
                pass

        await stop_event.wait()
    finally:
        if runner is not None:
            await runner.cleanup()
        if agent is not None:
            await agent.close()


def main(argv: list[str] | None = None) -> int:
    logger = logging.getLogger(f"{__name__}.server")
    args = parse_args(argv)
    config = load_config_from_yaml(args.config)
    configure_logging(args.log_level or config.log_level)
    try:
        asyncio.run(run_server(config, args.ollama_url))
    except KeyboardInterrupt:
        logger.info("AI server stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
