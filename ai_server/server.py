from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from aiohttp import web

from ai_server.agent import create_agent
from ai_server.config import Config, LOG_LEVELS, load_config_from_yaml
from ai_server.home_assistant import HomeAssistantConnection, parse_home_assistant_options
from ai_server.microphones import init_mics
from ai_server.user_settings import create_user_settings_provider
from ai_server.websocket_server import create_app

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
THIRD_PARTY_LOGGERS = (
    "aioesphomeapi",
    "httpcore",
    "httpx",
    "tzlocal",
    "urllib3",
    "zeroconf",
)
QUIET_THIRD_PARTY_LOGGERS = (
    "aioesphomeapi.connection",
)


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
    level = getattr(logging, log_level)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger().setLevel(level)
    for logger_name in THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.INFO)
    for logger_name in QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


async def run_server(config: Config, ollama_url: str) -> None:
    logger = logging.getLogger(f"{__name__}.server")
    agent = None
    runner = None
    microphone_manager = None
    home_assistant_connection = None

    try:
        home_assistant_connection = create_home_assistant_connection(config)
        if home_assistant_connection is not None:
            await home_assistant_connection.start()
        user_settings_provider = create_user_settings_provider(
            home_assistant_connection=home_assistant_connection,
            fallback_settings=config.users,
        )

        agent = await create_agent(
            config.agent,
            ollama_url,
            home_assistant_connection=home_assistant_connection,
            server_config=config.server,
            processing_updates=config.processing_updates,
            cache_dir=config.cache_dir,
        )
        microphone_manager = await init_mics(
            config.microphones,
            config.stt,
            config.tts,
            config.conversation,
            config.speaker_recognition,
            agent,
            default_user=config.default_user,
            user_settings=config.users,
            user_settings_provider=user_settings_provider,
            processing_update_spoken_cues=config.processing_updates.spoken_cues,
        )
        app = create_app(config, agent, user_settings_provider=user_settings_provider)
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
        if microphone_manager is not None:
            await microphone_manager.close()
        if runner is not None:
            await runner.cleanup()
        if agent is not None:
            await agent.close()
        if home_assistant_connection is not None:
            await home_assistant_connection.close()


def create_home_assistant_connection(config: Config) -> HomeAssistantConnection | None:
    if "home_assistant" not in config.agent.options:
        return None
    return HomeAssistantConnection(parse_home_assistant_options(config.agent.options))


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
