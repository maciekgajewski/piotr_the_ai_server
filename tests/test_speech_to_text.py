import asyncio
import logging
from pathlib import Path
import time
import wave

from ai_server.config import SttConfig
from ai_server.speech_to_text.faster_whisper import FasterWhisperSpeechToText, _create_faster_whisper_transcriber
from ai_server.speech_to_text.messages import TextEnd, TextFragment, TextPartial
from ai_server.speech_to_text.types import DEFAULT_STT_AUDIO_FORMAT, PcmAudioChunk


class FakeTranscriber:
    def __init__(self, text: str = "włącz tryb ventilacji", delay_seconds: float = 0.0) -> None:
        self.text = text
        self.delay_seconds = delay_seconds
        self.calls = []

    def transcribe(self, wav_path: Path, language: str | None, beam_size: int) -> str:
        with wave.open(str(wav_path), "rb") as reader:
            self.calls.append(
                {
                    "language": language,
                    "beam_size": beam_size,
                    "rate": reader.getframerate(),
                    "width": reader.getsampwidth(),
                    "channels": reader.getnchannels(),
                    "frames": reader.readframes(reader.getnframes()),
                }
            )
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        return self.text


def test_faster_whisper_stt_assembles_pcm_wav_and_returns_text() -> None:
    async def run() -> None:
        transcriber = FakeTranscriber()
        stt = FasterWhisperSpeechToText(
            SttConfig(model="fake", language="pl", device="cpu", compute_type="int8", beam_size=3),
            transcriber_factory=lambda _config: transcriber,
        )

        await stt.start()
        session = await stt.create_session("test-session")
        await session.send_audio(PcmAudioChunk(data=b"one"))
        await session.send_audio(PcmAudioChunk(data=b"two"))
        await session.end_audio()

        assert await session.receive_text() == TextFragment(text="włącz tryb wentylacji")
        assert await session.receive_text() == TextEnd()
        assert transcriber.calls == [
            {
                "language": "pl",
                "beam_size": 3,
                "rate": DEFAULT_STT_AUDIO_FORMAT.rate,
                "width": DEFAULT_STT_AUDIO_FORMAT.width,
                "channels": DEFAULT_STT_AUDIO_FORMAT.channels,
                "frames": b"onetwo",
            }
        ]

    asyncio.run(run())


def test_faster_whisper_stt_logs_metadata_without_transcript_text(caplog) -> None:
    async def run() -> None:
        transcriber = FakeTranscriber(text="tajny tekst")
        stt = FasterWhisperSpeechToText(
            SttConfig(model="fake", language="pl", device="cpu", compute_type="int8"),
            transcriber_factory=lambda _config: transcriber,
        )

        with caplog.at_level(logging.INFO, logger="ai_server.speech_to_text.faster_whisper"):
            await stt.start()
            session = await stt.create_session("private-session")
            await session.send_audio(PcmAudioChunk(data=b"audio"))
            await session.end_audio()
            assert await session.receive_text() == TextFragment(text="tajny tekst")

    asyncio.run(run())

    assert "STT transcription finished" in caplog.text
    assert "chars=11" in caplog.text
    assert "tajny tekst" not in caplog.text


def test_faster_whisper_stt_receive_waits_for_audio_end() -> None:
    async def run() -> None:
        transcriber = FakeTranscriber(text="gotowe")
        stt = FasterWhisperSpeechToText(
            SttConfig(model="fake", language="pl", device="cpu", compute_type="int8"),
            transcriber_factory=lambda _config: transcriber,
        )
        await stt.start()
        session = await stt.create_session("concurrent-session")

        receive_task = asyncio.create_task(session.receive_text())
        await asyncio.sleep(0)
        assert not receive_task.done()

        await session.send_audio(PcmAudioChunk(data=b"audio!"))
        await asyncio.sleep(0)
        assert not receive_task.done()

        await session.end_audio()
        assert await receive_task == TextFragment(text="gotowe")
        assert transcriber.calls[0]["frames"] == b"audio!"

    asyncio.run(run())


def test_faster_whisper_streaming_stt_emits_partial_and_final_text() -> None:
    async def run() -> None:
        transcriber = FakeTranscriber(text="Ryszardzie, włącz światło")
        stt = FasterWhisperSpeechToText(
            SttConfig(
                model="fake",
                language="pl",
                device="cpu",
                compute_type="int8",
                beam_size=4,
                partial_beam_size=1,
                partial_interval_seconds=0.01,
                partial_window_seconds=0.5,
            ),
            transcriber_factory=lambda _config: transcriber,
        )
        await stt.start()
        session = await stt.create_streaming_session("streaming-session")
        await session.send_audio(PcmAudioChunk(data=b"a" * DEFAULT_STT_AUDIO_FORMAT.byte_rate))

        event = await asyncio.wait_for(session.receive_text(), timeout=1)
        assert event == TextPartial(
            text="Ryszardzie, włącz światło",
            audio_start_seconds=0.5,
            audio_end_seconds=1.0,
            duration_seconds=0.5,
        )

        await session.end_audio()
        assert await asyncio.wait_for(session.receive_text(), timeout=1) == TextEnd()
        assert await session.transcribe_final() == "Ryszardzie, włącz światło"
        assert [call["beam_size"] for call in transcriber.calls] == [1, 4]
        assert transcriber.calls[0]["frames"] == b"a" * (DEFAULT_STT_AUDIO_FORMAT.byte_rate // 2)
        assert transcriber.calls[1]["frames"] == b"a" * DEFAULT_STT_AUDIO_FORMAT.byte_rate

    asyncio.run(run())


def test_faster_whisper_streaming_stt_warns_and_drops_stale_partial_without_text(caplog) -> None:
    async def run() -> None:
        transcriber = FakeTranscriber(text="tajny tekst tła", delay_seconds=0.05)
        stt = FasterWhisperSpeechToText(
            SttConfig(
                model="fake",
                language="pl",
                device="cpu",
                compute_type="int8",
                partial_interval_seconds=0.01,
                partial_window_seconds=1.0,
                partial_max_backlog_seconds=0.001,
            ),
            transcriber_factory=lambda _config: transcriber,
        )

        with caplog.at_level(logging.WARNING, logger="ai_server.speech_to_text.faster_whisper"):
            await stt.start()
            session = await stt.create_streaming_session("backlog-session")
            await session.send_audio(PcmAudioChunk(data=b"a" * DEFAULT_STT_AUDIO_FORMAT.byte_rate))
            await asyncio.sleep(0.02)
            await session.send_audio(PcmAudioChunk(data=b"b" * DEFAULT_STT_AUDIO_FORMAT.byte_rate))
            await asyncio.wait_for(_wait_until(lambda: "dropping stale partial" in caplog.text), timeout=1)
            await session.close()

    asyncio.run(run())

    assert "streaming STT partial backlog_seconds=" in caplog.text
    assert "tajny tekst tła" not in caplog.text


def test_faster_whisper_model_loader_uses_local_files_only(monkeypatch) -> None:
    calls = []

    class FakeWhisperModel:
        def __init__(self, *args, **kwargs) -> None:
            calls.append((args, kwargs))

    monkeypatch.setattr("ai_server.speech_to_text.faster_whisper.WhisperModel", FakeWhisperModel)

    _create_faster_whisper_transcriber(
        SttConfig(
            model="large",
            language="pl",
            device="cuda",
            compute_type="int8_float16",
            local_files_only=True,
        )
    )

    assert calls == [
        (
            ("large",),
            {
                "device": "cuda",
                "compute_type": "int8_float16",
                "local_files_only": True,
            },
        )
    ]


async def _wait_until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0)
