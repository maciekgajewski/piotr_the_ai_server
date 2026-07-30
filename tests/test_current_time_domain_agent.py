import asyncio
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from conftest import agent_context
from ai_server.domain_agents.current_time import CurrentTimeDomainAgent, TimezoneResolver


def _route(request: str) -> dict:
    return {"id": "t1", "domain": "time", "request": request, "depends_on": []}


def test_current_time_domain_agent_uses_configured_timezone_and_location(tmp_path: Path) -> None:
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 29, 14, 5, tzinfo=zone),
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            _route("która jest godzina?"),
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["timezone"] == "Europe/Warsaw"
    assert result["time"] == "14:05"
    assert result["text"] == "czternasta zero pięć"
    assert result["final_reply_mode"] == "verbatim"


def test_current_time_domain_agent_resolves_jacksonville_from_query(tmp_path: Path) -> None:
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 29, 8, 30, tzinfo=zone),
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            _route("która godzina jest teraz w jacksonville"),
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["timezone"] == "America/New_York"
    assert result["text"] == "Teraz w jacksonville jest ósma trzydzieści."


def test_current_time_domain_agent_uses_named_location_from_request(tmp_path: Path) -> None:
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 29, 8, 30, tzinfo=zone),
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice", "area": "office"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            _route("która godzina jest teraz w Jacksonville"),
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["timezone"] == "America/New_York"
    assert result["location"] == "Jacksonville"


def test_current_time_domain_agent_does_not_treat_conversation_area_as_location(tmp_path: Path) -> None:
    resolver = FailingTimezoneResolver()
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 30, 10, 4, tzinfo=zone),
        timezone_resolver=resolver,
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice", "area": "office"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            _route("Która godzina."),
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["timezone"] == "Europe/Warsaw"
    assert result["location"] == "Wrocław"
    assert result["time"] == "10:04"
    assert result["text"] == "dziesiąta zero cztery"
    assert not resolver.called


def test_current_time_domain_agent_formats_full_hour_as_words(tmp_path: Path) -> None:
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 30, 8, 0, tzinfo=zone),
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            _route("Która godzina?"),
            {},
        )
    )

    assert result["time"] == "08:00"
    assert result["text"] == "ósma zero zero"


def test_current_time_domain_agent_resolves_location_from_request_text(tmp_path: Path) -> None:
    resolver = MappingTimezoneResolver({"Jacksonville": "America/New_York"})
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 30, 4, 46, tzinfo=zone),
        timezone_resolver=resolver,
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            _route("która godzina jest teraz w Jacksonville"),
            {},
        )
    )

    assert result["timezone"] == "America/New_York"
    assert result["location"] == "Jacksonville"
    assert result["time"] == "04:46"
    assert resolver.calls == ["Jacksonville"]


def test_current_time_domain_agent_handles_date_components(tmp_path: Path) -> None:
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 29, 14, 5, tzinfo=zone),
    )
    conversation = agent_context(conversation_id="conversation-1", attributes={"medium": "voice"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            _route("jaki jest dziś dzień tygodnia?"),
            {},
        )
    )

    assert result["text"] == "Dzisiaj w Wrocławiu jest piątek."
    assert "final_reply_mode" not in result
    assert result["components"]["day_of_week"] == "piątek"


def test_timezone_resolver_caches_online_lookup(tmp_path: Path) -> None:
    resolver = FakeOnlineTimezoneResolver(
        cache_dir=tmp_path,
        configured_location="Wrocław",
        configured_timezone="Europe/Warsaw",
    )

    first_result = asyncio.run(resolver.resolve("Test City"))
    second_result = asyncio.run(resolver.resolve("Test City"))

    cache_path = tmp_path / "timezones" / "locations.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert first_result == "America/Chicago"
    assert second_result == "America/Chicago"
    assert resolver.online_lookup_count == 1
    assert cache["test city"]["timezone"] == "America/Chicago"


class FakeOnlineTimezoneResolver(TimezoneResolver):
    def __init__(self, *, cache_dir: Path, configured_location: str, configured_timezone: str) -> None:
        super().__init__(
            cache_dir=cache_dir,
            configured_location=configured_location,
            configured_timezone=configured_timezone,
        )
        self.online_lookup_count = 0

    async def _resolve_online(self, location: str) -> str:
        assert location == "Test City"
        self.online_lookup_count += 1
        ZoneInfo("America/Chicago")
        return "America/Chicago"


class FailingTimezoneResolver:
    def __init__(self) -> None:
        self.called = False

    async def resolve(self, location: str) -> str:
        self.called = True
        raise AssertionError(f"unexpected timezone lookup for {location}")

    async def close(self) -> None:
        pass


class MappingTimezoneResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls = []

    async def resolve(self, location: str) -> str:
        self.calls.append(location)
        return self._mapping[location]

    async def close(self) -> None:
        pass
