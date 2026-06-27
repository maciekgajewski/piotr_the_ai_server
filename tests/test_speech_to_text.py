import asyncio
import logging
from pathlib import Path
import wave

from ai_server.config import SttConfig
from ai_server.speech_to_text.faster_whisper import FasterWhisperSpeechToText
from ai_server.speech_to_text.messages import TextEnd, TextFragment
from ai_server.speech_to_text.types import DEFAULT_STT_AUDIO_FORMAT, PcmAudioChunk


class FakeTranscriber:
    def __init__(self, text: str = "włącz tryb ventilacji") -> None:
        self.text = text
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
