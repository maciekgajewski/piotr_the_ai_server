import asyncio
import logging
import socket
import struct
import sys
from types import SimpleNamespace
import urllib.request

import pytest

from ai_server.config import MicrophoneConfig
import ai_server.microphones.drivers.box3_esphome as box3_esphome
from ai_server.microphones.drivers import create_microphone
from ai_server.microphones.drivers.box3_esphome import Box3EsphomeMicrophone
from ai_server.microphones.interfaces import MicrophoneUnavailable
from ai_server.microphones.messages import AudioChunk, AudioEnd, AudioProgress, AudioStart, MessageEndCue, StartFollowUpListening
from ai_server.microphones.messages import OpenMicWakeCandidateRejected
from ai_server.microphones.messages import StartOpenMicListening
from ai_server.microphones.messages import StartWakeWordListening
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget


def test_box3_microphone_from_config() -> None:
    microphone = Box3EsphomeMicrophone.from_config(
        MicrophoneConfig(
            type="box3_esphome",
            name="box3-office",
            area="office",
            speech_peak_threshold=900,
            post_speech_ignore_seconds=1.5,
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
        area="office",
    )
    assert microphone.playback_target == PlaybackTarget(
        type="box3_esphome",
        name="box3-office",
        address="box.local",
        api_key="secret",
        expected_name="box3-office",
    )
    assert microphone._speech_peak_threshold == 900
    assert microphone._post_speech_ignore_seconds == 1.5


def test_create_microphone_returns_box3_esphome_microphone() -> None:
    microphone = create_microphone(
        MicrophoneConfig(
            type="box3_esphome",
            name="box3-office",
            area=None,
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
                area=None,
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
        monkeypatch.setattr(box3_esphome, "SPEECH_START_SECONDS", 0)
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
                area=None,
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
                area=None,
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


def test_box3_microphone_ignores_initial_follow_up_audio(monkeypatch) -> None:
    async def run() -> None:
        now = 100.0

        def fake_monotonic() -> float:
            return now

        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="voice-pe-bedroom",
                area="bedroom",
                post_speech_ignore_seconds=1.0,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        async def fake_ensure_connected() -> None:
            pass

        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(box3_esphome.time, "monotonic", fake_monotonic)

        await microphone._handle_start("conversation", 0, object(), "follow_up")
        assert microphone._stream_started_at == 101.0

        now = 100.5
        await microphone._handle_audio(_pcm16_chunk(4000))

        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioStart(wake_word="follow_up")
        assert microphone._events.empty()
        assert microphone._audio_chunk_count == 0
        assert microphone._ignored_audio_chunk_count == 1

        now = 101.1
        accepted_chunk = _pcm16_chunk(0)
        await microphone._handle_audio(accepted_chunk)

        assert microphone._events.empty()
        assert microphone._audio_chunk_count == 1
        assert microphone._pending_audio_chunks == [accepted_chunk]

    asyncio.run(run())


def test_box3_microphone_detects_initial_silence_timeout(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
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

        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioEnd()
        assert microphone._events.empty()
        assert microphone._pending_audio_chunks == []
        assert events == ["VOICE_ASSISTANT_STT_VAD_END"]

    asyncio.run(run())


def test_box3_microphone_keeps_open_mic_stream_running_during_initial_silence(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
                initial_silence_seconds=0.01,
                options={"address": "box.local", "api_key": "secret"},
            )
        )
        voice_assistant_events = []
        api_services = []

        async def fake_finish_voice_assistant_run() -> None:
            pass

        async def fake_execute_api_service(service_name: str) -> None:
            api_services.append(service_name)

        async def fake_ensure_connected() -> None:
            pass

        def fake_send_voice_assistant_event(event_name: str) -> None:
            voice_assistant_events.append(event_name)

        monkeypatch.setattr(microphone, "_finish_voice_assistant_run", fake_finish_voice_assistant_run)
        monkeypatch.setattr(microphone, "_execute_api_service", fake_execute_api_service)
        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(microphone, "_send_voice_assistant_event", fake_send_voice_assistant_event)

        await microphone.send_output_event(StartOpenMicListening())
        await microphone._handle_start("conversation", 0, object(), None)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioStart(wake_word=None)

        await asyncio.sleep(0.02)
        silence_chunk = _pcm16_chunk(0)
        await microphone._handle_audio(silence_chunk)

        assert api_services == ["start_open_mic_listening"]
        assert microphone._events.empty()
        assert microphone._audio_ended is False
        assert microphone._pending_audio_chunks == [silence_chunk]
        assert voice_assistant_events == []

    asyncio.run(run())


def test_box3_microphone_emits_open_mic_audio_progress_during_silence(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        async def fake_finish_voice_assistant_run() -> None:
            pass

        async def fake_execute_api_service(_service_name: str) -> None:
            pass

        async def fake_ensure_connected() -> None:
            pass

        monkeypatch.setattr(microphone, "_finish_voice_assistant_run", fake_finish_voice_assistant_run)
        monkeypatch.setattr(microphone, "_execute_api_service", fake_execute_api_service)
        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)

        await microphone.send_output_event(StartOpenMicListening())
        await microphone._handle_start("conversation", 0, object(), None)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioStart(wake_word=None)

        silence_chunk = _pcm16_chunk(0)
        for _ in range(box3_esphome.OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL):
            await microphone._handle_audio(silence_chunk)

        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioProgress(
            chunks=box3_esphome.OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL,
            bytes=len(silence_chunk) * box3_esphome.OPEN_MIC_AUDIO_PROGRESS_CHUNK_INTERVAL,
        )
        assert microphone._audio_ended is False
        assert microphone._voice_assistant_run_active is True

    asyncio.run(run())


def test_box3_microphone_drains_stale_events_before_new_open_mic_stream(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        async def fake_ensure_connected() -> None:
            pass

        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        microphone._events.put_nowait(AudioProgress(chunks=50, bytes=51200))
        microphone._events.put_nowait(AudioChunk(data=b"stale-audio"))
        microphone._events.put_nowait(AudioEnd())
        microphone._listening_mode = "open_mic"

        await microphone._handle_start("conversation", 0, object(), None)

        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioStart(wake_word=None)
        assert microphone._events.empty()

    asyncio.run(run())


def test_box3_microphone_drains_stale_events_before_open_mic_rearm(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )
        api_services = []

        async def fake_finish_voice_assistant_run() -> None:
            pass

        async def fake_execute_api_service(service_name: str) -> None:
            api_services.append(service_name)

        monkeypatch.setattr(microphone, "_finish_voice_assistant_run", fake_finish_voice_assistant_run)
        monkeypatch.setattr(microphone, "_execute_api_service", fake_execute_api_service)
        microphone._events.put_nowait(AudioProgress(chunks=50, bytes=51200))
        microphone._events.put_nowait(AudioChunk(data=b"stale-audio"))
        microphone._events.put_nowait(AudioEnd())

        await microphone.send_output_event(StartOpenMicListening())

        assert api_services == ["start_open_mic_listening"]
        assert microphone._events.empty()

    asyncio.run(run())


def test_box3_microphone_keeps_open_mic_stream_running_after_speech_segment(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
                end_silence_seconds=0.01,
                options={"address": "box.local", "api_key": "secret"},
            )
        )
        voice_assistant_events = []

        async def fake_finish_voice_assistant_run() -> None:
            pass

        async def fake_execute_api_service(_service_name: str) -> None:
            pass

        async def fake_ensure_connected() -> None:
            pass

        def fake_send_voice_assistant_event(event_name: str) -> None:
            voice_assistant_events.append(event_name)

        monkeypatch.setattr(microphone, "_finish_voice_assistant_run", fake_finish_voice_assistant_run)
        monkeypatch.setattr(microphone, "_execute_api_service", fake_execute_api_service)
        monkeypatch.setattr(microphone, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(microphone, "_send_voice_assistant_event", fake_send_voice_assistant_event)
        monkeypatch.setattr(box3_esphome, "SPEECH_START_SECONDS", 0.01)

        await microphone.send_output_event(StartOpenMicListening())
        await microphone._handle_start("conversation", 0, object(), None)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioStart(wake_word=None)

        speech_chunk = _pcm16_chunk(2000)
        silence_chunk = _pcm16_chunk(0)
        await microphone._handle_audio(speech_chunk)
        await asyncio.sleep(0.02)
        await microphone._handle_audio(speech_chunk)
        await asyncio.sleep(0.02)
        await microphone._handle_audio(silence_chunk)

        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=speech_chunk)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=speech_chunk)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioChunk(data=silence_chunk)
        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioEnd()
        assert voice_assistant_events == []
        assert microphone._audio_ended is False
        assert microphone._voice_assistant_run_active is True

    asyncio.run(run())


def test_box3_microphone_drops_second_callback_chunk_if_first_chunk_ends_audio(monkeypatch) -> None:
    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
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
                area=None,
                initial_silence_seconds=0.05,
                end_silence_seconds=0.01,
                post_speech_ignore_seconds=0,
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

        assert await asyncio.wait_for(microphone.wait_for_event(), timeout=1) == AudioEnd()
        assert microphone._events.empty()
        assert microphone._pending_audio_chunks == []
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
                area=None,
                options={"address": "piotr-box3-01-cbfaA8.local", "api_key": "secret"},
            )
        )

        await microphone._ensure_connected()

        assert microphone.playback_target.address == "192.168.0.180"

    asyncio.run(run())


