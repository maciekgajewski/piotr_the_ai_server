from __future__ import annotations

import asyncio

from ai_server.microphones.interfaces import TextToSpeech
from ai_server.microphones.messages import SynthesizedAudioChunk, SynthesizedAudioEnd, SynthesizedAudioStart
from openclaw_voice.gateway.messages import GatewayNotification, GatewayNotifyRejected
from openclaw_voice.media.messages import PreparedNotificationAudio, PreparedSpeechSegment
from openclaw_voice.media.settings import NotificationAudioSettings
from openclaw_voice.utils.interfaces import FatalTermination
from openclaw_voice.utils.tasks import cancel_and_join_or_terminate


async def prepare_notification_audio(
    notification: GatewayNotification,
    tts: TextToSpeech,
    settings: NotificationAudioSettings,
    *,
    deadline: float,
    fatal_termination: FatalTermination,
) -> PreparedNotificationAudio:
    """Prepare all speech before playback, joining TTS even on deadline expiry.

    The caller supplies the native request deadline and a process fatal controller.
    An unjoinable TTS worker must terminate the process: returning would leave
    audio work running after a notification has been rejected.
    """
    loop = asyncio.get_running_loop()
    margin = settings.cleanup_and_receipt_margin_seconds
    cleanup_timeout = settings.synthesis_cleanup_timeout_seconds
    if deadline - loop.time() <= margin + cleanup_timeout:
        raise GatewayNotifyRejected("DEADLINE_TOO_SHORT", "notification deadline is too short")
    synthesis_deadline = min(
        deadline - margin - cleanup_timeout,
        loop.time() + settings.synthesis_timeout_seconds,
    )
    task = asyncio.create_task(_synthesize(notification, tts, settings))
    try:
        done, _ = await asyncio.wait({task}, timeout=max(0.0, synthesis_deadline - loop.time()))
        if not done:
            cancelled_during_cleanup = await cancel_and_join_or_terminate(
                task, timeout=cleanup_timeout, fatal_termination=fatal_termination,
                detail="notification TTS did not stop after cancellation",
            )
            if cancelled_during_cleanup:
                raise asyncio.CancelledError
            raise GatewayNotifyRejected("DEADLINE_TOO_SHORT", "notification synthesis exceeded the deadline")
        prepared = task.result()
    except asyncio.CancelledError:
        if not task.done():
            await cancel_and_join_or_terminate(
                task, timeout=cleanup_timeout, fatal_termination=fatal_termination,
                detail="notification TTS did not stop after cancellation",
            )
        raise
    if prepared.duration_seconds + margin > deadline - loop.time():
        raise GatewayNotifyRejected("DEADLINE_TOO_SHORT", "notification playback cannot fit the deadline")
    return prepared


async def _synthesize(
    notification: GatewayNotification,
    tts: TextToSpeech,
    settings: NotificationAudioSettings,
) -> PreparedNotificationAudio:
    texts = tuple(text for text in (notification.title, notification.body) if text)
    assert 1 <= len(texts) <= 2
    pause = settings.title_body_pause_seconds if len(texts) == 2 else 0.0
    segments: list[PreparedSpeechSegment] = []
    total_bytes = 0
    total_duration = pause
    for text in texts:
        stream = tts.synthesize(text)
        try:
            start: SynthesizedAudioStart | None = None
            chunks: list[bytes] = []
            ended = False
            async for event in stream:
                if isinstance(event, SynthesizedAudioStart):
                    assert start is None and not ended
                    assert event.rate > 0 and event.width > 0 and event.channels > 0
                    start = event
                elif isinstance(event, SynthesizedAudioChunk):
                    assert start is not None and not ended
                    assert isinstance(event.data, bytes) and event.data
                    frame_bytes = start.width * start.channels
                    assert len(event.data) % frame_bytes == 0
                    total_bytes += len(event.data)
                    if total_bytes > settings.max_audio_bytes:
                        raise GatewayNotifyRejected("TOO_LONG", "notification audio exceeds the byte limit")
                    chunks.append(event.data)
                elif isinstance(event, SynthesizedAudioEnd):
                    assert start is not None and chunks and not ended
                    ended = True
                else:
                    raise AssertionError("unsupported synthesized audio event")
            assert start is not None and ended
            segment = PreparedSpeechSegment(
                rate=start.rate, width=start.width, channels=start.channels,
                volume=start.volume, pcm=b"".join(chunks),
            )
            total_duration += segment.duration_seconds
            if total_duration > settings.max_audio_seconds:
                raise GatewayNotifyRejected("TOO_LONG", "notification audio exceeds the duration limit")
            segments.append(segment)
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
    return PreparedNotificationAudio(tuple(segments), pause)
