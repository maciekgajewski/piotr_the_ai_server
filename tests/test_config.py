from pathlib import Path

import pytest

from ai_server.config import (
    AgentConfig,
    ConversationConfig,
    DEFAULT_DATA_DIR,
    MicrophoneDefaultsConfig,
    ProcessingUpdatesConfig,
    ServerConfig,
    DEFAULT_LOG_LEVEL,
    DEFAULT_WEBSOCKET_FOLLOW_UP_TIMEOUT_SECONDS,
    DEFAULT_WEBSOCKET_HOST,
    DEFAULT_WEBSOCKET_PATH,
    Config,
    MicrophoneConfig,
    SpeakerRecognitionConfig,
    SttConfig,
    TtsConfig,
    WebsocketConfig,
    load_config_from_yaml,
)


def write_config(tmp_path: Path, content: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(content, encoding="utf-8")
    return config_path


def test_load_config_with_defaults(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
""",
    )

    assert load_config_from_yaml(config_path) == Config(
        agent=AgentConfig(type="echo", options={}),
        log_level=DEFAULT_LOG_LEVEL,
        websocket=WebsocketConfig(
            host=DEFAULT_WEBSOCKET_HOST,
            port=2137,
            path=DEFAULT_WEBSOCKET_PATH,
            follow_up_timeout_seconds=DEFAULT_WEBSOCKET_FOLLOW_UP_TIMEOUT_SECONDS,
        ),
        data_dir=Path(DEFAULT_DATA_DIR).expanduser(),
    )


def test_load_config_with_explicit_websocket_values(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  host: 127.0.0.1
  port: 2137
  path: /chat
  follow_up_timeout_seconds: 45
agent:
  type: echo
  temperature: 0
""",
    )

    assert load_config_from_yaml(config_path) == Config(
        agent=AgentConfig(type="echo", options={"temperature": 0}),
        log_level=DEFAULT_LOG_LEVEL,
        websocket=WebsocketConfig(host="127.0.0.1", port=2137, path="/chat", follow_up_timeout_seconds=45.0)
    )


def test_load_config_with_user_settings(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
users:
  Maciek:
    media:
      liked_songs_media_id: library://playlist/7
      liked_songs_media_type: playlist
      liked_songs_name: Liked Songs macson_g
websocket:
  port: 2137
agent:
  type: echo
""",
    )

    assert load_config_from_yaml(config_path) == Config(
        agent=AgentConfig(type="echo", options={}),
        log_level=DEFAULT_LOG_LEVEL,
        websocket=WebsocketConfig(
            host=DEFAULT_WEBSOCKET_HOST,
            port=2137,
            path=DEFAULT_WEBSOCKET_PATH,
            follow_up_timeout_seconds=DEFAULT_WEBSOCKET_FOLLOW_UP_TIMEOUT_SECONDS,
        ),
        users={
            "Maciek": {
                "media": {
                    "liked_songs_media_id": "library://playlist/7",
                    "liked_songs_media_type": "playlist",
                    "liked_songs_name": "Liked Songs macson_g",
                }
            }
        },
    )


def test_load_config_with_user_home_assistant_user_id(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
users:
  Maciek:
    home_assistant_user_id: 01HY3C67GQ70R6E7M5F9Q6B7CZ
websocket:
  port: 2137
agent:
  type: echo
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.users["Maciek"]["home_assistant_user_id"] == "01HY3C67GQ70R6E7M5F9Q6B7CZ"


def test_load_config_with_speaker_recognition_and_voice_profile(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
users:
  Maciek:
    voice_profile: /profiles/maciek/speaker_profile.npz
speaker_recognition:
  url: http://127.0.0.1:2140
  timeout_seconds: 0.8
websocket:
  port: 2137
agent:
  type: echo
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.speaker_recognition == SpeakerRecognitionConfig(
        url="http://127.0.0.1:2140",
        timeout_seconds=0.8,
    )
    assert config.users["Maciek"]["voice_profile"] == "/profiles/maciek/speaker_profile.npz"


def test_load_config_with_explicit_log_level(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
log_level: debug
websocket:
  port: 2137
agent:
  type: echo
""",
    )

    assert load_config_from_yaml(config_path) == Config(
        agent=AgentConfig(type="echo", options={}),
        log_level="DEBUG",
        websocket=WebsocketConfig(
            host=DEFAULT_WEBSOCKET_HOST,
            port=2137,
            path=DEFAULT_WEBSOCKET_PATH,
        ),
    )


def test_load_config_with_polite_reply_agent_model(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: polite_reply
  model: qwen3:4b
""",
    )

    assert load_config_from_yaml(config_path) == Config(
        agent=AgentConfig(type="polite_reply", options={"model": "qwen3:4b"}),
        log_level=DEFAULT_LOG_LEVEL,
        websocket=WebsocketConfig(
            host=DEFAULT_WEBSOCKET_HOST,
            port=2137,
            path=DEFAULT_WEBSOCKET_PATH,
        ),
    )


def test_load_config_adds_top_level_home_assistant_to_agent_options(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: assistant
  intent_router_model: llama3.2:3b
home_assistant:
  url: http://ha.local:8123
  token: secret-token
""",
    )

    assert load_config_from_yaml(config_path).agent == AgentConfig(
        type="assistant",
        options={
            "intent_router_model": "llama3.2:3b",
            "home_assistant": {
                "url": "http://ha.local:8123",
                "token": "secret-token",
            },
        },
    )


def test_load_config_with_orchestrator_agent_and_server_defaults(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
cache_dir: ~/.ai-server/cache/
data_dir: ~/.ai-server/data/
server:
  timezone: Europe/Warsaw
  location: Wrocław
websocket:
  port: 2137
agent:
  type: orchestrator
  orchestrator_model: qwen3:4b-instruct
  cloud_model: gpt-oss:20b-cloud
  domain_agents:
    home_assistant:
      model: qwen3:8b
    time: {}
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.server == ServerConfig(timezone="Europe/Warsaw", location="Wrocław")
    assert config.cache_dir == Path("~/.ai-server/cache/").expanduser()
    assert config.data_dir == Path("~/.ai-server/data/").expanduser()
    assert config.agent == AgentConfig(
        type="orchestrator",
        options={
            "orchestrator_model": "qwen3:4b-instruct",
            "cloud_model": "gpt-oss:20b-cloud",
            "domain_agents": {
                "home_assistant": {"model": "qwen3:8b"},
                "time": {},
            },
        },
    )


def test_load_config_with_orchestrator_local_model(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: orchestrator
  orchestrator_model: qwen3:4b-instruct
  cloud_model: gpt-oss:20b-cloud
  clarification_model: gpt-oss:20b-cloud
  local_model: qwen3:4b-instruct
  fallback_backoff_seconds: 120
  domain_agents:
    home_assistant:
      fallback_model: qwen3:8b
""",
    )

    assert load_config_from_yaml(config_path).agent == AgentConfig(
        type="orchestrator",
        options={
            "orchestrator_model": "qwen3:4b-instruct",
            "cloud_model": "gpt-oss:20b-cloud",
            "clarification_model": "gpt-oss:20b-cloud",
            "local_model": "qwen3:4b-instruct",
            "fallback_backoff_seconds": 120.0,
            "domain_agents": {
                "home_assistant": {"fallback_model": "qwen3:8b"},
            },
        },
    )


def test_load_config_with_voice_defaults_and_multiple_microphones(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
microphones:
  - type: box3_esphome
    name: box3-office
    address: piotr-box3-01-cbfaA8.local
    api_key: abc
    area: office
    open_mic: true
  - type: box3_esphome
    name: box3-roaming
    address: 192.168.1.42
    api_key: def
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.stt == SttConfig()
    assert config.tts == TtsConfig()
    assert config.microphones == (
        MicrophoneConfig(
            type="box3_esphome",
            name="box3-office",
            area="office",
            open_mic=True,
            initial_silence_seconds=3.0,
            end_silence_seconds=0.9,
            follow_up_timeout_seconds=15.0,
            options={"address": "piotr-box3-01-cbfaA8.local", "api_key": "abc"},
        ),
        MicrophoneConfig(
            type="box3_esphome",
            name="box3-roaming",
            area=None,
            initial_silence_seconds=3.0,
            end_silence_seconds=0.9,
            follow_up_timeout_seconds=15.0,
            options={"address": "192.168.1.42", "api_key": "def"},
        ),
    )


def test_load_config_with_explicit_microphone_defaults_and_device_timing_overrides(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
microphones:
  initial_silence_seconds: 4
  end_silence_seconds: 1.2
  open_mic_wake_phrase: Alfredzie
  speech_peak_threshold: 600
  post_speech_ignore_seconds: 1.5
  follow_up_timeout_seconds: 12.5
  devices:
    - type: box3_esphome
      name: box3-office
      address: box.local
      api_key: abc
      initial_silence_seconds: 5
      end_silence_seconds: 0.8
      speech_peak_threshold: 900
      post_speech_ignore_seconds: 2
      follow_up_timeout_seconds: 3
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.microphone_defaults == MicrophoneDefaultsConfig(
        open_mic_wake_phrase="Alfredzie",
        initial_silence_seconds=4.0,
        end_silence_seconds=1.2,
        speech_peak_threshold=600,
        post_speech_ignore_seconds=1.5,
        follow_up_timeout_seconds=12.5,
    )
    assert config.microphones[0].initial_silence_seconds == 5.0
    assert config.microphones[0].end_silence_seconds == 0.8
    assert config.microphones[0].speech_peak_threshold == 900
    assert config.microphones[0].post_speech_ignore_seconds == 2.0
    assert config.microphones[0].follow_up_timeout_seconds == 3.0


def test_load_config_uses_legacy_conversation_timeout_as_microphone_default(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
conversation:
  follow_up_timeout_seconds: 12.5
microphones:
  - type: box3_esphome
    name: box3-office
    address: box.local
    api_key: abc
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.conversation == ConversationConfig(follow_up_timeout_seconds=12.5)
    assert config.microphone_defaults.follow_up_timeout_seconds == 12.5
    assert config.microphones[0].follow_up_timeout_seconds == 12.5


def test_load_config_with_explicit_voice_values(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
stt:
  model: small
  language: pl
  device: cpu
  compute_type: int8
  local_files_only: false
  beam_size: 3
  capture_seconds: 4.5
  partial_interval_seconds: 0.25
  partial_window_seconds: 2.5
  partial_beam_size: 1
  partial_max_backlog_seconds: 0.8
tts:
  voice: pl_PL-darkman-medium
  volume: 0.7
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.stt == SttConfig(
        model="small",
        language="pl",
        device="cpu",
        compute_type="int8",
        local_files_only=False,
        beam_size=3,
        capture_seconds=4.5,
        partial_interval_seconds=0.25,
        partial_window_seconds=2.5,
        partial_beam_size=1,
        partial_max_backlog_seconds=0.8,
    )
    assert config.tts == TtsConfig(voice="pl_PL-darkman-medium", volume=0.7)


def test_load_config_with_processing_updates(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
processing_updates:
  interval_seconds: 2.5
  spoken_cues:
    - Hmm...
    - Myslę....
    - momencik...
""",
    )

    config = load_config_from_yaml(config_path)

    assert config.processing_updates == ProcessingUpdatesConfig(
        interval_seconds=2.5,
        spoken_cues=("Hmm...", "Myslę....", "momencik..."),
    )


def test_load_config_rejects_legacy_microphone_location(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  port: 2137
agent:
  type: echo
microphones:
  - type: box3_esphome
    name: box3-office
    address: box.local
    api_key: abc
    location: office
""",
    )

    with pytest.raises(ValueError, match="microphones\\[0\\]\\.location has been renamed"):
        load_config_from_yaml(config_path)


def test_load_config_requires_websocket_port(tmp_path: Path) -> None:
    config_path = write_config(
        tmp_path,
        """
websocket:
  path: /chat
agent:
  type: echo
""",
    )

    with pytest.raises(ValueError, match="websocket.port is required"):
        load_config_from_yaml(config_path)


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("", "config must contain a websocket mapping"),
        ("[]", "config must be a YAML mapping"),
        ("websocket: []", "config must contain a websocket mapping"),
        ("websocket:\n  port: 2137", "config must contain an agent mapping"),
        ("websocket:\n  port: 2137\nagent: []", "config must contain an agent mapping"),
        ("websocket:\n  port: 2137\nagent: {}", "agent.type must be a non-empty string"),
        (
            "websocket:\n  port: 2137\nagent:\n  type: polite_reply",
            "agent.model must be a non-empty string for polite_reply",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: polite_reply\n  model: ''",
            "agent.model must be a non-empty string for polite_reply",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: polite_reply\n  model: 123",
            "agent.model must be a non-empty string for polite_reply",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: orchestrator\n  cloud_model: main",
            "agent.orchestrator_model must be a non-empty string for orchestrator",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: orchestrator\n  orchestrator_model: small\n  model: main",
            "agent.model has been renamed to agent.cloud_model for orchestrator",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: orchestrator\n  orchestrator_model: small\n  cloud_model: cloud\n  fallback_model: local",
            "agent.fallback_model has been renamed to agent.local_model for orchestrator",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: orchestrator\n  orchestrator_model: small",
            "agent.cloud_model must be a non-empty string for orchestrator",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: orchestrator\n  orchestrator_model: small\n  cloud_model: main\n  clarification_model: ''",
            "agent.clarification_model must be a non-empty string when provided",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: orchestrator\n  orchestrator_model: small\n  cloud_model: main\n  local_model: ''",
            "agent.local_model must be a non-empty string when provided",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: orchestrator\n  orchestrator_model: small\n  cloud_model: main\n  fallback_backoff_seconds: 0",
            "agent.fallback_backoff_seconds must be a positive number",
        ),
        ("websocket:\n  port: nope\nagent:\n  type: echo", "websocket.port must be an integer"),
        ("websocket:\n  port: 0\nagent:\n  type: echo", "websocket.port must be between 1 and 65535"),
        (
            "websocket:\n  port: 2137\n  path: chat\nagent:\n  type: echo",
            "websocket.path must be a string starting with '/'",
        ),
        (
            "websocket:\n  port: 2137\n  follow_up_timeout_seconds: 0\nagent:\n  type: echo",
            "websocket.follow_up_timeout_seconds must be a positive number",
        ),
        ("log_level: noisy\nwebsocket:\n  port: 2137\nagent:\n  type: echo", "log_level must be one of"),
        ("log_level: 1\nwebsocket:\n  port: 2137\nagent:\n  type: echo", "log_level must be a string"),
        (
            "default_user: Maciek\nwebsocket:\n  port: 2137\nagent:\n  type: echo",
            "default_user has been removed",
        ),
        ("users: []\nwebsocket:\n  port: 2137\nagent:\n  type: echo", "users must be a mapping"),
        ("users:\n  Maciek: []\nwebsocket:\n  port: 2137\nagent:\n  type: echo", "users.Maciek must be a mapping"),
        (
            "users:\n  Maciek:\n    home_assistant_user_id: ''\nwebsocket:\n  port: 2137\nagent:\n  type: echo",
            "users.Maciek.home_assistant_user_id must be a non-empty string when provided",
        ),
        (
            "users:\n  Maciek:\n    home_assistant_user_id: 123\nwebsocket:\n  port: 2137\nagent:\n  type: echo",
            "users.Maciek.home_assistant_user_id must be a non-empty string when provided",
        ),
        (
            "home_assistant_user_settings: {}\nwebsocket:\n  port: 2137\nagent:\n  type: echo",
            "home_assistant_user_settings has moved to users.<user>.home_assistant_user_id",
        ),
        ("websocket:\n  port: 2137\nagent:\n  type: echo\nstt: []", "stt must be a mapping"),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nstt:\n  device: nope",
            "stt.device must be one of",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nstt:\n  compute_type: ''",
            "stt.compute_type must be a non-empty string",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nstt:\n  local_files_only: nope",
            "stt.local_files_only must be a boolean",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nstt:\n  partial_interval_seconds: 0",
            "stt.partial_interval_seconds must be a positive number",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nstt:\n  partial_window_seconds: no",
            "stt.partial_window_seconds must be a positive number",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nstt:\n  partial_beam_size: 0",
            "stt.partial_beam_size must be a positive integer",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nstt:\n  partial_max_backlog_seconds: -1",
            "stt.partial_max_backlog_seconds must be a positive number",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\ntts:\n  volume: 2",
            "tts.volume must be between",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\ndata_dir: []",
            "data_dir must be a non-empty string",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  devices: nope",
            "microphones.devices must be a list",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nprocessing_updates: nope",
            "processing_updates must be a mapping",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nprocessing_updates:\n  interval_seconds: 0",
            "processing_updates.interval_seconds must be a positive number",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nprocessing_updates:\n  spoken_cues: []",
            "processing_updates.spoken_cues must contain at least one cue",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nprocessing_updates:\n  spoken_cues:\n    - ''",
            r"processing_updates.spoken_cues\[0\] must be a non-empty string",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  initial_silence_seconds: 0",
            "microphones.initial_silence_seconds must be a positive number",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  open_mic_wake_phrase: ''",
            "microphones.open_mic_wake_phrase must be a non-empty string",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  devices:\n    - type: box3_esphome\n      name: box\n      address: host\n      api_key: key\n      end_silence_seconds: nope",
            r"microphones\[0\].end_silence_seconds must be a positive number",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  speech_peak_threshold: 0",
            r"microphones.speech_peak_threshold must be an integer between 1 and 32767",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  devices:\n    - type: box3_esphome\n      name: box\n      address: host\n      api_key: key\n      speech_peak_threshold: loud",
            r"microphones\[0\].speech_peak_threshold must be an integer between 1 and 32767",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  post_speech_ignore_seconds: -1",
            r"microphones.post_speech_ignore_seconds must be a non-negative number",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  devices:\n    - type: box3_esphome\n      name: box\n      address: host\n      api_key: key\n      post_speech_ignore_seconds: nope",
            r"microphones\[0\].post_speech_ignore_seconds must be a non-negative number",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  - name: box",
            r"microphones\[0\].type must be a non-empty string",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  - type: box3_esphome",
            r"microphones\[0\].name must be a non-empty string",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  - type: box3_esphome\n    name: box",
            r"microphones\[0\].address must be a non-empty string for box3_esphome",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  - type: box3_esphome\n    name: box\n    open_mic: yes please",
            r"microphones\[0\].open_mic must be a boolean",
        ),
        (
            "websocket:\n  port: 2137\nagent:\n  type: echo\nmicrophones:\n  - type: box3_esphome\n    name: box\n    address: host",
            r"microphones\[0\].api_key must be a non-empty string for box3_esphome",
        ),
    ],
)
def test_load_config_rejects_invalid_values(tmp_path: Path, content: str, error: str) -> None:
    config_path = write_config(tmp_path, content)

    with pytest.raises(ValueError, match=error):
        load_config_from_yaml(config_path)
