import asyncio
import logging
import struct
import sys
from types import SimpleNamespace
import urllib.request

import pytest

from ai_server.config import MicrophoneConfig
import ai_server.microphones.drivers.box3_esphome as box3_esphome
from ai_server.microphones.drivers import create_microphone
from ai_server.microphones.drivers.box3_esphome import Box3EsphomeMicrophone
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioStart, MessageEndCue, StartFollowUpListening
from ai_server.microphones.messages import StartWakeWordListening
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget


def test_box3_microphone_from_config() -> None:
    microphone = Box3EsphomeMicrophone.from_config(
        MicrophoneConfig(
            type="box3_esphome",
            name="box3-office",
            location="office",
            options={
                "address": "box.local",
                "api_key": "secret",
                "expected_name": "box3-office",
            },
        )
    )

    assert microphone.context == MicrophoneContext(
        type="box3_esphome",
        name="box3-office",
        location="office",
    )
    assert microphone.playback_target == PlaybackTarget(
        type="box3_esphome",
        name="box3-office",
        address="box.local",
        api_key="secret",
        expected_name="box3-office",
    )


def test_create_microphone_returns_box3_esphome_microphone() -> None:
    microphone = create_microphone(
        MicrophoneConfig(
            type="box3_esphome",
            name="box3-office",
            location=None,
            options={"address": "box.local", "api_key": "secret"},
        )
    )

    assert isinstance(microphone, Box3EsphomeMicrophone)


def test_box3_microphone_emits_audio_events_without_automatic_run_end(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                end_silence_seconds=0.01,
                options={"address": "box.local", "api_key": "secret"},
            )
        )
        events = []

        async def fake_ensure_connected() -> None:
            pass

        def fake_send_voice_assistant_event(event_name: str) -> None:
            events.append(event_name)

        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(microphone, "_send_voice_assistant_event", fake_send_voice_assistant_event)
        start_task = asyncio.create_task(microphone.wait_for_event())
        await asyncio.sleep(0)
        await microphone._handle_start("conversation", 0, object(), "Ryszardzie")
        start_event = await asyncio.wait_for(start_task, timeout=1)
        await microphone._handle_audio(b"one", b"two")
        await microphone._handle_stop(False)

        assert start_event == AudioStart(wake_word="Ryszardzie")
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=b"one")
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=b"two")
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioEnd()
        await asyncio.sleep(0)
        assert events == []

    asyncio.run(run())


def test_box3_microphone_detects_end_of_speech_and_stops_stream(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                end_silence_seconds=0.01,
                options={"address": "box.local", "api_key": "secret"},
            )
        )
        events = []

        async def fake_ensure_connected() -> None:
            pass

        def fake_send_voice_assistant_event(event_name: str) -> None:
            events.append(event_name)
            if event_name == "VOICE_ASSISTANT_STT_VAD_END":
                microphone._stream_done.set()

        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(microphone, "_send_voice_assistant_event", fake_send_voice_assistant_event)
        monkeypatch.setattr(box3_esphome, "SPEECH_START_SECONDS", 0.01)
        monkeypatch.setattr(box3_esphome, "VOICE_ASSISTANT_REARM_DELAY_SECONDS", 0)

        start_task = asyncio.create_task(microphone.wait_for_event())
        await asyncio.sleep(0)
        await microphone._handle_start("conversation", 0, object(), "Ryszardzie")
        start_event = await asyncio.wait_for(start_task, timeout=1)
        await microphone._handle_audio(_pcm16_chunk(2000))
        await asyncio.sleep(0.02)
        await microphone._handle_audio(_pcm16_chunk(2000))
        await asyncio.sleep(0.02)
        await microphone._handle_audio(_pcm16_chunk(0))

        assert start_event == AudioStart(wake_word="Ryszardzie")
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=_pcm16_chunk(2000))
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=_pcm16_chunk(2000))
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=_pcm16_chunk(0))
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioEnd()
        assert events == ["VOICE_ASSISTANT_STT_VAD_END"]
        await microphone.send_output_event(StartWakeWordListening())
        assert events == ["VOICE_ASSISTANT_STT_VAD_END", "VOICE_ASSISTANT_RUN_END"]

    asyncio.run(run())


