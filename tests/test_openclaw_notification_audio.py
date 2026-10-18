from __future__ import annotations

import asyncio

import pytest

from ai_server.microphones.messages import SynthesizedAudioChunk, SynthesizedAudioEnd, SynthesizedAudioStart
from openclaw_voice.gateway.messages import GatewayNotification, GatewayNotifyRejected
from openclaw_voice.media.prepare import prepare_notification_audio
from openclaw_voice.media.settings import NotificationAudioSettings


def _notification(*, title="Title", body="Body") -> GatewayNotification:
    return GatewayNotification(
        invocation_id="invoke-1", node_id="node-1", idempotency_key="dedupe-1",
        title=title, body=body, sound=None, priority=None, delivery=None,
        remaining_ms=30000,
    )


def _settings(**overrides) -> NotificationAudioSettings:
    values = dict(
        max_audio_bytes=1000, max_audio_seconds=5,
        title_body_pause_seconds=0.25, synthesis_timeout_seconds=1,
        synthesis_cleanup_timeout_seconds=0.1,
        cleanup_and_receipt_margin_seconds=0.5,
    )
    values.update(overrides)
    return NotificationAudioSettings(**values)


class _FakeTts:
    def __init__(self, pcm_by_text: dict[str, bytes]) -> None:
        self.pcm_by_text = pcm_by_text
        self.calls: list[str] = []

    async def synthesize(self, text: str):
        self.calls.append(text)
        yield SynthesizedAudioStart(rate=100, width=2, channels=1, volume=0.7)
        yield SynthesizedAudioChunk(self.pcm_by_text[text])
        yield SynthesizedAudioEnd()


class _FatalAudioExit(Exception):
    pass


class _FakeFatal:
    async def terminate(self, detail: str):
        raise _FatalAudioExit(detail)


def test_notification_preflight_prepares_both_fields_with_measured_pause():
    async def run():
        tts = _FakeTts({"Title": bytes(200), "Body": bytes(100)})
        prepared = await prepare_notification_audio(
            _notification(), tts, _settings(),
            deadline=asyncio.get_running_loop().time() + 5,
            fatal_termination=_FakeFatal(),
        )
        assert tts.calls == ["Title", "Body"]
        assert [segment.pcm for segment in prepared.segments] == [bytes(200), bytes(100)]
        assert prepared.pause_seconds == 0.25
        assert prepared.duration_seconds == 1.75

    asyncio.run(run())


@pytest.mark.parametrize("settings,pcm,code", [
    (_settings(max_audio_bytes=100), bytes(200), "TOO_LONG"),
    (_settings(max_audio_seconds=0.5), bytes(200), "TOO_LONG"),
])
def test_notification_preflight_rejects_audio_over_budget_before_playback(settings, pcm, code):
    async def run():
        tts = _FakeTts({"Body": pcm})
        with pytest.raises(GatewayNotifyRejected) as raised:
            await prepare_notification_audio(
                _notification(title=""), tts, settings,
                deadline=asyncio.get_running_loop().time() + 5,
                fatal_termination=_FakeFatal(),
            )
        assert raised.value.code == code

    asyncio.run(run())


def test_notification_preflight_rejects_when_measured_playback_will_miss_deadline():
    async def run():
        tts = _FakeTts({"Body": bytes(400)})
        with pytest.raises(GatewayNotifyRejected) as raised:
            await prepare_notification_audio(
                _notification(title=""), tts, _settings(),
                deadline=asyncio.get_running_loop().time() + 1.5,
                fatal_termination=_FakeFatal(),
            )
        assert raised.value.code == "DEADLINE_TOO_SHORT"

    asyncio.run(run())


def test_notification_preflight_bounds_stalled_synthesis():
    class _StalledTts:
        async def synthesize(self, text: str):
            await asyncio.Event().wait()
            yield SynthesizedAudioStart(rate=100, width=2, channels=1)

    async def run():
        with pytest.raises(GatewayNotifyRejected) as raised:
            await prepare_notification_audio(
                _notification(title=""), _StalledTts(),
                _settings(synthesis_timeout_seconds=0.01),
                deadline=asyncio.get_running_loop().time() + 5,
                fatal_termination=_FakeFatal(),
            )
        assert raised.value.code == "DEADLINE_TOO_SHORT"

    asyncio.run(run())


def test_notification_preflight_accepts_iterator_without_aclose():
    class _PlainIterator:
        def __init__(self):
            self._events = iter((
                SynthesizedAudioStart(rate=100, width=2, channels=1),
                SynthesizedAudioChunk(bytes(20)),
                SynthesizedAudioEnd(),
            ))

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration:
                raise StopAsyncIteration from None

    class _PlainTts:
        def synthesize(self, text: str):
            return _PlainIterator()

    async def run():
        prepared = await prepare_notification_audio(
            _notification(title=""), _PlainTts(), _settings(),
            deadline=asyncio.get_running_loop().time() + 5,
            fatal_termination=_FakeFatal(),
        )
        assert prepared.duration_seconds == 0.1

    asyncio.run(run())


