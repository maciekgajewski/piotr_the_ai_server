import pytest

from ai_server.microphones.messages import AudioChunk, AudioProgress, Close, CueFinished, CueType, ListeningMode
from ai_server.microphones.messages import ListeningStarted
from ai_server.microphones.messages import ListeningStopped, PlaybackBegin, PlaybackEnd, PlaybackFinished, PlayCue
from ai_server.microphones.messages import MicrophoneUnavailable, PlaybackChunk, ResetWakeCandidate, SetVisualState
from ai_server.microphones.messages import SpeechEnded
from ai_server.microphones.messages import SpeechStarted, StartListening, StopListening
from ai_server.microphones.messages import VisualState
from ai_server.microphones.protocol import DriverState, MicrophoneProtocolState


def test_wake_word_generation_disarms_after_one_segment() -> None:
    protocol = MicrophoneProtocolState()

    protocol.command(StartListening("listen-1", ListeningMode.WAKE_WORD))
    protocol.event(ListeningStarted("listen-1", ListeningMode.WAKE_WORD))
    protocol.event(SpeechStarted("listen-1", "utterance-1", 16000, 2, 1, "Ryszardzie"))
    protocol.event(AudioChunk("listen-1", "utterance-1", b"audio"))
    protocol.event(SpeechEnded("listen-1", "utterance-1", "completed"))

    assert protocol.snapshot.state is DriverState.DISARMED
    assert protocol.snapshot.listen_id is None


def test_open_mic_generation_accepts_multiple_distinct_segments() -> None:
    protocol = MicrophoneProtocolState()
    protocol.command(StartListening("listen-1", ListeningMode.OPEN_MIC))
    protocol.event(ListeningStarted("listen-1", ListeningMode.OPEN_MIC))

    for utterance_id in ("utterance-1", "utterance-2"):
        protocol.event(SpeechStarted("listen-1", utterance_id, 16000, 2, 1))
        protocol.event(SpeechEnded("listen-1", utterance_id, "completed"))

    assert protocol.snapshot.state is DriverState.LISTENING
    assert protocol.snapshot.listen_id == "listen-1"


def test_stop_requires_matching_listen_id() -> None:
    protocol = MicrophoneProtocolState()
    protocol.command(StartListening("listen-1", ListeningMode.OPEN_MIC))
    protocol.event(ListeningStarted("listen-1", ListeningMode.OPEN_MIC))

    with pytest.raises(AssertionError, match="mismatched listen_id"):
        protocol.command(StopListening("stale-listen", "cancelled"))


def test_identifier_cannot_be_reused() -> None:
    protocol = MicrophoneProtocolState()
    protocol.command(StartListening("same-id", ListeningMode.WAKE_WORD))
    protocol.event(ListeningStarted("same-id", ListeningMode.WAKE_WORD))
    protocol.command(StopListening("same-id", "cancelled"))
    protocol.event(ListeningStopped("same-id", "cancelled"))

    with pytest.raises(AssertionError, match="identifier reused"):
        protocol.command(PlayCue("same-id", CueType.UTTERANCE_ACCEPTED))


def test_playback_finishes_only_after_end() -> None:
    protocol = MicrophoneProtocolState()
    protocol.command(PlaybackBegin("playback-1", 22050, 2, 1))

    with pytest.raises(AssertionError, match="before PlaybackEnd"):
        protocol.event(PlaybackFinished("playback-1"))

    protocol.command(PlaybackEnd("playback-1"))
    protocol.event(PlaybackFinished("playback-1"))
    assert protocol.snapshot.state is DriverState.DISARMED


def test_cue_is_half_duplex() -> None:
    protocol = MicrophoneProtocolState()
    protocol.command(StartListening("listen-1", ListeningMode.OPEN_MIC))

    with pytest.raises(AssertionError, match="PlayCue invalid in arming"):
        protocol.command(PlayCue("cue-1", CueType.UTTERANCE_ACCEPTED))