def test_box3_microphone_drops_audio_chunks_after_audio_end(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                end_silence_seconds=0.01,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        async def fake_ensure_connected() -> None:
            pass

        def fake_send_voice_assistant_event(_event_name: str) -> None:
            pass

        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(microphone, "_send_voice_assistant_event", fake_send_voice_assistant_event)
        monkeypatch.setattr(box3_esphome, "SPEECH_START_SECONDS", 0.01)

        await microphone._handle_start("conversation", 0, object(), "Ryszardzie")
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioStart(wake_word="Ryszardzie")

        await microphone._handle_stop(False)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioEnd()

        await microphone._handle_audio(b"late-one", b"late-two")
        assert microphone._events.empty()

    asyncio.run(run())


def test_box3_microphone_detects_initial_silence_timeout(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                initial_silence_seconds=0.01,
                options={"address": "box.local", "api_key": "secret"},
            )
        )
        events = []

        async def fake_ensure_connected() -> None:
            pass

        def fake_send_voice_assistant_event(event_name: str) -> None:
            events.append(event_name)

        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(microphone, "_send_voice_assistant_event", fake_send_voice_assistant_event)
        monkeypatch.setattr(box3_esphome, "SPEECH_START_SECONDS", 0.01)

        await microphone._handle_start("conversation", 0, object(), "Ryszardzie")
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioStart(wake_word="Ryszardzie")

        await asyncio.sleep(0.02)
        silence_chunk = _pcm16_chunk(0)
        await microphone._handle_audio(silence_chunk)

        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=silence_chunk)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioEnd()
        assert events == ["VOICE_ASSISTANT_STT_VAD_END"]

    asyncio.run(run())


def test_box3_microphone_drops_second_callback_chunk_if_first_chunk_ends_audio(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                end_silence_seconds=0.01,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        async def fake_ensure_connected() -> None:
            pass

        def fake_send_voice_assistant_event(_event_name: str) -> None:
            pass

        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(microphone, "_send_voice_assistant_event", fake_send_voice_assistant_event)
        monkeypatch.setattr(box3_esphome, "SPEECH_START_SECONDS", 0.01)

        await microphone._handle_start("conversation", 0, object(), "Ryszardzie")
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioStart(wake_word="Ryszardzie")

        speech_chunk = _pcm16_chunk(2000)
        silence_chunk = _pcm16_chunk(0)
        await microphone._handle_audio(speech_chunk)
        await asyncio.sleep(0.02)
        await microphone._handle_audio(speech_chunk)
        await asyncio.sleep(0.02)
        await microphone._handle_audio(silence_chunk, b"late-second")

        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=speech_chunk)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=speech_chunk)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=silence_chunk)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioEnd()
        assert microphone._events.empty()

    asyncio.run(run())


def test_box3_microphone_ignores_short_startup_audio_blip(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                initial_silence_seconds=0.05,
                end_silence_seconds=0.01,
                options={"address": "box.local", "api_key": "secret"},
            )
        )
        events = []

        async def fake_ensure_connected() -> None:
            pass

        def fake_send_voice_assistant_event(event_name: str) -> None:
            events.append(event_name)

        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(microphone, "_send_voice_assistant_event", fake_send_voice_assistant_event)

        await microphone._handle_start("conversation", 0, object(), "follow_up")
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioStart(wake_word="follow_up")

        blip = _pcm16_chunk(8000)
        silence = _pcm16_chunk(0)
        await microphone._handle_audio(blip)
        await asyncio.sleep(0.02)
        await microphone._handle_audio(silence)
        await asyncio.sleep(0.04)
        await microphone._handle_audio(silence)

        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=blip)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=silence)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=silence)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioEnd()
        assert events == ["VOICE_ASSISTANT_STT_VAD_END"]

    asyncio.run(run())


