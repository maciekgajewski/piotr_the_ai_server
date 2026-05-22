import asyncio
import sys
from types import SimpleNamespace

from ai_server.config import MicrophoneConfig
from ai_server.microphones.box3_esphome import Box3EsphomeMicrophoneDriver
from ai_server.microphones.types import MicrophoneContext, PlaybackTarget


def test_box3_driver_from_config() -> None:
    driver = Box3EsphomeMicrophoneDriver.from_config(
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

    assert driver.context == MicrophoneContext(
        type="box3_esphome",
        name="box3-office",
        location="office",
    )
    assert driver.playback_target == PlaybackTarget(
        type="box3_esphome",
        name="box3-office",
        address="box.local",
        api_key="secret",
        expected_name="box3-office",
    )


def test_box3_driver_wait_for_utterance_collects_audio_and_sends_events(monkeypatch) -> None:
    async def run() -> None:
        driver = Box3EsphomeMicrophoneDriver.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                options={"address": "box.local", "api_key": "secret"},
            )
        )
        events = []

        async def fake_ensure_connected() -> None:
            pass

        def fake_send_voice_assistant_event(event_name: str) -> None:
            events.append(event_name)
            if event_name == "VOICE_ASSISTANT_STT_VAD_END":
                driver._stream_done.set()

        monkeypatch.setattr(driver, "_ensure_connected", fake_ensure_connected)
        monkeypatch.setattr(driver, "_send_voice_assistant_event", fake_send_voice_assistant_event)

        task = asyncio.create_task(driver.wait_for_utterance(0.01))
        await asyncio.sleep(0)
        await driver._handle_start("conversation", 0, object(), "Ryszardzie")
        await driver._handle_audio(b"one", b"two")
        utterance = await asyncio.wait_for(task, timeout=1)

        assert utterance.wake_word == "Ryszardzie"
        assert utterance.audio_chunks == (b"one", b"two")
        assert events == ["VOICE_ASSISTANT_STT_VAD_END", "VOICE_ASSISTANT_RUN_END"]

    asyncio.run(run())


def test_box3_driver_updates_playback_target_to_connected_ip(monkeypatch) -> None:
    class FakeClient:
        connected_address = "192.168.0.180"

        def __init__(self, *args, **kwargs) -> None:
            self.subscribed = False

        async def connect(self, login: bool) -> None:
            assert login is True

        def subscribe_voice_assistant(self, **kwargs):
            self.subscribed = True
            return lambda: None

    fake_module = SimpleNamespace(APIClient=FakeClient)
    monkeypatch.setitem(sys.modules, "aioesphomeapi", fake_module)

    async def run() -> None:
        driver = Box3EsphomeMicrophoneDriver.from_config(
            MicrophoneConfig(
                type="box3_esphome",
                name="box3-office",
                location=None,
                options={"address": "piotr-box3-01-cbfaA8.local", "api_key": "secret"},
            )
        )

        await driver._ensure_connected()

        assert driver.playback_target.address == "192.168.0.180"

    asyncio.run(run())
