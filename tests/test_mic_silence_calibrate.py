from ai_server.config import MicrophoneConfig
from tools.lib import mic_silence_calibrate


def test_summarize_peaks_uses_configured_discard_window_and_recommends_threshold() -> None:
    summary = mic_silence_calibrate.summarize_peaks(
        peaks=[
            mic_silence_calibrate.ChunkPeak(seconds_since_start=0.1, peak=8000, byte_count=2),
            mic_silence_calibrate.ChunkPeak(seconds_since_start=0.6, peak=200, byte_count=2),
            mic_silence_calibrate.ChunkPeak(seconds_since_start=0.7, peak=300, byte_count=2),
            mic_silence_calibrate.ChunkPeak(seconds_since_start=0.8, peak=400, byte_count=2),
        ],
        discard_seconds=0.5,
        min_threshold=500,
        margin=1.5,
        round_to=50,
    )

    assert summary.chunk_count == 3
    assert summary.p50 == 300
    assert summary.p99 == 400
    assert summary.max_peak == 400
    assert summary.recommended_threshold == 600


def test_select_microphone_config_requires_name_when_multiple() -> None:
    microphones = (
        MicrophoneConfig(type="box3_esphome", name="box3-office", area=None, options={}),
        MicrophoneConfig(type="box3_esphome", name="voice-pe-bedroom", area=None, options={}),
    )

    try:
        mic_silence_calibrate.select_microphone_config(microphones, None)
    except ValueError as error:
        assert "--microphone is required" in str(error)
    else:
        raise AssertionError("expected ValueError")

    selected = mic_silence_calibrate.select_microphone_config(microphones, "voice-pe-bedroom")
    assert selected.name == "voice-pe-bedroom"
