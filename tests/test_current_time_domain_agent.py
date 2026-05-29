import asyncio
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_server.domain_agents.current_time import CurrentTimeDomainAgent, TimezoneResolver
from ai_server.interfaces import Conversation


def test_current_time_domain_agent_uses_configured_timezone_and_location(tmp_path: Path) -> None:
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 29, 14, 5, tzinfo=zone),
    )
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "time", "command": {"query": "która jest godzina?"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["timezone"] == "Europe/Warsaw"
    assert result["time"] == "14:05"
    assert result["text"] == "Teraz w Wrocławiu jest 14:05."


def test_current_time_domain_agent_resolves_jacksonville_from_query(tmp_path: Path) -> None:
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 29, 8, 30, tzinfo=zone),
    )
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "time", "command": {"query": "która godzina jest teraz w jacksonville"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["timezone"] == "America/New_York"
    assert result["text"] == "Teraz w jacksonville jest 08:30."


def test_current_time_domain_agent_handles_date_components(tmp_path: Path) -> None:
    agent = CurrentTimeDomainAgent(
        timezone="Europe/Warsaw",
        location="Wrocław",
        cache_dir=tmp_path,
        now_factory=lambda zone: dt.datetime(2026, 5, 29, 14, 5, tzinfo=zone),
    )
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "time", "command": {"query": "jaki jest dziś dzień tygodnia?"}},
            {},
        )
    )

    assert result["text"] == "Dzisiaj w Wrocławiu jest piątek."
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
