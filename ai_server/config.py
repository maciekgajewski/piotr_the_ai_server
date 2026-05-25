from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_WEBSOCKET_HOST = "0.0.0.0"
DEFAULT_WEBSOCKET_PATH = "/chat"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_STT_MODEL = "medium"
DEFAULT_STT_LANGUAGE = "pl"
DEFAULT_STT_DEVICE = "auto"
DEFAULT_STT_BEAM_SIZE = 5
DEFAULT_STT_CAPTURE_SECONDS = 5.0
DEFAULT_TTS_VOICE = "pl_PL-bass-high"
DEFAULT_TTS_VOLUME = 1.0
DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS = 15.0
LOG_LEVELS = frozenset(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
STT_DEVICES = frozenset(("auto", "cuda", "cpu"))


@dataclass(frozen=True)
class WebsocketConfig:
    port: int
    host: str = DEFAULT_WEBSOCKET_HOST
    path: str = DEFAULT_WEBSOCKET_PATH


@dataclass(frozen=True)
class AgentConfig:
    type: str
    options: dict[str, Any]


@dataclass(frozen=True)
class SttConfig:
    model: str = DEFAULT_STT_MODEL
    language: str = DEFAULT_STT_LANGUAGE
    device: str = DEFAULT_STT_DEVICE
    beam_size: int = DEFAULT_STT_BEAM_SIZE
    capture_seconds: float = DEFAULT_STT_CAPTURE_SECONDS


@dataclass(frozen=True)
class TtsConfig:
    voice: str = DEFAULT_TTS_VOICE
    volume: float = DEFAULT_TTS_VOLUME


@dataclass(frozen=True)
class ConversationConfig:
    follow_up_timeout_seconds: float = DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS


@dataclass(frozen=True)
class MicrophoneConfig:
    type: str
    name: str
    location: str | None
    options: dict[str, Any]
    follow_up_timeout_seconds: float | None = None


@dataclass(frozen=True)
class Config:
    agent: AgentConfig
    websocket: WebsocketConfig
    log_level: str = DEFAULT_LOG_LEVEL
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    conversation: ConversationConfig = ConversationConfig()
    microphones: tuple[MicrophoneConfig, ...] = ()


def load_config_from_yaml(path: str | Path) -> Config:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as config_file:
        raw_config = yaml.safe_load(config_file)

    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise ValueError("config must be a YAML mapping")

    websocket_config = raw_config.get("websocket")
    if not isinstance(websocket_config, dict):
        raise ValueError("config must contain a websocket mapping")

    agent_config = raw_config.get("agent")
    if not isinstance(agent_config, dict):
        raise ValueError("config must contain an agent mapping")

    return Config(
        agent=_parse_agent_config(agent_config, raw_config.get("home_assistant")),
        websocket=_parse_websocket_config(websocket_config),
        log_level=_parse_log_level(raw_config),
        stt=_parse_stt_config(raw_config.get("stt", {})),
        tts=_parse_tts_config(raw_config.get("tts", {})),
        conversation=_parse_conversation_config(raw_config.get("conversation", {})),
        microphones=_parse_microphone_configs(raw_config.get("microphones", [])),
    )


def _parse_agent_config(raw_config: dict[str, Any], home_assistant_config: Any = None) -> AgentConfig:
    agent_type = raw_config.get("type")
    if not isinstance(agent_type, str) or not agent_type:
        raise ValueError("agent.type must be a non-empty string")

    options = {key: value for key, value in raw_config.items() if key != "type"}
    if home_assistant_config is not None:
        options["home_assistant"] = home_assistant_config

    if agent_type == "polite_reply":
        model = options.get("model")
        if not isinstance(model, str) or not model:
            raise ValueError("agent.model must be a non-empty string for polite_reply")

    return AgentConfig(
        type=agent_type,
        options=options,
    )


def _parse_websocket_config(raw_config: dict[str, Any]) -> WebsocketConfig:
    if "port" not in raw_config:
        raise ValueError("websocket.port is required")

    port = raw_config["port"]
    if not isinstance(port, int) or isinstance(port, bool):
        raise ValueError("websocket.port must be an integer")
    if port < 1 or port > 65535:
        raise ValueError("websocket.port must be between 1 and 65535")

    host = raw_config.get("host", DEFAULT_WEBSOCKET_HOST)
    if not isinstance(host, str) or not host:
        raise ValueError("websocket.host must be a non-empty string")

    path = raw_config.get("path", DEFAULT_WEBSOCKET_PATH)
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("websocket.path must be a string starting with '/'")

    return WebsocketConfig(port=port, host=host, path=path)


def _parse_stt_config(raw_config: Any) -> SttConfig:
    if not isinstance(raw_config, dict):
        raise ValueError("stt must be a mapping")

    model = raw_config.get("model", DEFAULT_STT_MODEL)
    if not isinstance(model, str) or not model:
        raise ValueError("stt.model must be a non-empty string")

    language = raw_config.get("language", DEFAULT_STT_LANGUAGE)
    if not isinstance(language, str) or not language:
        raise ValueError("stt.language must be a non-empty string")

    device = raw_config.get("device", DEFAULT_STT_DEVICE)
    if not isinstance(device, str) or device not in STT_DEVICES:
        raise ValueError("stt.device must be one of auto, cuda, cpu")

    beam_size = raw_config.get("beam_size", DEFAULT_STT_BEAM_SIZE)
    if not isinstance(beam_size, int) or isinstance(beam_size, bool) or beam_size <= 0:
        raise ValueError("stt.beam_size must be a positive integer")

    capture_seconds = raw_config.get("capture_seconds", DEFAULT_STT_CAPTURE_SECONDS)
    if not isinstance(capture_seconds, (int, float)) or isinstance(capture_seconds, bool) or capture_seconds <= 0:
        raise ValueError("stt.capture_seconds must be a positive number")

    return SttConfig(
        model=model,
        language=language,
        device=device,
        beam_size=beam_size,
        capture_seconds=float(capture_seconds),
    )


def _parse_tts_config(raw_config: Any) -> TtsConfig:
    if not isinstance(raw_config, dict):
        raise ValueError("tts must be a mapping")

    voice = raw_config.get("voice", DEFAULT_TTS_VOICE)
    if not isinstance(voice, str) or not voice:
        raise ValueError("tts.voice must be a non-empty string")

    volume = raw_config.get("volume", DEFAULT_TTS_VOLUME)
    if not isinstance(volume, (int, float)) or isinstance(volume, bool) or not 0.0 <= volume <= 1.0:
        raise ValueError("tts.volume must be between 0.0 and 1.0")

    return TtsConfig(voice=voice, volume=float(volume))


def _parse_conversation_config(raw_config: Any) -> ConversationConfig:
    if not isinstance(raw_config, dict):
        raise ValueError("conversation must be a mapping")

    follow_up_timeout_seconds = raw_config.get(
        "follow_up_timeout_seconds",
        DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS,
    )
    if (
        not isinstance(follow_up_timeout_seconds, (int, float))
        or isinstance(follow_up_timeout_seconds, bool)
        or follow_up_timeout_seconds <= 0
    ):
        raise ValueError("conversation.follow_up_timeout_seconds must be a positive number")

    return ConversationConfig(follow_up_timeout_seconds=float(follow_up_timeout_seconds))


def _parse_microphone_configs(raw_config: Any) -> tuple[MicrophoneConfig, ...]:
    if raw_config is None:
        return ()
    if not isinstance(raw_config, list):
        raise ValueError("microphones must be a list")

    microphones = []
    for index, raw_microphone in enumerate(raw_config):
        if not isinstance(raw_microphone, dict):
            raise ValueError(f"microphones[{index}] must be a mapping")

        microphone_type = raw_microphone.get("type")
        if not isinstance(microphone_type, str) or not microphone_type:
            raise ValueError(f"microphones[{index}].type must be a non-empty string")

        name = raw_microphone.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"microphones[{index}].name must be a non-empty string")

        location = raw_microphone.get("location")
        if location is not None and (not isinstance(location, str) or not location):
            raise ValueError(f"microphones[{index}].location must be a non-empty string when provided")

        follow_up_timeout_seconds = raw_microphone.get("follow_up_timeout_seconds")
        if follow_up_timeout_seconds is not None and (
            not isinstance(follow_up_timeout_seconds, (int, float))
            or isinstance(follow_up_timeout_seconds, bool)
            or follow_up_timeout_seconds <= 0
        ):
            raise ValueError(f"microphones[{index}].follow_up_timeout_seconds must be a positive number")

        options = {
            key: value
            for key, value in raw_microphone.items()
            if key not in ("type", "name", "location", "follow_up_timeout_seconds")
        }
        if microphone_type == "box3_esphome":
            address = options.get("address")
            if not isinstance(address, str) or not address:
                raise ValueError(f"microphones[{index}].address must be a non-empty string for box3_esphome")

            api_key = options.get("api_key")
            if not isinstance(api_key, str) or not api_key:
                raise ValueError(f"microphones[{index}].api_key must be a non-empty string for box3_esphome")

        microphones.append(
            MicrophoneConfig(
                type=microphone_type,
                name=name,
                location=location,
                follow_up_timeout_seconds=(
                    None if follow_up_timeout_seconds is None else float(follow_up_timeout_seconds)
                ),
                options=options,
            )
        )

    return tuple(microphones)


def _parse_log_level(raw_config: dict[str, Any]) -> str:
    log_level = raw_config.get("log_level", DEFAULT_LOG_LEVEL)
    if not isinstance(log_level, str):
        raise ValueError("log_level must be a string")

    normalized_log_level = log_level.upper()
    if normalized_log_level not in LOG_LEVELS:
        raise ValueError("log_level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")

    return normalized_log_level
