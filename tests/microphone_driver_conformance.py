from __future__ import annotations

from typing import Protocol

from ai_server.microphones.interfaces import Microphone
from ai_server.microphones.messages import AudioChunk, CueFinished, CueType, ListeningMode, ListeningStarted
from ai_server.microphones.messages import ListeningStopped, PlaybackBegin, PlaybackChunk, PlaybackEnd
from ai_server.microphones.messages import PlaybackFinished, PlayCue, SpeechEnded, SpeechStarted, StartListening
from ai_server.microphones.messages import StopListening


class DriverStimulus(Protocol):
    microphone: Microphone

    async def emit_speech(self, data: bytes, reason: str = "completed") -> None:
        raise NotImplementedError


async def assert_listening_and_capture_contract(
    stimulus: DriverStimulus,
    mode: ListeningMode,
) -> None:
    """Reusable black-box checks for MP-ID-001/002 and MP-STATE-001."""
    microphone = stimulus.microphone
    listen_id = f"listen-{mode.value}"
    command = StartListening(listen_id, mode)
    await microphone.send_output_event(command)
    assert await microphone.wait_for_event() == ListeningStarted(listen_id, mode)

    await stimulus.emit_speech(b"audio")
    started = await microphone.wait_for_event()
    assert isinstance(started, SpeechStarted)
    assert started.listen_id == listen_id
    assert started.utterance_id
    assert await microphone.wait_for_event() == AudioChunk(listen_id, started.utterance_id, b"audio")
    assert await microphone.wait_for_event() == SpeechEnded(listen_id, started.utterance_id, "completed")

    if mode is ListeningMode.OPEN_MIC:
        await microphone.send_output_event(StopListening(listen_id, "test_complete"))
        assert await microphone.wait_for_event() == ListeningStopped(listen_id, "test_complete")


async def assert_cue_contract(microphone: Microphone) -> None:
    """Reusable black-box check for correlated cue completion."""
    command = PlayCue("cue-1", CueType.UTTERANCE_ACCEPTED)
    await microphone.send_output_event(command)
    assert await microphone.wait_for_event() == CueFinished("cue-1")


async def assert_playback_contract(microphone: Microphone) -> None:
    """Reusable black-box check for MP-AUDIO-001 drain completion."""
    await microphone.send_output_event(PlaybackBegin("playback-1", 22050, 2, 1))
    await microphone.send_output_event(PlaybackChunk("playback-1", b"audio"))
    await microphone.send_output_event(PlaybackEnd("playback-1"))
    assert await microphone.wait_for_event() == PlaybackFinished("playback-1")