def test_cue_completion_requires_matching_id() -> None:
    protocol = MicrophoneProtocolState()
    protocol.command(PlayCue("cue-1", CueType.FOLLOW_UP_READY))

    with pytest.raises(AssertionError, match="mismatched cue_id"):
        protocol.event(CueFinished("stale-cue"))


def test_error_visual_cannot_be_commanded() -> None:
    with pytest.raises(ValueError, match="firmware-owned"):
        SetVisualState(VisualState.ERROR)


def test_visual_command_does_not_change_audio_state() -> None:
    protocol = MicrophoneProtocolState()
    protocol.command(StartListening("listen-1", ListeningMode.OPEN_MIC))
    protocol.command(SetVisualState(VisualState.IDLE))
    assert protocol.snapshot.state is DriverState.ARMING


def test_nested_speech_segment_is_rejected() -> None:
    protocol = _listening_protocol()
    protocol.event(SpeechStarted("listen-1", "utterance-1", 16000, 2, 1))
    with pytest.raises(AssertionError, match="SpeechStarted invalid in capturing"):
        protocol.event(SpeechStarted("listen-1", "utterance-2", 16000, 2, 1))


def test_audio_outside_segment_is_rejected() -> None:
    protocol = _listening_protocol()
    with pytest.raises(AssertionError, match="AudioChunk invalid in listening"):
        protocol.event(AudioChunk("listen-1", "utterance-1", b"audio"))


def test_mismatched_utterance_is_rejected() -> None:
    protocol = _listening_protocol()
    protocol.event(SpeechStarted("listen-1", "utterance-1", 16000, 2, 1))
    with pytest.raises(AssertionError, match="mismatched utterance_id"):
        protocol.event(AudioChunk("listen-1", "stale-utterance", b"audio"))


def test_playback_is_rejected_while_listening() -> None:
    protocol = _listening_protocol()
    with pytest.raises(AssertionError, match="PlaybackBegin invalid in listening"):
        protocol.command(PlaybackBegin("playback-1", 22050, 2, 1))


def test_implicit_rearm_is_rejected() -> None:
    protocol = _listening_protocol()
    with pytest.raises(AssertionError, match="StartListening invalid in listening"):
        protocol.command(StartListening("listen-2", ListeningMode.OPEN_MIC))


def test_unavailability_requires_active_operation_correlation() -> None:
    protocol = _listening_protocol()
    with pytest.raises(AssertionError, match="must identify"):
        protocol.event(MicrophoneUnavailable("offline"))


def test_close_is_terminal() -> None:
    protocol = _listening_protocol()
    protocol.command(Close())
    assert protocol.snapshot.state is DriverState.CLOSED
    with pytest.raises(AssertionError, match="in CLOSED"):
        protocol.command(SetVisualState(VisualState.IDLE))


def test_playback_rejects_chunk_after_end() -> None:
    protocol = MicrophoneProtocolState()
    protocol.command(PlaybackBegin("playback-1", 22050, 2, 1))
    protocol.command(PlaybackEnd("playback-1"))
    with pytest.raises(AssertionError, match="after PlaybackEnd"):
        protocol.command(PlaybackChunk("playback-1", b"late"))


@pytest.mark.parametrize("state", list(DriverState)[:-1])
def test_visual_commands_are_idempotent_in_every_connected_state(state: DriverState) -> None:
    """MP-VISUAL-002: visuals are independent of every non-closed audio state."""
    protocol = _protocol_in_state(state)
    before = protocol.snapshot

    protocol.command(SetVisualState(VisualState.PROCESSING))
    protocol.command(SetVisualState(VisualState.PROCESSING))

    assert protocol.snapshot == before