def test_box3_microphone_preserves_hostname_when_connected_address_is_ipv6(monkeypatch) -> None:
    class FakeClient:
        connected_address = "2a02:a317:e4df:b400:22f8:3bff:fe0a:cf27"

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def connect(self, login: bool) -> None:
            assert login is True

        def subscribe_voice_assistant(self, **kwargs):
            return lambda: None

    fake_module = SimpleNamespace(APIClient=FakeClient)
    monkeypatch.setitem(sys.modules, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "aioesphomeapi", fake_module)

    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="voice-pe-bedroom",
                area="bedroom",
                options={"address": "piotr-voice-pe-bedroom-01.local", "api_key": "secret"},
            )
        )

        await microphone._ensure_connected()

        assert microphone.playback_target.address == "piotr-voice-pe-bedroom-01.local"

    asyncio.run(run())


def test_box3_microphone_reports_connection_failure_as_unavailable(monkeypatch) -> None:
    class APIConnectionError(Exception):
        pass

    class FakeClient:
        disconnect_called = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def connect(self, login: bool) -> None:
            assert login is True
            raise APIConnectionError("offline")

        async def disconnect(self) -> None:
            FakeClient.disconnect_called = True

    fake_module = SimpleNamespace(APIClient=FakeClient, APIConnectionError=APIConnectionError)
    monkeypatch.setitem(sys.modules, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "aioesphomeapi", fake_module)

    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        with pytest.raises(MicrophoneUnavailable, match="address=box.local"):
            await microphone._ensure_connected()

        assert microphone._client is None
        assert FakeClient.disconnect_called is True

    asyncio.run(run())


