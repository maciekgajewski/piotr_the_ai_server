from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import struct
import sys

import pytest

from ai_server.config import AgentConfig, Config, MicrophoneConfig, WebsocketConfig
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioStart, ConversationTimeoutCue, MessageEndCue
from ai_server.microphones.messages import StartFollowUpListening
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "lib" / "mic_protocol_test.py"
SPEC = importlib.util.spec_from_file_location("mic_protocol_test", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
mic_protocol_test = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mic_protocol_test
SPEC.loader.exec_module(mic_protocol_test)


def test_select_microphone_config_uses_only_configured_microphone() -> None:
    microphone = MicrophoneConfig(
        type="box3_esphome",
        name="box3-office",
        area="office",
        options={"address": "box.local", "api_key": "secret"},
    )
    config = _config(microphones=(microphone,))

    assert mic_protocol_test.select_microphone_config(config, None) is microphone


def test_select_microphone_config_requires_name_when_multiple() -> None:
    config = _config(
        microphones=(
            MicrophoneConfig(type="test", name="one", area=None, options={}),
            MicrophoneConfig(type="test", name="two", area=None, options={}),
        )
    )

    with pytest.raises(ValueError, match="use --mic"):
        mic_protocol_test.select_microphone_config(config, None)


def test_select_microphone_config_rejects_unknown_name() -> None:
    config = _config(microphones=(MicrophoneConfig(type="test", name="one", area=None, options={}),))

    with pytest.raises(ValueError, match="unknown microphone"):
        mic_protocol_test.select_microphone_config(config, "missing")


def test_replay_utterance_sends_audio_events() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        utterance = mic_protocol_test.RecordedUtterance(
            label="sample",
            start=AudioStart(wake_word="wake"),
            chunks=(b"one", b"two"),
            rate=16000,
            width=2,
            channels=1,
        )

        await mic_protocol_test.replay_utterance(
            microphone,
            utterance,
            volume=0.5,
            normalize_replay=False,
            normalize_target_peak=0.85,
        )

        assert microphone.output_events == [
            AudioStart(rate=16000, width=2, channels=1, volume=0.5),
            AudioChunk(data=b"one"),
            AudioChunk(data=b"two"),
            AudioEnd(),
        ]

    asyncio.run(run())


def test_replay_utterance_normalizes_pcm16_audio() -> None:
    async def run() -> None:
        microphone = FakeMicrophone()
        utterance = mic_protocol_test.RecordedUtterance(
            label="sample",
            start=AudioStart(wake_word="wake"),
            chunks=(_pcm16_chunk(1000, -1000),),
            rate=16000,
            width=2,
            channels=1,
        )

        await mic_protocol_test.replay_utterance(
            microphone,
            utterance,
            volume=1.0,
            normalize_replay=True,
            normalize_target_peak=0.5,
        )

        assert microphone.output_events == [
            AudioStart(rate=16000, width=2, channels=1, volume=1.0),
            AudioChunk(data=_pcm16_chunk(16383, -16383)),
            AudioEnd(),
        ]

    asyncio.run(run())


def test_capture_cue_and_replay_sends_message_end_cue_before_operator_questions() -> None:
    async def run() -> None:
        microphone = FakeMicrophone(
            input_events=[
                AudioStart(wake_word="follow_up", rate=16000, width=2, channels=1),
                AudioChunk(data=b"phrase"),
                AudioEnd(),
            ]
        )
        operator = FakeOperator(microphone)

        utterance = await mic_protocol_test.capture_cue_and_replay_step(
            microphone=microphone,
            operator=operator,
            label="follow-up",
            start_timeout_seconds=1,
            stream_event_timeout_seconds=1,
            volume=1.0,
            wake_word_expected=False,
            start_cue_question="Was the follow-up chime audible?",
        )

        assert utterance is not None
        assert isinstance(microphone.output_events[0], MessageEndCue)
        assert operator.output_counts_at_questions == [1, 1, 4, 4]

    asyncio.run(run())


def test_timeout_step_checks_follow_up_chime_while_waiting_for_audio_start() -> None:
    async def run() -> None:
        microphone = FakeMicrophone(
            input_events=[
                AudioStart(wake_word="follow_up", rate=16000, width=2, channels=1),
                AudioEnd(),
            ]
        )
        operator = FakeOperator(microphone)

        assert await mic_protocol_test.run_timeout_step(
            microphone=microphone,
            operator=operator,
            timeout_seconds=1,
            stream_event_timeout_seconds=1,
        )
        assert isinstance(microphone.output_events[0], StartFollowUpListening)
        assert isinstance(microphone.output_events[1], ConversationTimeoutCue)
        assert operator.questions == [
            "Was the timeout-step follow-up chime audible?",
            "Was the timeout chime audible?",
        ]
        assert operator.output_counts_at_questions == [2, 2]

    asyncio.run(run())


def test_parse_args_rejects_invalid_volume() -> None:
    with pytest.raises(SystemExit, match="--volume must be between 0.0 and 1.0"):
        mic_protocol_test.parse_args(["--volume", "2"])


def test_parse_args_rejects_invalid_normalize_peak() -> None:
    with pytest.raises(SystemExit, match="--normalize-replay-peak must be between 0.0 and 1.0"):
        mic_protocol_test.parse_args(["--normalize-replay-peak", "0"])


def _pcm16_chunk(*samples: int) -> bytes:
    return struct.pack("<" + "h" * len(samples), *samples)


def _config(microphones: tuple[MicrophoneConfig, ...]) -> Config:
    return Config(
        agent=AgentConfig(type="interrogator", options={}),
        websocket=WebsocketConfig(port=8765),
        microphones=microphones,
    )


class FakeOperator:
    def __init__(self, microphone: "FakeMicrophone") -> None:
        self.microphone = microphone
        self.questions = []
        self.output_counts_at_questions = []

    async def ask_yes_no(self, question: str) -> bool:
        self.questions.append(question)
        self.output_counts_at_questions.append(len(self.microphone.output_events))
        return True

    async def pause(self, _message: str) -> None:
        return None


class FakeMicrophone:
    context = MicrophoneContext(type="test", name="fake")
    playback_target = PlaybackTarget(type="test", name="fake", address="fake", api_key="secret")

    def __init__(self, input_events=None) -> None:
        self.input_events = asyncio.Queue()
        for event in input_events or []:
            self.input_events.put_nowait(event)
        self.output_events = []

    async def wait_for_event(self):
        return await self.input_events.get()

    async def send_output_event(self, event) -> None:
        self.output_events.append(event)

    async def close(self) -> None:
        pass