@pytest.mark.parametrize(
    ("event", "error"),
    [
        (ListeningStarted("stale-listen", ListeningMode.OPEN_MIC), "mismatched listen_id"),
        (ListeningStarted("listen-1", ListeningMode.FOLLOW_UP), "mismatched mode"),
    ],
)
def test_arming_rejects_stale_or_wrong_listening_started(event, error: str) -> None:
    """MP-ID-003/MP-STATE-001: delayed start callbacks cannot arm a generation."""
    protocol = MicrophoneProtocolState()
    protocol.command(StartListening("listen-1", ListeningMode.OPEN_MIC))

    with pytest.raises(AssertionError, match=error):
        protocol.event(event)


@pytest.mark.parametrize(
    "event",
    [
        AudioChunk("stale-listen", "utterance-1", b"audio"),
        AudioProgress("listen-1", "stale-utterance", 1, 5),
        SpeechEnded("listen-1", "stale-utterance", "completed"),
    ],
)
def test_capturing_rejects_every_stale_segment_event(event) -> None:
    """MP-ID-003: no stale segment event can complete or mutate active capture."""
    protocol = _listening_protocol()
    protocol.event(SpeechStarted("listen-1", "utterance-1", 16000, 2, 1))
    before = protocol.snapshot

    with pytest.raises(AssertionError, match="mismatched"):
        protocol.event(event)

    assert protocol.snapshot == before


@pytest.mark.parametrize(
    ("event", "error"),
    [
        (CueFinished("stale-cue"), "mismatched cue_id"),
        (PlaybackFinished("stale-playback"), "mismatched playback_id"),
    ],
)
def test_output_completion_rejects_stale_identifiers(event, error: str) -> None:
    """MP-ID-003: delayed output completion cannot drain a newer operation."""
    protocol = MicrophoneProtocolState()
    if isinstance(event, CueFinished):
        protocol.command(PlayCue("cue-1", CueType.FOLLOW_UP_READY))
    else:
        protocol.command(PlaybackBegin("playback-1", 22050, 2, 1))
        protocol.command(PlaybackEnd("playback-1"))
    before = protocol.snapshot

    with pytest.raises(AssertionError, match=error):
        protocol.event(event)

    assert protocol.snapshot == before


def test_reset_candidate_rejects_unknown_utterance_without_mutating_generation() -> None:
    """MP-OPENMIC-002/MP-ID-003: reset is correlated to a completed active segment."""
    protocol = _listening_protocol()
    before = protocol.snapshot

    with pytest.raises(AssertionError, match="unknown utterance_id"):
        protocol.command(ResetWakeCandidate("listen-1", "stale-utterance"))

    assert protocol.snapshot == before


def _listening_protocol() -> MicrophoneProtocolState:
    protocol = MicrophoneProtocolState()
    protocol.command(StartListening("listen-1", ListeningMode.OPEN_MIC))
    protocol.event(ListeningStarted("listen-1", ListeningMode.OPEN_MIC))
    return protocol


def _protocol_in_state(state: DriverState) -> MicrophoneProtocolState:
    protocol = MicrophoneProtocolState()
    if state is DriverState.DISARMED:
        return protocol
    if state is DriverState.ARMING:
        protocol.command(StartListening("listen-1", ListeningMode.OPEN_MIC))
        return protocol
    if state in (DriverState.LISTENING, DriverState.CAPTURING, DriverState.STOPPING):
        protocol.command(StartListening("listen-1", ListeningMode.OPEN_MIC))
        protocol.event(ListeningStarted("listen-1", ListeningMode.OPEN_MIC))
        if state is DriverState.CAPTURING:
            protocol.event(SpeechStarted("listen-1", "utterance-1", 16000, 2, 1))
        elif state is DriverState.STOPPING:
            protocol.command(StopListening("listen-1", "test"))
        return protocol
    if state is DriverState.PLAYING_CUE:
        protocol.command(PlayCue("cue-1", CueType.FOLLOW_UP_READY))
        return protocol
    if state is DriverState.PLAYING_AUDIO:
        protocol.command(PlaybackBegin("playback-1", 22050, 2, 1))
        return protocol
    raise AssertionError(f"unsupported test state: {state}")