def test_box3_microphone_updates_playback_target_to_connected_ip(monkeypatch) -> None:
    class FakeClient:
        connected_address = "192.168.0.180"

        def __init__(self, *args, **kwargs) -> None:
            self.subscribed = False

        async def connect(self, login: bool) -> None:
            assert login is True

        def subscribe_voice_assistant(self, **kwargs):
            self.subscribed = True
            return lambda: None

        async def list_entities_services(self):
            return [], []

    fake_module = SimpleNamespace(APIClient=FakeClient)
    monkeypatch.setitem(sys.modules, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "aioesphomeapi", fake_module)

    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                options={"address": "piotr-box3-01-cbfaA8.local", "api_key": "secret"},
            )
        )

        await microphone._ensure_connected()

        assert microphone.playback_target.address == "192.168.0.180"

    asyncio.run(run())


def test_box3_microphone_streams_audio_events_over_http(monkeypatch) -> None:
    class MediaPlayerInfo:
        key = 7

    class FakeClient:
        connected_address = "127.0.0.1"
        instances = []

        def __init__(self, *args, **kwargs) -> None:
            self.commands = []
            FakeClient.instances.append(self)

        async def connect(self, login: bool) -> None:
            assert login is True

        def subscribe_voice_assistant(self, **kwargs):
            return lambda: None

        async def list_entities_services(self):
            return [MediaPlayerInfo()], []

        def media_player_command(self, key: int, **kwargs) -> None:
            assert key == 7
            self.commands.append(kwargs)

    async def fake_resolve_connect_host(host: str, port: int = box3_esphome.API_PORT) -> str:
        return "127.0.0.1"

    fake_module = SimpleNamespace(APIClient=FakeClient)
    monkeypatch.setitem(sys.modules, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "_resolve_connect_host", fake_resolve_connect_host)
    monkeypatch.setattr(box3_esphome, "_local_ip_for", lambda host, port=box3_esphome.API_PORT: "127.0.0.1")

    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        await microphone.send_output_event(AudioStart(rate=22050, width=2, channels=1, volume=0.7))
        client = FakeClient.instances[-1]
        assert client.commands[0] == {"volume": 0.7}
        url = client.commands[1]["media_url"]

        read_task = asyncio.create_task(asyncio.to_thread(_read_url, url))
        await asyncio.sleep(0)
        await microphone.send_output_event(AudioChunk(data=b"abc"))
        await microphone.send_output_event(AudioEnd())
        data = await asyncio.wait_for(read_task, timeout=1)

        assert data[:4] == b"RIFF"
        assert data[44:] == b"abc"

    asyncio.run(run())


def test_box3_microphone_executes_cue_and_follow_up_services(monkeypatch) -> None:
    class UserService:
        def __init__(self, name: str) -> None:
            self.name = name
            self.key = 1
            self.args = []

    class FakeClient:
        connected_address = "127.0.0.1"

        def __init__(self, *args, **kwargs) -> None:
            self.executed = []

        async def connect(self, login: bool) -> None:
            assert login is True

        def subscribe_voice_assistant(self, **kwargs):
            return lambda: None

        async def list_entities_services(self):
            return [], [
                UserService("play_message_end_cue"),
                UserService("start_follow_up_listening"),
            ]

        async def execute_service(self, service, data):
            self.executed.append((service.name, data))

    fake_module = SimpleNamespace(APIClient=FakeClient)
    monkeypatch.setitem(sys.modules, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "aioesphomeapi", fake_module)

    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        await microphone.send_output_event(MessageEndCue())
        await microphone.send_output_event(StartWakeWordListening())
        await microphone.send_output_event(StartFollowUpListening())

        assert microphone._client.executed == [
            ("play_message_end_cue", {}),
            ("start_follow_up_listening", {}),
        ]

    asyncio.run(run())


def test_box3_playback_stream_estimates_remaining_speaker_drain_time() -> None:
    stream = box3_esphome.Box3PlaybackStream(
        local_ip="127.0.0.1",
        rate=10,
        width=2,
        channels=1,
        logger=logging.getLogger("test"),
    )
    stream.start()
    try:
        stream.write(b"1234567890")
        stream.first_audio_drained_at = 100.0

        assert stream.audio_seconds == 0.5
        assert stream.remaining_playback_seconds(now=100.1) == pytest.approx(0.6)
        assert stream.remaining_playback_seconds(now=101.0) == 0.0
    finally:
        stream.close()


def _pcm16_chunk(value: int, samples: int = 512) -> bytes:
    return struct.pack("<" + "h" * samples, *([value] * samples))


def _read_url(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=2) as response:
        return response.read()
