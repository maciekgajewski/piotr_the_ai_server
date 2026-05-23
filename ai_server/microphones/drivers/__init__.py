from __future__ import annotations

from ai_server.config import MicrophoneConfig
from ai_server.microphones.interfaces import Microphone
from ai_server.microphones.drivers.box3_esphome import Box3EsphomeMicrophone


def create_microphone(config: MicrophoneConfig) -> Microphone:
    if config.type == "box3_esphome":
        return Box3EsphomeMicrophone.from_config(config)

    raise ValueError(f"unsupported microphone type: {config.type}")
