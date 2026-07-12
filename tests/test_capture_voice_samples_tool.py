from __future__ import annotations

import asyncio
import array
from pathlib import Path
import sys
import wave

from ai_server.config import MicrophoneConfig
from ai_server.microphones.messages import ListeningMode, ListeningStarted, SpeechEnded, SpeechStarted, StartListening


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "lib"))

import capture_voice_samples


def pcm16_chunk(value: int, samples: int) -> bytes:
    data = array.array("h", [value] * samples)
    return data.tobytes()


def write_wav(path: Path, seconds: float, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = pcm16_chunk(1000, int(rate * seconds))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(frames)


def test_scan_output_dir_counts_existing_wavs_and_first_free_name(tmp_path: Path) -> None:
    write_wav(tmp_path / "0001.wav", 5.0)
    write_wav(tmp_path / "0003.wav", 2.5)

    scan = capture_voice_samples.scan_output_dir(tmp_path)

    assert scan.file_count == 2
    assert scan.existing_seconds == 7.5
    assert scan.next_index == 2


def test_assembler_removes_silent_sections_and_writes_five_second_samples(tmp_path: Path) -> None:
    assembler = capture_voice_samples.VoiceSampleAssembler(
        output_dir=tmp_path,
        start_index=1,
        existing_seconds=0.0,
        threshold=500,
        audio_format=capture_voice_samples.AudioFormat(rate=10, width=2, channels=1),
    )

    silence = pcm16_chunk(0, 10)
    voice = pcm16_chunk(1000, 50)
    saved = assembler.add_chunk(silence + voice)

    assert saved == [tmp_path / "0001.wav"]
    assert assembler.total_usable_seconds == 5.0
    assert assembler.pending_seconds == 0.0
    with wave.open(str(tmp_path / "0001.wav"), "rb") as reader:
        assert reader.getframerate() == 10
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getnframes() == 50


def test_load_example_phrases_reads_messages_and_dsa_queries(tmp_path: Path) -> None:
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(
        """
group: sample
type: orchestrator
cases:
  - id: turn
    messages:
      - pierwsza fraza
      - druga fraza
  - id: dsa
    task:
      command:
        query: trzecia fraza
""",
        encoding="utf-8",
    )

    assert capture_voice_samples.load_example_phrases(tmp_path) == [
        "pierwsza fraza",
        "druga fraza",
        "trzecia fraza",
    ]


def test_continuous_capture_config_disables_silence_timeouts() -> None:
    config = MicrophoneConfig(
        type="box3_esphome",
        name="office",
        area="office",
        options={"address": "box.local", "api_key": "key"},
        initial_silence_seconds=3.0,
        end_silence_seconds=0.9,
        post_speech_ignore_seconds=1.0,
    )

    continuous = capture_voice_samples.continuous_capture_config(config)

    assert continuous.initial_silence_seconds == capture_voice_samples.CONTINUOUS_CAPTURE_TIMEOUT_SECONDS
    assert continuous.end_silence_seconds == capture_voice_samples.CONTINUOUS_CAPTURE_TIMEOUT_SECONDS
    assert continuous.post_speech_ignore_seconds == 0.0
    assert continuous.speech_peak_threshold == capture_voice_samples.CONTINUOUS_CAPTURE_DRIVER_THRESHOLD


def test_capture_samples_does_not_rearm_after_audio_end(tmp_path: Path) -> None:
    class FakeMicrophone:
        def __init__(self) -> None:
            self.sent = []
            self.events = []

        async def send_output_event(self, event) -> None:
            self.sent.append(event)
            if isinstance(event, StartListening):
                self.events.extend(
                    [
                        ListeningStarted(event.listen_id, event.mode),
                        SpeechStarted(event.listen_id, "utterance-1", 16000, 2, 1),
                        SpeechEnded(event.listen_id, "utterance-1", "completed"),
                    ]
                )

        async def wait_for_event(self):
            return self.events.pop(0)

    async def run() -> FakeMicrophone:
        microphone = FakeMicrophone()
        assembler = capture_voice_samples.VoiceSampleAssembler(
            output_dir=tmp_path,
            start_index=1,
            existing_seconds=0.0,
            threshold=500,
            audio_format=capture_voice_samples.AudioFormat(),
        )
        await capture_voice_samples.capture_samples(microphone, assembler)
        return microphone

    microphone = asyncio.run(run())

    assert len(microphone.sent) == 1
    assert isinstance(microphone.sent[0], StartListening)
    assert microphone.sent[0].mode is ListeningMode.FOLLOW_UP