def test_notification_preflight_counts_pause_against_duration_cap():
    async def run():
        tts = _FakeTts({"Title": bytes(100), "Body": bytes(100)})
        with pytest.raises(GatewayNotifyRejected) as raised:
            await prepare_notification_audio(
                _notification(), tts, _settings(max_audio_seconds=1.1),
                deadline=asyncio.get_running_loop().time() + 5,
                fatal_termination=_FakeFatal(),
            )
        assert raised.value.code == "TOO_LONG"

    asyncio.run(run())


def test_notification_preflight_joins_slow_tts_cleanup_before_rejection():
    cleaned = asyncio.Event()

    class _SlowCleanupTts:
        async def synthesize(self, text: str):
            try:
                yield SynthesizedAudioStart(rate=100, width=2, channels=1)
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.02)
                cleaned.set()

    async def run():
        with pytest.raises(GatewayNotifyRejected) as raised:
            await prepare_notification_audio(
                _notification(title=""), _SlowCleanupTts(),
                _settings(synthesis_timeout_seconds=0.01),
                deadline=asyncio.get_running_loop().time() + 5,
                fatal_termination=_FakeFatal(),
            )
        assert raised.value.code == "DEADLINE_TOO_SHORT"
        assert cleaned.is_set()

    asyncio.run(run())


def test_notification_preflight_fatally_contains_unjoinable_tts_cleanup():
    release_cleanup = asyncio.Event()

    class _UnjoinableTts:
        async def synthesize(self, text: str):
            try:
                yield SynthesizedAudioStart(rate=100, width=2, channels=1)
                await asyncio.Event().wait()
            finally:
                await release_cleanup.wait()

    async def run():
        before = asyncio.all_tasks()
        try:
            with pytest.raises(_FatalAudioExit, match="did not stop"):
                await prepare_notification_audio(
                    _notification(title=""), _UnjoinableTts(),
                    _settings(synthesis_timeout_seconds=0.01,
                              synthesis_cleanup_timeout_seconds=0.01),
                    deadline=asyncio.get_running_loop().time() + 5,
                    fatal_termination=_FakeFatal(),
                )
        finally:
            release_cleanup.set()
            workers = asyncio.all_tasks() - before
            if workers:
                await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), 1)

    asyncio.run(run())


def test_repeated_caller_cancellation_joins_tts_before_exit():
    entered = asyncio.Event()
    finalizing = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    class _DelayedCleanupTts:
        async def synthesize(self, text: str):
            try:
                entered.set()
                yield SynthesizedAudioStart(rate=100, width=2, channels=1)
                await asyncio.Event().wait()
            finally:
                finalizing.set()
                await release.wait()
                cleaned.set()

    async def run():
        caller = asyncio.create_task(prepare_notification_audio(
            _notification(title=""), _DelayedCleanupTts(),
            _settings(synthesis_cleanup_timeout_seconds=0.5),
            deadline=asyncio.get_running_loop().time() + 5,
            fatal_termination=_FakeFatal(),
        ))
        await entered.wait()
        caller.cancel()
        await finalizing.wait()
        caller.cancel()
        assert not caller.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert cleaned.is_set()

    asyncio.run(run())


def test_repeated_caller_cancellation_still_invokes_fatal_containment():
    entered = asyncio.Event()
    finalizing = asyncio.Event()
    release = asyncio.Event()

    class _UnjoinableTts:
        async def synthesize(self, text: str):
            try:
                entered.set()
                yield SynthesizedAudioStart(rate=100, width=2, channels=1)
                await asyncio.Event().wait()
            finally:
                finalizing.set()
                await release.wait()

    async def run():
        before = asyncio.all_tasks()
        caller = asyncio.create_task(prepare_notification_audio(
            _notification(title=""), _UnjoinableTts(),
            _settings(synthesis_cleanup_timeout_seconds=0.02),
            deadline=asyncio.get_running_loop().time() + 5,
            fatal_termination=_FakeFatal(),
        ))
        try:
            await entered.wait()
            caller.cancel()
            await finalizing.wait()
            caller.cancel()
            with pytest.raises(_FatalAudioExit, match="did not stop"):
                await caller
        finally:
            release.set()
            workers = asyncio.all_tasks() - before
            if workers:
                await asyncio.wait_for(asyncio.gather(*workers, return_exceptions=True), 1)

    asyncio.run(run())
