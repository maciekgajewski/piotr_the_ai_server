from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


DEFAULT_WEBSOCKET_HOST = "0.0.0.0"
DEFAULT_WEBSOCKET_PATH = "/chat"
DEFAULT_WEBSOCKET_FOLLOW_UP_TIMEOUT_SECONDS = 60.0
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_STT_MODEL = "medium"
DEFAULT_STT_LANGUAGE = "pl"
DEFAULT_STT_DEVICE = "auto"
DEFAULT_STT_COMPUTE_TYPE = "default"
DEFAULT_STT_LOCAL_FILES_ONLY = True
DEFAULT_STT_BEAM_SIZE = 5
DEFAULT_STT_CAPTURE_SECONDS = 5.0
DEFAULT_STT_PARTIAL_INTERVAL_SECONDS = 0.75
DEFAULT_STT_PARTIAL_WINDOW_SECONDS = 4.0
DEFAULT_STT_PARTIAL_BEAM_SIZE = 1
DEFAULT_STT_PARTIAL_MAX_BACKLOG_SECONDS = 2.0
DEFAULT_STT_LOG_TRANSCRIPTS = False
DEFAULT_TTS_VOICE = "pl_PL-bass-high"
DEFAULT_TTS_VOLUME = 1.0
DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS = 15.0
DEFAULT_OPEN_MIC_WAKE_PHRASE = "Ryszardzie"
DEFAULT_AUDIO_START_TIMEOUT_SECONDS = 5.0
DEFAULT_AUDIO_EVENT_TIMEOUT_SECONDS = 5.0
DEFAULT_INITIAL_SILENCE_SECONDS = 3.0
DEFAULT_END_SILENCE_SECONDS = 0.9
DEFAULT_SPEECH_PEAK_THRESHOLD = 500
DEFAULT_POST_SPEECH_IGNORE_SECONDS = 1.0
DEFAULT_CACHE_DIR = "~/.ai-server/cache/"
DEFAULT_DATA_DIR = "~/.ai-server/data/"
DEFAULT_SPEAKER_RECOGNITION_TIMEOUT_SECONDS = 1.0
DEFAULT_PROCESSING_UPDATE_INTERVAL_SECONDS = 5.0
DEFAULT_PROCESSING_UPDATE_SPOKEN_CUES = ("Hmm...", "Myslę....", "momencik...")
LOG_LEVELS = frozenset(("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
STT_DEVICES = frozenset(("auto", "cuda", "cpu"))
PCM16_MAX_POSITIVE = 32767


@dataclass(frozen=True)
class WebsocketConfig:
    port: int
    host: str = DEFAULT_WEBSOCKET_HOST
    path: str = DEFAULT_WEBSOCKET_PATH
    follow_up_timeout_seconds: float = DEFAULT_WEBSOCKET_FOLLOW_UP_TIMEOUT_SECONDS


@dataclass(frozen=True)
class AgentConfig:
    type: str
    options: dict[str, Any]


@dataclass(frozen=True)
class ServerConfig:
    timezone: str | None = None
    location: str | None = None


@dataclass(frozen=True)
class SttConfig:
    model: str = DEFAULT_STT_MODEL
    language: str = DEFAULT_STT_LANGUAGE
    device: str = DEFAULT_STT_DEVICE
    compute_type: str = DEFAULT_STT_COMPUTE_TYPE
    local_files_only: bool = DEFAULT_STT_LOCAL_FILES_ONLY
    beam_size: int = DEFAULT_STT_BEAM_SIZE
    capture_seconds: float = DEFAULT_STT_CAPTURE_SECONDS
    partial_interval_seconds: float = DEFAULT_STT_PARTIAL_INTERVAL_SECONDS
    partial_window_seconds: float = DEFAULT_STT_PARTIAL_WINDOW_SECONDS
    partial_beam_size: int = DEFAULT_STT_PARTIAL_BEAM_SIZE
    partial_max_backlog_seconds: float = DEFAULT_STT_PARTIAL_MAX_BACKLOG_SECONDS
    log_transcripts: bool = DEFAULT_STT_LOG_TRANSCRIPTS


@dataclass(frozen=True)
class TtsConfig:
    voice: str = DEFAULT_TTS_VOICE
    volume: float = DEFAULT_TTS_VOLUME


@dataclass(frozen=True)
class ConversationConfig:
    follow_up_timeout_seconds: float = DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ProcessingUpdatesConfig:
    interval_seconds: float = DEFAULT_PROCESSING_UPDATE_INTERVAL_SECONDS
    spoken_cues: tuple[str, ...] = DEFAULT_PROCESSING_UPDATE_SPOKEN_CUES


@dataclass(frozen=True)
class SpeakerRecognitionConfig:
    url: str | None = None
    timeout_seconds: float = DEFAULT_SPEAKER_RECOGNITION_TIMEOUT_SECONDS


@dataclass(frozen=True)
class MicrophoneDefaultsConfig:
    open_mic_wake_phrase: str = DEFAULT_OPEN_MIC_WAKE_PHRASE
    audio_start_timeout_seconds: float = DEFAULT_AUDIO_START_TIMEOUT_SECONDS
    audio_event_timeout_seconds: float = DEFAULT_AUDIO_EVENT_TIMEOUT_SECONDS
    initial_silence_seconds: float = DEFAULT_INITIAL_SILENCE_SECONDS
    end_silence_seconds: float = DEFAULT_END_SILENCE_SECONDS
    speech_peak_threshold: int = DEFAULT_SPEECH_PEAK_THRESHOLD
    post_speech_ignore_seconds: float = DEFAULT_POST_SPEECH_IGNORE_SECONDS
    follow_up_timeout_seconds: float = DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS


@dataclass(frozen=True)
class MicrophoneConfig:
    type: str
    name: str
    area: str | None
    options: dict[str, Any]
    open_mic: bool = False
    audio_start_timeout_seconds: float = DEFAULT_AUDIO_START_TIMEOUT_SECONDS
    audio_event_timeout_seconds: float = DEFAULT_AUDIO_EVENT_TIMEOUT_SECONDS
    initial_silence_seconds: float = DEFAULT_INITIAL_SILENCE_SECONDS
    end_silence_seconds: float = DEFAULT_END_SILENCE_SECONDS
    speech_peak_threshold: int = DEFAULT_SPEECH_PEAK_THRESHOLD
    post_speech_ignore_seconds: float = DEFAULT_POST_SPEECH_IGNORE_SECONDS
    follow_up_timeout_seconds: float = DEFAULT_FOLLOW_UP_TIMEOUT_SECONDS


@dataclass(frozen=True)
class Config:
    agent: AgentConfig
    websocket: WebsocketConfig
    log_level: str = DEFAULT_LOG_LEVEL
    server: ServerConfig = ServerConfig()
    cache_dir: Path = Path(DEFAULT_CACHE_DIR).expanduser()
    data_dir: Path = Path(DEFAULT_DATA_DIR).expanduser()
    stt: SttConfig = SttConfig()
    tts: TtsConfig = TtsConfig()
    conversation: ConversationConfig = ConversationConfig()
    processing_updates: ProcessingUpdatesConfig = ProcessingUpdatesConfig()
    speaker_recognition: SpeakerRecognitionConfig = SpeakerRecognitionConfig()
    microphone_defaults: MicrophoneDefaultsConfig = MicrophoneDefaultsConfig()
    microphones: tuple[MicrophoneConfig, ...] = ()
    users: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_config_from_yaml(path: str | Path) -> Config:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as config_file:
        raw_config = yaml.safe_load(config_file)

    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, dict):
        raise ValueError("config must be a YAML mapping")
    if "default_user" in raw_config:
        raise ValueError("default_user has been removed; identify users explicitly or omit user for anonymous requests")
    if "home_assistant_user_settings" in raw_config:
        raise ValueError("home_assistant_user_settings has moved to users.<user>.home_assistant_user_id")

    websocket_config = raw_config.get("websocket")
    if not isinstance(websocket_config, dict):
        raise ValueError("config must contain a websocket mapping")

    agent_config = raw_config.get("agent")
    if not isinstance(agent_config, dict):
        raise ValueError("config must contain an agent mapping")

    conversation = _parse_conversation_config(raw_config.get("conversation", {}))
    microphone_defaults, microphones = _parse_microphones_config(
        raw_config.get("microphones", []),
        legacy_follow_up_timeout_seconds=conversation.follow_up_timeout_seconds,
    )

    return Config(
        agent=_parse_agent_config(agent_config, raw_config.get("home_assistant")),
        websocket=_parse_websocket_config(websocket_config),
        log_level=_parse_log_level(raw_config),
        server=_parse_server_config(raw_config.get("server", {})),
        cache_dir=_parse_cache_dir(raw_config.get("cache_dir", DEFAULT_CACHE_DIR)),
        data_dir=_parse_data_dir(raw_config.get("data_dir", DEFAULT_DATA_DIR)),
        stt=_parse_stt_config(raw_config.get("stt", {})),
        tts=_parse_tts_config(raw_config.get("tts", {})),
        conversation=conversation,
        processing_updates=_parse_processing_updates_config(raw_config.get("processing_updates", {})),
        speaker_recognition=_parse_speaker_recognition_config(raw_config.get("speaker_recognition", {})),
        microphone_defaults=microphone_defaults,
        microphones=microphones,
        users=_parse_users_config(raw_config.get("users", {})),
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

    if agent_type == "orchestrator":
        orchestrator_model = options.get("orchestrator_model")
        if not isinstance(orchestrator_model, str) or not orchestrator_model:
            raise ValueError("agent.orchestrator_model must be a non-empty string for orchestrator")
        if "model" in options:
            raise ValueError("agent.model has been renamed to agent.cloud_model for orchestrator")
        if "fallback_model" in options:
            raise ValueError("agent.fallback_model has been renamed to agent.local_model for orchestrator")
        cloud_model = options.get("cloud_model")
        if not isinstance(cloud_model, str) or not cloud_model:
            raise ValueError("agent.cloud_model must be a non-empty string for orchestrator")
        local_model = options.get("local_model")
        if local_model is not None and (not isinstance(local_model, str) or not local_model):
            raise ValueError("agent.local_model must be a non-empty string when provided")
        _validate_optional_non_empty_string(options, "clarification_model", "agent.clarification_model")
        if "fallback_backoff_seconds" in options:
            options["fallback_backoff_seconds"] = _parse_optional_positive_float(
                options.get("fallback_backoff_seconds"),
                300.0,
                "agent.fallback_backoff_seconds",
            )
        domain_agents = options.get("domain_agents", {})
        if not isinstance(domain_agents, dict):
            raise ValueError("agent.domain_agents must be a mapping for orchestrator")
        _validate_domain_agent_model_keys(domain_agents)

    return AgentConfig(
        type=agent_type,
        options=options,
    )


def _validate_domain_agent_model_keys(domain_agents: dict[str, Any]) -> None:
    for domain, raw_options in domain_agents.items():
        if not isinstance(raw_options, dict):
            continue
        if "model" in raw_options:
            raise ValueError(f"agent.domain_agents.{domain}.model has been renamed to agent.domain_agents.{domain}.cloud_model")
        if "fallback_model" in raw_options:
            raise ValueError(
                f"agent.domain_agents.{domain}.fallback_model has been renamed to agent.domain_agents.{domain}.local_model"
            )


def _parse_users_config(raw_config: Any) -> dict[str, dict[str, Any]]:
    if raw_config is None:
        return {}
    if not isinstance(raw_config, dict):
        raise ValueError("users must be a mapping")
    users: dict[str, dict[str, Any]] = {}
    for user, settings in raw_config.items():
        if not isinstance(user, str) or not user:
            raise ValueError("users keys must be non-empty strings")
        if not isinstance(settings, dict):
            raise ValueError(f"users.{user} must be a mapping")
        home_assistant_user_id = settings.get("home_assistant_user_id")
        if home_assistant_user_id is not None and (
            not isinstance(home_assistant_user_id, str) or not home_assistant_user_id
        ):
            raise ValueError(f"users.{user}.home_assistant_user_id must be a non-empty string when provided")
        users[user] = settings
    return users


def _parse_speaker_recognition_config(raw_config: Any) -> SpeakerRecognitionConfig:
    if raw_config is None:
        return SpeakerRecognitionConfig()
    if not isinstance(raw_config, dict):
        raise ValueError("speaker_recognition must be a mapping")

    url = raw_config.get("url")
    if url is not None and (not isinstance(url, str) or not url):
        raise ValueError("speaker_recognition.url must be a non-empty string when provided")

    timeout_seconds = _parse_optional_positive_float(
        raw_config.get("timeout_seconds"),
        DEFAULT_SPEAKER_RECOGNITION_TIMEOUT_SECONDS,
        "speaker_recognition.timeout_seconds",
    )
    return SpeakerRecognitionConfig(url=url, timeout_seconds=timeout_seconds)


def _validate_optional_non_empty_string(options: dict[str, Any], key: str, field: str) -> None:
    value = options.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError(f"{field} must be a non-empty string when provided")


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

    follow_up_timeout_seconds = _parse_optional_positive_float(
        raw_config.get("follow_up_timeout_seconds"),
        DEFAULT_WEBSOCKET_FOLLOW_UP_TIMEOUT_SECONDS,
        "websocket.follow_up_timeout_seconds",
    )

    return WebsocketConfig(
        port=port,
        host=host,
        path=path,
        follow_up_timeout_seconds=follow_up_timeout_seconds,
    )


def _parse_server_config(raw_config: Any) -> ServerConfig:
    if not isinstance(raw_config, dict):
        raise ValueError("server must be a mapping")

    timezone = raw_config.get("timezone")
    if timezone is not None and (not isinstance(timezone, str) or not timezone):
        raise ValueError("server.timezone must be a non-empty string when provided")

    location = raw_config.get("location")
    if location is not None and (not isinstance(location, str) or not location):
        raise ValueError("server.location must be a non-empty string when provided")

    return ServerConfig(timezone=timezone, location=location)


def _parse_cache_dir(raw_config: Any) -> Path:
    if not isinstance(raw_config, str) or not raw_config:
        raise ValueError("cache_dir must be a non-empty string")
    return Path(raw_config).expanduser()


def _parse_data_dir(raw_config: Any) -> Path:
    if not isinstance(raw_config, str) or not raw_config:
        raise ValueError("data_dir must be a non-empty string")
    return Path(raw_config).expanduser()


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

    compute_type = raw_config.get("compute_type", DEFAULT_STT_COMPUTE_TYPE)
    if not isinstance(compute_type, str) or not compute_type:
        raise ValueError("stt.compute_type must be a non-empty string")

    local_files_only = raw_config.get("local_files_only", DEFAULT_STT_LOCAL_FILES_ONLY)
    if not isinstance(local_files_only, bool):
        raise ValueError("stt.local_files_only must be a boolean")

    beam_size = raw_config.get("beam_size", DEFAULT_STT_BEAM_SIZE)
    if not isinstance(beam_size, int) or isinstance(beam_size, bool) or beam_size <= 0:
        raise ValueError("stt.beam_size must be a positive integer")

    capture_seconds = raw_config.get("capture_seconds", DEFAULT_STT_CAPTURE_SECONDS)
    if not isinstance(capture_seconds, (int, float)) or isinstance(capture_seconds, bool) or capture_seconds <= 0:
        raise ValueError("stt.capture_seconds must be a positive number")

    partial_interval_seconds = _parse_optional_positive_float(
        raw_config.get("partial_interval_seconds"),
        DEFAULT_STT_PARTIAL_INTERVAL_SECONDS,
        "stt.partial_interval_seconds",
    )
    partial_window_seconds = _parse_optional_positive_float(
        raw_config.get("partial_window_seconds"),
        DEFAULT_STT_PARTIAL_WINDOW_SECONDS,
        "stt.partial_window_seconds",
    )
    partial_beam_size = raw_config.get("partial_beam_size", DEFAULT_STT_PARTIAL_BEAM_SIZE)
    if (
        not isinstance(partial_beam_size, int)
        or isinstance(partial_beam_size, bool)
        or partial_beam_size <= 0
    ):
        raise ValueError("stt.partial_beam_size must be a positive integer")
    partial_max_backlog_seconds = _parse_optional_positive_float(
        raw_config.get("partial_max_backlog_seconds"),
        DEFAULT_STT_PARTIAL_MAX_BACKLOG_SECONDS,
        "stt.partial_max_backlog_seconds",
    )
    log_transcripts = raw_config.get("log_transcripts", DEFAULT_STT_LOG_TRANSCRIPTS)
    if not isinstance(log_transcripts, bool):
        raise ValueError("stt.log_transcripts must be a boolean")

    return SttConfig(
        model=model,
        language=language,
        device=device,
        compute_type=compute_type,
        local_files_only=local_files_only,
        beam_size=beam_size,
        capture_seconds=float(capture_seconds),
        partial_interval_seconds=partial_interval_seconds,
        partial_window_seconds=partial_window_seconds,
        partial_beam_size=partial_beam_size,
        partial_max_backlog_seconds=partial_max_backlog_seconds,
        log_transcripts=log_transcripts,
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


def _parse_processing_updates_config(raw_config: Any) -> ProcessingUpdatesConfig:
    if not isinstance(raw_config, dict):
        raise ValueError("processing_updates must be a mapping")

    interval_seconds = _parse_optional_positive_float(
        raw_config.get("interval_seconds"),
        DEFAULT_PROCESSING_UPDATE_INTERVAL_SECONDS,
        "processing_updates.interval_seconds",
    )
    raw_spoken_cues = raw_config.get("spoken_cues", DEFAULT_PROCESSING_UPDATE_SPOKEN_CUES)
    if not isinstance(raw_spoken_cues, (list, tuple)):
        raise ValueError("processing_updates.spoken_cues must be a list")
    if not raw_spoken_cues:
        raise ValueError("processing_updates.spoken_cues must contain at least one cue")
    spoken_cues = []
    for index, cue in enumerate(raw_spoken_cues):
        if not isinstance(cue, str) or not cue:
            raise ValueError(f"processing_updates.spoken_cues[{index}] must be a non-empty string")
        spoken_cues.append(cue)

    return ProcessingUpdatesConfig(
        interval_seconds=interval_seconds,
        spoken_cues=tuple(spoken_cues),
    )


def _parse_microphones_config(
    raw_config: Any,
    legacy_follow_up_timeout_seconds: float,
) -> tuple[MicrophoneDefaultsConfig, tuple[MicrophoneConfig, ...]]:
    if raw_config is None:
        return MicrophoneDefaultsConfig(follow_up_timeout_seconds=legacy_follow_up_timeout_seconds), ()
    if isinstance(raw_config, list):
        defaults = MicrophoneDefaultsConfig(follow_up_timeout_seconds=legacy_follow_up_timeout_seconds)
        raw_microphones = raw_config
    elif isinstance(raw_config, dict):
        defaults = _parse_microphone_defaults(raw_config, legacy_follow_up_timeout_seconds)
        raw_microphones = raw_config.get("devices", [])
        if not isinstance(raw_microphones, list):
            raise ValueError("microphones.devices must be a list")
    else:
        raise ValueError("microphones must be a list or mapping")

    microphones = []
    for index, raw_microphone in enumerate(raw_microphones):
        if not isinstance(raw_microphone, dict):
            raise ValueError(f"microphones[{index}] must be a mapping")

        microphone_type = raw_microphone.get("type")
        if not isinstance(microphone_type, str) or not microphone_type:
            raise ValueError(f"microphones[{index}].type must be a non-empty string")

        name = raw_microphone.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"microphones[{index}].name must be a non-empty string")

        if "location" in raw_microphone:
            raise ValueError(f"microphones[{index}].location has been renamed to microphones[{index}].area")

        area = raw_microphone.get("area")
        if area is not None and (not isinstance(area, str) or not area):
            raise ValueError(f"microphones[{index}].area must be a non-empty string when provided")

        open_mic = raw_microphone.get("open_mic", False)
        if not isinstance(open_mic, bool):
            raise ValueError(f"microphones[{index}].open_mic must be a boolean")

        initial_silence_seconds = _parse_optional_positive_float(
            raw_microphone.get("initial_silence_seconds"),
            defaults.initial_silence_seconds,
            f"microphones[{index}].initial_silence_seconds",
        )
        audio_start_timeout_seconds = _parse_optional_positive_float(
            raw_microphone.get("audio_start_timeout_seconds"),
            defaults.audio_start_timeout_seconds,
            f"microphones[{index}].audio_start_timeout_seconds",
        )
        audio_event_timeout_seconds = _parse_optional_positive_float(
            raw_microphone.get("audio_event_timeout_seconds"),
            defaults.audio_event_timeout_seconds,
            f"microphones[{index}].audio_event_timeout_seconds",
        )
        end_silence_seconds = _parse_optional_positive_float(
            raw_microphone.get("end_silence_seconds"),
            defaults.end_silence_seconds,
            f"microphones[{index}].end_silence_seconds",
        )
        speech_peak_threshold = _parse_optional_pcm16_threshold(
            raw_microphone.get("speech_peak_threshold"),
            defaults.speech_peak_threshold,
            f"microphones[{index}].speech_peak_threshold",
        )
        post_speech_ignore_seconds = _parse_optional_non_negative_float(
            raw_microphone.get("post_speech_ignore_seconds"),
            defaults.post_speech_ignore_seconds,
            f"microphones[{index}].post_speech_ignore_seconds",
        )
        follow_up_timeout_seconds = _parse_optional_positive_float(
            raw_microphone.get("follow_up_timeout_seconds"),
            defaults.follow_up_timeout_seconds,
            f"microphones[{index}].follow_up_timeout_seconds",
        )

        options = {
            key: value
            for key, value in raw_microphone.items()
            if key
            not in (
                "type",
                "name",
                "area",
                "open_mic",
                "audio_start_timeout_seconds",
                "audio_event_timeout_seconds",
                "initial_silence_seconds",
                "end_silence_seconds",
                "speech_peak_threshold",
                "post_speech_ignore_seconds",
                "follow_up_timeout_seconds",
            )
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
                area=area,
                open_mic=open_mic,
                audio_start_timeout_seconds=audio_start_timeout_seconds,
                audio_event_timeout_seconds=audio_event_timeout_seconds,
                initial_silence_seconds=initial_silence_seconds,
                end_silence_seconds=end_silence_seconds,
                speech_peak_threshold=speech_peak_threshold,
                post_speech_ignore_seconds=post_speech_ignore_seconds,
                follow_up_timeout_seconds=follow_up_timeout_seconds,
                options=options,
            )
        )

    return defaults, tuple(microphones)


def _parse_microphone_defaults(
    raw_config: dict[str, Any],
    legacy_follow_up_timeout_seconds: float,
) -> MicrophoneDefaultsConfig:
    open_mic_wake_phrase = raw_config.get("open_mic_wake_phrase", DEFAULT_OPEN_MIC_WAKE_PHRASE)
    if not isinstance(open_mic_wake_phrase, str) or not open_mic_wake_phrase:
        raise ValueError("microphones.open_mic_wake_phrase must be a non-empty string")

    return MicrophoneDefaultsConfig(
        open_mic_wake_phrase=open_mic_wake_phrase,
        audio_start_timeout_seconds=_parse_optional_positive_float(
            raw_config.get("audio_start_timeout_seconds"),
            DEFAULT_AUDIO_START_TIMEOUT_SECONDS,
            "microphones.audio_start_timeout_seconds",
        ),
        audio_event_timeout_seconds=_parse_optional_positive_float(
            raw_config.get("audio_event_timeout_seconds"),
            DEFAULT_AUDIO_EVENT_TIMEOUT_SECONDS,
            "microphones.audio_event_timeout_seconds",
        ),
        initial_silence_seconds=_parse_optional_positive_float(
            raw_config.get("initial_silence_seconds"),
            DEFAULT_INITIAL_SILENCE_SECONDS,
            "microphones.initial_silence_seconds",
        ),
        end_silence_seconds=_parse_optional_positive_float(
            raw_config.get("end_silence_seconds"),
            DEFAULT_END_SILENCE_SECONDS,
            "microphones.end_silence_seconds",
        ),
        speech_peak_threshold=_parse_optional_pcm16_threshold(
            raw_config.get("speech_peak_threshold"),
            DEFAULT_SPEECH_PEAK_THRESHOLD,
            "microphones.speech_peak_threshold",
        ),
        post_speech_ignore_seconds=_parse_optional_non_negative_float(
            raw_config.get("post_speech_ignore_seconds"),
            DEFAULT_POST_SPEECH_IGNORE_SECONDS,
            "microphones.post_speech_ignore_seconds",
        ),
        follow_up_timeout_seconds=_parse_optional_positive_float(
            raw_config.get("follow_up_timeout_seconds"),
            legacy_follow_up_timeout_seconds,
            "microphones.follow_up_timeout_seconds",
        ),
    )


def _parse_optional_positive_float(value: Any, default: float, field: str) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive number")
    return float(value)


def _parse_optional_non_negative_float(value: Any, default: float, field: str) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return float(value)


def _parse_optional_pcm16_threshold(value: Any, default: int, field: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= PCM16_MAX_POSITIVE:
        raise ValueError(f"{field} must be an integer between 1 and {PCM16_MAX_POSITIVE}")
    return value


def _parse_log_level(raw_config: dict[str, Any]) -> str:
    log_level = raw_config.get("log_level", DEFAULT_LOG_LEVEL)
    if not isinstance(log_level, str):
        raise ValueError("log_level must be a string")

    normalized_log_level = log_level.upper()
    if normalized_log_level not in LOG_LEVELS:
        raise ValueError("log_level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")

    return normalized_log_level
