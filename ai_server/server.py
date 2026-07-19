from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from aiohttp import web

from ai_server.agent import create_agent
from ai_server.config import Config, LOG_LEVELS, load_config_from_yaml
from ai_server.conversations.bridge import FatalTerminationController
from ai_server.conversations.context_provider import ConfigContextProvider
from ai_server.home_assistant import HomeAssistantConnection, parse_home_assistant_options
from ai_server.microphones.manager import init_mics
from ai_server.user_settings import create_user_settings_provider
from ai_server.utils import JsonFileStore
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
WARNING_THIRD_PARTY_LOGGERS = (
    "faster_whisper",
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
    for logger_name in WARNING_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    for logger_name in QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.ERROR)


async def run_server(config: Config, ollama_url: str) -> None:
    logger = logging.getLogger(f"{__name__}.server")
    agent = None
    runner = None
    microphone_manager = None
    home_assistant_connection = None
    fatal_termination = _ProcessFatalTerminationController()

    try:
        home_assistant_connection = create_home_assistant_connection(config)
        if home_assistant_connection is not None:
            await home_assistant_connection.start()
        user_settings_provider = create_user_settings_provider(
            home_assistant_connection=home_assistant_connection,
            fallback_settings=config.users,
        )
        context_provider = await _build_context_snapshot(config, user_settings_provider)

        agent = await create_agent(
            config.agent,
            ollama_url,
            home_assistant_connection=home_assistant_connection,
            server_config=config.server,
            processing_updates=config.processing_updates,
            cache_dir=config.cache_dir,
            data_store=JsonFileStore(config.data_dir),
        )
        microphone_manager = await init_mics(
            config.microphones,
            config.stt,
            config.tts,
            config.conversation,
            config.speaker_recognition,
            agent,
            user_settings=config.users,
            context_provider=context_provider,
            fatal_termination=fatal_termination,
            processing_update_spoken_cues=config.processing_updates.spoken_cues,
            open_mic_wake_phrase=config.microphone_defaults.open_mic_wake_phrase,
        )
        app = create_app(
            config,
            agent,
            context_provider=context_provider,
            fatal_termination=fatal_termination,
        )
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
        shutdown_started = False
        loop = asyncio.get_running_loop()

        def request_shutdown() -> None:
            nonlocal shutdown_started
            if shutdown_started:
                fatal_termination.hard_exit("second signal during shutdown")
            shutdown_started = True
            stop_event.set()

        for signame in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(getattr(signal, signame), request_shutdown)
            except NotImplementedError:
                pass

        await stop_event.wait()
    finally:
        try:
            await asyncio.wait_for(
                _close_runtime(
                    microphone_manager=microphone_manager,
                    runner=runner,
                    agent=agent,
                    home_assistant_connection=home_assistant_connection,
                ),
                timeout=config.shutdown.grace_period_seconds,
            )
        except asyncio.TimeoutError:
            fatal_termination.hard_exit("shutdown grace period exceeded")


async def _build_context_snapshot(config: Config, user_settings_provider) -> ConfigContextProvider:
    users = {}
    for user in config.users:
        users[user] = await user_settings_provider.settings_for_user(user)
    return ConfigContextProvider(users)


async def _close_runtime(*, microphone_manager, runner, agent, home_assistant_connection) -> None:
    input_closers = []
    if microphone_manager is not None:
        input_closers.append(microphone_manager.close())
    if runner is not None:
        input_closers.append(runner.cleanup())
    if input_closers:
        await asyncio.gather(*input_closers)
    if agent is not None:
        await agent.close()
    if home_assistant_connection is not None:
        await home_assistant_connection.close()


class _ProcessFatalTerminationController(FatalTerminationController):
    async def terminate(self, detail: str):
        self.hard_exit(detail)

    def hard_exit(self, detail: str) -> None:
        logging.getLogger(f"{__name__}.server").critical("fatal process termination detail=%s", detail)
        os._exit(1)


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