def test_box3_microphone_reports_playback_routing_failure_as_unavailable(monkeypatch) -> None:
    class FakeClient:
        connected_address = "2a02:a317:e4df:b400:22f8:3bff:fe0a:cf27"
        disconnect_called = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def connect(self, login: bool) -> None:
            assert login is True

        async def disconnect(self) -> None:
            FakeClient.disconnect_called = True

        def subscribe_voice_assistant(self, **kwargs):
            return lambda: None

    async def fake_resolve_connect_host(host: str, port: int = box3_esphome.API_PORT) -> str:
        return "2a02:a317:e4df:b400:22f8:3bff:fe0a:cf27"

    def fake_local_ip_for(host: str, port: int = box3_esphome.API_PORT) -> str:
        raise socket.gaierror(-9, "Address family for hostname not supported")

    fake_module = SimpleNamespace(APIClient=FakeClient)
    monkeypatch.setitem(sys.modules, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "_resolve_connect_host", fake_resolve_connect_host)
    monkeypatch.setattr(box3_esphome, "_local_ip_for", fake_local_ip_for)

    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="voice-pe-bedroom",
                area="bedroom",
                options={"address": "piotr-voice-pe-bedroom-01.local", "api_key": "secret"},
            )
        )

        with pytest.raises(MicrophoneUnavailable, match="Address family for hostname not supported"):
            await microphone.send_output_event(AudioStart(rate=22050, width=2, channels=1, volume=0.7))

        assert microphone._client is None
        assert FakeClient.disconnect_called is True

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
                area=None,
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


