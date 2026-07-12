from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias


class ListeningMode(Enum):
    WAKE_WORD = "wake_word"
    OPEN_MIC = "open_mic"
    FOLLOW_UP = "follow_up"


class VisualState(Enum):
    ERROR = "error"
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"


class CueType(Enum):
    UTTERANCE_ACCEPTED = "utterance_accepted"
    FOLLOW_UP_READY = "follow_up_ready"
    FOLLOW_UP_TIMEOUT = "follow_up_timeout"


@dataclass(frozen=True)
class StartListening:
    listen_id: str
    mode: ListeningMode


@dataclass(frozen=True)
class StopListening:
    listen_id: str
    reason: str


@dataclass(frozen=True)
class SetVisualState:
    state: VisualState

    def __post_init__(self) -> None:
        if self.state is VisualState.ERROR:
            raise ValueError("ERROR is firmware-owned and cannot be commanded")


@dataclass(frozen=True)
class ResetWakeCandidate:
    listen_id: str
    utterance_id: str


@dataclass(frozen=True)
class PlayCue:
    cue_id: str
    cue_type: CueType


@dataclass(frozen=True)
class PlaybackBegin:
    playback_id: str
    rate: int
    width: int
    channels: int
    volume: float | None = None


@dataclass(frozen=True)
class PlaybackChunk:
    playback_id: str
    data: bytes


@dataclass(frozen=True)
class PlaybackEnd:
    playback_id: str


@dataclass(frozen=True)
class Close:
    pass


@dataclass(frozen=True)
class ListeningStarted:
    listen_id: str
    mode: ListeningMode


@dataclass(frozen=True)
class ListeningStopped:
    listen_id: str
    reason: str


@dataclass(frozen=True)
class SpeechStarted:
    listen_id: str
    utterance_id: str
    rate: int
    width: int
    channels: int
    wake_word: str | None = None


@dataclass(frozen=True)
class AudioChunk:
    listen_id: str
    utterance_id: str
    data: bytes


@dataclass(frozen=True)
class AudioProgress:
    listen_id: str
    utterance_id: str
    chunks: int
    bytes: int


@dataclass(frozen=True)
class SpeechEnded:
    listen_id: str
    utterance_id: str
    reason: str


@dataclass(frozen=True)
class CueFinished:
    cue_id: str


@dataclass(frozen=True)
class PlaybackFinished:
    playback_id: str


@dataclass(frozen=True)
class MicrophoneUnavailable:
    reason: str
    listen_id: str | None = None
    utterance_id: str | None = None
    cue_id: str | None = None
    playback_id: str | None = None


@dataclass(frozen=True)
class DriverClosed:
    pass


@dataclass(frozen=True)
class SynthesizedAudioStart:
    rate: int
    width: int
    channels: int
    volume: float | None = None


@dataclass(frozen=True)
class SynthesizedAudioChunk:
    data: bytes


@dataclass(frozen=True)
class SynthesizedAudioEnd:
    pass


MicrophoneCommand: TypeAlias = (
    StartListening
    | StopListening
    | SetVisualState
    | ResetWakeCandidate
    | PlayCue
    | PlaybackBegin
    | PlaybackChunk
    | PlaybackEnd
    | Close
)
MicrophoneEvent: TypeAlias = (
    ListeningStarted
    | ListeningStopped
    | SpeechStarted
    | AudioChunk
    | AudioProgress
    | SpeechEnded
    | CueFinished
    | PlaybackFinished
    | MicrophoneUnavailable
    | DriverClosed
)
SynthesizedAudioEvent: TypeAlias = SynthesizedAudioStart | SynthesizedAudioChunk | SynthesizedAudioEnd