def test_box3_microphone_pauses_open_mic_before_playback(monkeypatch) -> None:
    class MediaPlayerInfo:
        key = 7

    class FakeClient:
        connected_address = "127.0.0.1"

        async def list_entities_services(self):
            return [MediaPlayerInfo()], []

        def media_player_command(self, key: int, **kwargs) -> None:
            assert key == 7
            actions.append(("media_player_command", kwargs))

    actions = []

    async def fake_resolve_connect_host(host: str, port: int = box3_esphome.API_PORT) -> str:
        return "127.0.0.1"

    monkeypatch.setattr(box3_esphome, "_resolve_connect_host", fake_resolve_connect_host)
    monkeypatch.setattr(box3_esphome, "_local_ip_for", lambda host, port=box3_esphome.API_PORT: "127.0.0.1")

    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        def fake_send_voice_assistant_event(event_name: str) -> None:
            actions.append(("voice_assistant_event", event_name))
            if event_name == "VOICE_ASSISTANT_RUN_END":
                microphone._stream_done.set()

        monkeypatch.setattr(microphone, "_send_voice_assistant_event", fake_send_voice_assistant_event)
        microphone._client = FakeClient()
        microphone._listening_mode = "open_mic"
        microphone._voice_assistant_run_active = True
        microphone._stream_done.clear()

        await microphone.send_output_event(AudioStart(rate=22050, width=2, channels=1, volume=None))

        assert actions[0] == ("voice_assistant_event", "VOICE_ASSISTANT_RUN_END")
        assert actions[1][0] == "media_player_command"
        assert microphone._listening_mode == "playback"
        assert microphone._voice_assistant_run_active is False
        if microphone._playback_stream is not None:
            microphone._playback_stream.close()

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
                UserService("start_open_mic_listening"),
                UserService("reset_open_mic_wake_candidate"),
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
                area=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        await microphone.send_output_event(MessageEndCue())
        await microphone.send_output_event(StartWakeWordListening())
        await microphone.send_output_event(StartFollowUpListening())
        await microphone.send_output_event(StartOpenMicListening())
        await microphone.send_output_event(OpenMicWakeCandidateRejected())

        assert microphone._client.executed == [
            ("play_message_end_cue", {}),
            ("start_follow_up_listening", {}),
            ("start_open_mic_listening", {}),
            ("reset_open_mic_wake_candidate", {}),
        ]

    asyncio.run(run())


def test_box3_microphone_recovers_stale_stream_before_open_mic_rearm(monkeypatch) -> None:
    class UserService:
        def __init__(self, name: str) -> None:
            self.name = name
            self.key = 1
            self.args = []

    class FakeClient:
        instances = []
        connected_address = "127.0.0.1"

        def __init__(self, *args, **kwargs) -> None:
            self.executed = []
            self.disconnect_called = False
            FakeClient.instances.append(self)

        async def connect(self, login: bool) -> None:
            assert login is True

        async def disconnect(self) -> None:
            self.disconnect_called = True

        def subscribe_voice_assistant(self, **kwargs):
            return lambda: None

        async def list_entities_services(self):
            return [], [UserService("start_open_mic_listening")]

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
                area=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )
        await microphone._ensure_connected()
        stale_client = microphone._client
        microphone._voice_assistant_run_active = False
        microphone._run_end_sent = True
        microphone._audio_ended = True
        microphone._stream_done.clear()

        await microphone.send_output_event(StartOpenMicListening())

        assert stale_client.disconnect_called is True
        assert len(FakeClient.instances) == 2
        assert FakeClient.instances[-1].executed == [("start_open_mic_listening", {})]
        assert microphone._client is FakeClient.instances[-1]
        assert microphone._run_end_sent is False
        assert microphone._stream_done.is_set()

    asyncio.run(run())


def test_box3_microphone_reports_api_service_timeout_as_unavailable(monkeypatch) -> None:
    class UserService:
        def __init__(self, name: str) -> None:
            self.name = name
            self.key = 1
            self.args = []

    class FakeClient:
        connected_address = "127.0.0.1"
        disconnect_called = False

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def connect(self, login: bool) -> None:
            assert login is True

        async def disconnect(self) -> None:
            FakeClient.disconnect_called = True

        def subscribe_voice_assistant(self, **kwargs):
            return lambda: None

        async def list_entities_services(self):
            return [], [UserService("start_open_mic_listening")]

        async def execute_service(self, service, data):
            await asyncio.Event().wait()

    fake_module = SimpleNamespace(APIClient=FakeClient)
    monkeypatch.setitem(sys.modules, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "aioesphomeapi", fake_module)
    monkeypatch.setattr(box3_esphome, "API_SERVICE_TIMEOUT_SECONDS", 0.01)

    async def run() -> None:
        microphone = Box3EsphomeMicrophone.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                area=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )

        with pytest.raises(MicrophoneUnavailable, match="service=start_open_mic_listening timed out"):
            await microphone.send_output_event(StartOpenMicListening())

        assert microphone._client is None
        assert FakeClient.disconnect_called is True

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
