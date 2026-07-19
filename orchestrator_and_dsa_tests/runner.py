from __future__ import annotations

import argparse
import asyncio
import copy
import datetime as dt
import fnmatch
import json
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

import agent_tool_eval
from ai_server.orchestrator import OrchestratorAgent
from ai_server.domain_agents.current_time import PLANNING_PROMPT as TIME_PLANNING_PROMPT
from ai_server.domain_agents.current_time import CurrentTimeDomainAgent
from ai_server.domain_agents.home_assistant import PLANNING_PROMPT as HOME_ASSISTANT_PLANNING_PROMPT
from ai_server.domain_agents.home_assistant import HomeAssistantDomainAgent
from ai_server.domain_agents.media_player import MediaPlayerDomainAgent
from ai_server.domain_agents.media_player.agent import PLANNING_PROMPT as MEDIA_PLAYER_PLANNING_PROMPT
from ai_server.domain_agents.system_status.agent import PLANNING_PROMPT as SYSTEM_STATUS_PLANNING_PROMPT
from ai_server.domain_agents.system_status.agent import KNOWN_UTTERANCES as SYSTEM_STATUS_KNOWN_UTTERANCES
from ai_server.domain_agents.weather import CurrentWeather, WeatherDomainAgent, WeatherNowRequest
from ai_server.domain_agents.weather.agent import PLANNING_PROMPT as WEATHER_PLANNING_PROMPT
from ai_server.domain_agents.weather.astronomy import AstronomyRecord, AstronomySnapshot
from ai_server.domain_agents.wikipedia import PLANNING_PROMPT as WIKIPEDIA_PLANNING_PROMPT
from ai_server.domain_agents.wikipedia import WikipediaArticle, WikipediaDomainAgent, WikipediaSearchResult
from ai_server.conversations.agent_context import AgentExecutionContext
from ai_server.conversations.contexts import ConversationContext, ConversationMedium
from ai_server.conversations.messages import AssistantMessageCompleted, AssistantMessageStarted
from ai_server.conversations.messages import AssistantTextChunk, ProcessingUpdate, UserMessage
from ai_server.ollama_client import OllamaClient


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_SCENARIOS_DIR = ROOT / "scenarios"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:4b-instruct"
STUB_PLANNING_PROMPTS = {
    "home_assistant": HOME_ASSISTANT_PLANNING_PROMPT,
    "media_player": MEDIA_PLAYER_PLANNING_PROMPT,
    "system_status": SYSTEM_STATUS_PLANNING_PROMPT,
    "time": TIME_PLANNING_PROMPT,
    "weather": WEATHER_PLANNING_PROMPT,
    "wikipedia": WIKIPEDIA_PLANNING_PROMPT,
}


@dataclass(frozen=True)
class TestCase:
    group: str
    case_id: str
    kind: str
    raw: dict[str, Any]
    path: Path

    @property
    def name(self) -> str:
        return f"{self.group}/{self.case_id}"


@dataclass
class CaseResult:
    case: TestCase
    replies: list[str] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    task_results: list[dict[str, Any]] = field(default_factory=list)
    actual_calls: list[Any] = field(default_factory=list)
    planning_contexts: list[dict[str, Any]] = field(default_factory=list)
    model_requests: list[dict[str, Any]] = field(default_factory=list)
    model_responses: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return not self.failures


class RecordingOllamaClient:
    def __init__(self, *, base_url: str) -> None:
        self._inner = OllamaClient(base_url=base_url)
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(copy.deepcopy(payload))
        response = await self._inner.chat(payload)
        self.responses.append(copy.deepcopy(response))
        return response

    async def close(self) -> None:
        await self._inner.close()


class StubDomainAgent:
    def __init__(self, domain: str, results: dict[str, dict[str, Any]], traces: list[dict[str, Any]]) -> None:
        self._domain = domain
        self._results = results
        self._used_result_keys: set[str] = set()
        self._traces = traces

    def known_utterances(self) -> dict[str, dict[str, Any]]:
        if self._domain == "system_status":
            return SYSTEM_STATUS_KNOWN_UTTERANCES
        return {}

    def planning_prompt(self) -> str:
        return STUB_PLANNING_PROMPTS.get(self._domain, "")

    def query_capabilities(self) -> dict[str, Any]:
        return {}

    def query_capabilities_prompt(self) -> str:
        return ""

    async def run_task(
        self,
        conversation: AgentExecutionContext,
        task: dict[str, Any],
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        del conversation, active_context
        result = copy.deepcopy(self._result_for_task(task["id"]))
        self._traces.append({"task": copy.deepcopy(task), "result": copy.deepcopy(result)})
        return result

    async def close(self) -> None:
        pass

    def _result_for_task(self, task_id: str) -> dict[str, Any]:
        if task_id in self._results and task_id not in self._used_result_keys:
            self._used_result_keys.add(task_id)
            return self._results[task_id]
        for result_key, result in self._results.items():
            if result_key not in self._used_result_keys:
                self._used_result_keys.add(result_key)
                return result
        return {"status": "ok", "text": f"{self._domain} mocked"}


class TracingDomainAgent:
    def __init__(self, domain: str, inner: Any, traces: list[dict[str, Any]]) -> None:
        self._domain = domain
        self._inner = inner
        self._traces = traces

    def known_utterances(self) -> dict[str, dict[str, Any]]:
        return self._inner.known_utterances()

    def planning_prompt(self) -> str:
        return self._inner.planning_prompt()

    def query_capabilities(self) -> dict[str, Any]:
        return self._inner.query_capabilities()

    def query_capabilities_prompt(self) -> str:
        return self._inner.query_capabilities_prompt()

    async def run_task(
        self,
        conversation: AgentExecutionContext,
        task: dict[str, Any],
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        result = await self._inner.run_task(conversation, task, active_context)
        self._traces.append({"domain": self._domain, "task": copy.deepcopy(task), "result": copy.deepcopy(result)})
        return result

    async def close(self) -> None:
        await self._inner.close()


class FakeAgentChannel:
    def __init__(self, incoming: list[UserMessage]) -> None:
        self._incoming = list(incoming)
        self.sent: list[object] = []
        self._stream_open = False

    async def receive_user_message(self) -> UserMessage:
        if not self._incoming:
            raise AssertionError("unexpected receive")
        return self._incoming.pop(0)

    async def processing_update(self) -> None:
        assert not self._stream_open
        self.sent.append(ProcessingUpdate())

    async def start_assistant_message(self) -> None:
        assert not self._stream_open
        self._stream_open = True
        self.sent.append(AssistantMessageStarted())

    async def send_text(self, text: str) -> None:
        assert self._stream_open
        self.sent.append(AssistantTextChunk(text))

    async def complete_assistant_message(self) -> None:
        assert self._stream_open
        self._stream_open = False
        self.sent.append(AssistantMessageCompleted())

    async def send_message(self, text: str) -> None:
        await self.start_assistant_message()
        if text:
            await self.send_text(text)
        await self.complete_assistant_message()

    async def request_follow_up(self) -> None:
        if not self._incoming:
            raise AssertionError("behavior case requested an unavailable follow-up message")

    async def end_conversation(self) -> None:
        return None


class FakeTimezoneResolver:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = {_normalized(key): value for key, value in mapping.items()}
        self.calls: list[str] = []

    async def resolve(self, location: str) -> str:
        self.calls.append(location)
        timezone = self._mapping.get(_normalized(location))
        if timezone is None:
            raise RuntimeError(f"unknown timezone location: {location}")
        return timezone

    async def close(self) -> None:
        pass


class FakeWikipediaClient:
    def __init__(self, articles: dict[str, WikipediaArticle]) -> None:
        self._articles = {_normalized(key): article for key, article in articles.items()}
        self.queries: list[str] = []

    async def search(self, query: str, *, language: str | None = None, limit: int = 5) -> list[WikipediaSearchResult]:
        self.queries.append(query)
        normalized_query = _normalized(query)
        results = []
        preferred_languages = tuple(dict.fromkeys((language, "pl", "en"))) if language is not None else ("pl", "en")
        for key, article in self._articles.items():
            if article.language not in preferred_languages:
                continue
            normalized_title = _normalized(article.title)
            if key in normalized_query or normalized_query in key or normalized_title in normalized_query:
                results.append(
                    WikipediaSearchResult(
                        language=article.language,
                        title=article.title,
                        description=article.description,
                        page_url=article.page_url,
                    )
                )
        if not results and self._articles:
            for preferred_language in preferred_languages:
                for article in self._articles.values():
                    if article.language != preferred_language:
                        continue
                    results.append(
                        WikipediaSearchResult(
                            language=article.language,
                            title=article.title,
                            description=article.description,
                            page_url=article.page_url,
                        )
                    )
                    break
                if results:
                    break
        return results[:limit]

    async def summary(self, *, language: str, title: str) -> WikipediaArticle | None:
        for article in self._articles.values():
            if article.language == language and article.title == title:
                return article
        return None

    async def wikidata_facts(
        self,
        wikibase_item: str,
        *,
        property_ids: list[str] | None = None,
        limit: int = 24,
    ) -> dict[str, Any]:
        del property_ids, limit
        for article in self._articles.values():
            if article.wikibase_item == wikibase_item:
                return {
                    "id": wikibase_item,
                    "birth_year": article.birth_year,
                    "coordinates": article.coordinates,
                    "claims": [],
                }
        return {}

    async def wikidata_claims_by_property_query(
        self,
        wikibase_item: str,
        property_query: str,
        *,
        language: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        del language, limit
        for article in self._articles.values():
            if article.wikibase_item != wikibase_item:
                continue
            if "birth" in property_query.casefold() or "urod" in property_query.casefold():
                return {
                    "id": wikibase_item,
                    "property_query": property_query,
                    "property_candidates": [
                        {
                            "property_id": "P569",
                            "label": "date of birth",
                            "description": "date on which the subject was born",
                            "language": "en",
                            "url": "https://www.wikidata.org/wiki/Property:P569",
                        }
                    ],
                    "claims": [
                        {
                            "property_id": "P569",
                            "property": {
                                "property_id": "P569",
                                "label": "date of birth",
                                "description": "date on which the subject was born",
                                "language": "en",
                                "url": "https://www.wikidata.org/wiki/Property:P569",
                            },
                            "values": [
                                {
                                    "datatype": "time",
                                    "value": {
                                        "time": f"+{article.birth_year}-01-01T00:00:00Z" if article.birth_year is not None else "",
                                        "precision": 9,
                                        "calendar": "http://www.wikidata.org/entity/Q1985727",
                                    },
                                }
                            ],
                        }
                    ]
                    if article.birth_year is not None
                    else [],
                }
            return {"id": wikibase_item, "property_query": property_query, "property_candidates": [], "claims": []}
        return {}

    async def summary_for_query(self, query: str) -> WikipediaArticle:
        self.queries.append(query)
        article = self._articles.get(_normalized(query))
        if article is None:
            raise LookupError(query)
        return article

    async def close(self) -> None:
        pass


class FakeWeatherProvider:
    name = "fake_weather"

    def __init__(self, current: CurrentWeather) -> None:
        self._current = current

    async def get_weather_now(self, request: WeatherNowRequest) -> CurrentWeather | None:
        del request
        return self._current

    async def get_weather_forecast(self, request):
        del request
        return None

    async def close(self) -> None:
        pass


class FakeWeatherOllamaClient:
    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        raise AssertionError("unexpected weather LLM call")

    async def close(self) -> None:
        pass


class StaticAstronomyRefresher:
    def __init__(self, snapshot: AstronomySnapshot) -> None:
        self._snapshot = snapshot

    async def ensure_fresh(self) -> AstronomySnapshot:
        return self._snapshot

    async def close(self) -> None:
        pass


@dataclass(frozen=True)
class StaticHomeAssistantInventoryProvider:
    inventory: Any


def main() -> int:
    args = _parse_args()
    config = _load_yaml(args.config)
    cases = _load_cases(args.scenarios_dir)
    selected_cases = _select_cases(cases, args)
    if args.list:
        for case in selected_cases:
            print(case.name)
        return 0
    if not selected_cases:
        print("No cases selected.", file=sys.stderr)
        return 1

    settings = _settings(config, args)
    results = asyncio.run(
        _run_cases(
            selected_cases,
            settings,
            print_transcript=not args.no_transcript,
            verbose=args.verbose,
        )
    )
    _print_run_summary(results, settings)
    return 0 if all(result.passed for result in results) else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live orchestrator and DSA tests.")
    parser.add_argument("selectors", nargs="*", help="Optional selectors such as composite, composite/*, or composite/ha_time_combo.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Harness config YAML.")
    parser.add_argument("--scenarios-dir", type=Path, default=DEFAULT_SCENARIOS_DIR, help="Directory containing scenario YAML files.")
    parser.add_argument("--ollama-url", help="Override Ollama URL.")
    parser.add_argument("--orchestrator-model", help="Override orchestrator model.")
    parser.add_argument("--dsa-model", help="Override LLM-backed DSA model.")
    parser.add_argument("--group", action="append", default=[], help="Run a whole group. Can be repeated.")
    parser.add_argument("--select", action="append", default=[], help="Run a selector. Can be repeated.")
    parser.add_argument("--list", action="store_true", help="List selected cases and exit.")
    parser.add_argument("--verbose", action="store_true", help="Print detailed failures and observed values.")
    parser.add_argument("--no-transcript", action="store_true", help="Do not print message flow transcripts.")
    return parser.parse_args()


def _settings(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    models = _dict_or_empty(config.get("models"))
    defaults = _dict_or_empty(config.get("defaults"))
    return {
        "ollama_url": args.ollama_url or _str_or_default(config.get("ollama_url"), DEFAULT_OLLAMA_URL),
        "orchestrator_model": args.orchestrator_model or _str_or_default(models.get("orchestrator"), DEFAULT_MODEL),
        "dsa_model": args.dsa_model or _str_or_default(models.get("dsa"), DEFAULT_MODEL),
        "area": _str_or_default(defaults.get("area"), "office"),
        "user": _str_or_default(defaults.get("user"), "Maciek"),
        "users": _dict_or_empty(config.get("users")),
        "timezone": _str_or_default(defaults.get("timezone"), "Europe/Warsaw"),
        "location": _str_or_default(defaults.get("location"), "Wrocław"),
        "fixed_utc": _parse_datetime(_str_or_default(defaults.get("fixed_utc"), "2026-05-30T08:46:00+00:00")),
    }


def _load_cases(scenarios_dir: Path) -> list[TestCase]:
    cases: list[TestCase] = []
    for path in sorted(scenarios_dir.glob("*.yaml")):
        config = _load_yaml(path)
        group = _required_string(config, "group", str(path))
        kind = _str_or_default(config.get("type"), group)
        raw_cases = config.get("cases")
        if not isinstance(raw_cases, list):
            raise ValueError(f"{path}: cases must be a list")
        for index, raw_case in enumerate(raw_cases, start=1):
            if not isinstance(raw_case, dict):
                raise ValueError(f"{path}: case #{index} must be a mapping")
            case_id = _required_string(raw_case, "id", f"{path}: case #{index}")
            cases.append(TestCase(group=group, case_id=case_id, kind=kind, raw=raw_case, path=path))
    return cases


def _select_cases(cases: list[TestCase], args: argparse.Namespace) -> list[TestCase]:
    selectors = [f"{group}/*" for group in args.group]
    selectors.extend(args.select)
    selectors.extend(args.selectors)
    if not selectors:
        return cases

    selected: list[TestCase] = []
    missing = []
    for selector in selectors:
        matches = [case for case in cases if _selector_matches(selector, case)]
        if not matches:
            missing.append(selector)
            continue
        for match in matches:
            if match not in selected:
                selected.append(match)
    if missing:
        raise ValueError(f"selector(s) matched no cases: {', '.join(missing)}")
    return selected


def _selector_matches(selector: str, case: TestCase) -> bool:
    if "/" not in selector and "*" not in selector:
        return selector == case.group
    if selector.endswith("/*") and selector[:-2] == case.group:
        return True
    return fnmatch.fnmatchcase(case.name, selector)


async def _run_cases(
    cases: list[TestCase],
    settings: dict[str, Any],
    *,
    print_transcript: bool,
    verbose: bool,
) -> list[CaseResult]:
    results = []
    for index, case in enumerate(cases, start=1):
        _print_case_start(case, index, len(cases))
        result = await _run_case(case, settings)
        results.append(result)
        if print_transcript:
            _print_transcript(result)
        _print_case_result(result, verbose=verbose)
    return results


async def _run_case(case: TestCase, settings: dict[str, Any]) -> CaseResult:
    if case.kind == "orchestrator":
        return await _run_orchestrator_case(case, settings)
    if case.kind == "dsa_ha":
        return await _run_dsa_ha_case(case, settings)
    if case.kind == "dsa_media":
        return await _run_dsa_media_case(case, settings)
    if case.kind == "dsa_time":
        return await _run_dsa_time_case(case, settings)
    if case.kind == "dsa_wikipedia":
        return await _run_dsa_wikipedia_case(case, settings)
    if case.kind == "dsa_weather":
        return await _run_dsa_weather_case(case, settings)
    if case.kind == "composite":
        return await _run_composite_case(case, settings)
    return CaseResult(case=case, failures=[f"unsupported case type: {case.kind}"])


async def _run_orchestrator_case(case: TestCase, settings: dict[str, Any]) -> CaseResult:
    started_at = time.perf_counter()
    raw = case.raw
    task_traces: list[dict[str, Any]] = []
    domain_results = _dict_or_empty(raw.get("domain_results"))
    domain_agents = {
        domain: StubDomainAgent(domain, results, task_traces)
        for domain, results in domain_results.items()
        if isinstance(results, dict)
    }
    ollama = RecordingOllamaClient(base_url=settings["ollama_url"])
    agent = OrchestratorAgent(
        orchestrator_model=settings["orchestrator_model"],
        domain_agents=domain_agents,
        ollama_client=ollama,
        owns_ollama_client=True,
        home_assistant_inventory_provider=StaticHomeAssistantInventoryProvider(_home_assistant_inventory()),
    )
    result = CaseResult(case=case)
    try:
        await _run_agent_conversation(agent, raw, settings, result)
    finally:
        await agent.close()
    _collect_orchestrator_observations(result, ollama, task_traces)
    result.duration_seconds = time.perf_counter() - started_at
    _score_orchestrator_expectations(result)
    _score_reply_expectations(result)
    return result


async def _run_composite_case(case: TestCase, settings: dict[str, Any]) -> CaseResult:
    started_at = time.perf_counter()
    raw = case.raw
    inventory = _home_assistant_inventory()
    fake_ha = agent_tool_eval.FakeHomeAssistantConnection(
        inventory,
        _parse_expected_calls(raw),
        transcript=False,
    )
    temp_dir = tempfile.TemporaryDirectory(prefix="orchestrator-dsa-time-")
    task_traces: list[dict[str, Any]] = []
    agents = _composite_domain_agents(raw, settings, fake_ha, Path(temp_dir.name), task_traces)
    ollama = RecordingOllamaClient(base_url=settings["ollama_url"])
    agent = OrchestratorAgent(
        orchestrator_model=settings["orchestrator_model"],
        domain_agents=agents,
        ollama_client=ollama,
        owns_ollama_client=True,
        home_assistant_inventory_provider=fake_ha,
    )
    result = CaseResult(case=case)
    try:
        await _run_agent_conversation(agent, raw, settings, result)
    finally:
        await agent.close()
        temp_dir.cleanup()
    _collect_orchestrator_observations(result, ollama, task_traces)
    result.actual_calls = list(fake_ha.calls)
    result.duration_seconds = time.perf_counter() - started_at
    _score_orchestrator_expectations(result)
    _score_reply_expectations(result)
    _score_ha_expectations(result, inventory)
    return result


async def _run_dsa_ha_case(case: TestCase, settings: dict[str, Any]) -> CaseResult:
    started_at = time.perf_counter()
    raw = case.raw
    inventory = _home_assistant_inventory()
    fake_ha = agent_tool_eval.FakeHomeAssistantConnection(
        inventory,
        _parse_expected_calls(raw),
        transcript=False,
    )
    dsa = HomeAssistantDomainAgent(
        model=settings["dsa_model"],
        ollama_url=settings["ollama_url"],
        connection=fake_ha,
    )
    result = CaseResult(case=case)
    try:
        dsa_result = await dsa.run_task(_conversation(raw, settings, f"dsa-ha-{case.name}"), _task(raw), _dict_or_empty(raw.get("active_context")))
    finally:
        await dsa.close()
    result.duration_seconds = time.perf_counter() - started_at
    result.task_results = [{"task": _task(raw), "result": dsa_result}]
    result.replies = [_str_or_default(dsa_result.get("text"), "")]
    result.actual_calls = list(fake_ha.calls)
    _score_result_match(result, raw.get("expected_result"), dsa_result, "DSA result")
    _score_reply_expectations(result)
    _score_ha_expectations(result, inventory)
    return result


async def _run_dsa_media_case(case: TestCase, settings: dict[str, Any]) -> CaseResult:
    started_at = time.perf_counter()
    raw = case.raw
    inventory = _home_assistant_inventory()
    fake_ha = agent_tool_eval.FakeHomeAssistantConnection(
        inventory,
        _parse_expected_calls(raw),
        transcript=False,
    )
    dsa = MediaPlayerDomainAgent(
        model=settings["dsa_model"],
        connection=fake_ha,
        ollama_client=FakeWeatherOllamaClient(),
    )
    result = CaseResult(case=case)
    try:
        dsa_result = await dsa.run_task(_conversation(raw, settings, f"dsa-media-{case.name}"), _task(raw), _dict_or_empty(raw.get("active_context")))
    finally:
        await dsa.close()
    result.duration_seconds = time.perf_counter() - started_at
    result.task_results = [{"task": _task(raw), "result": dsa_result}]
    result.replies = [_str_or_default(dsa_result.get("text"), "")]
    result.actual_calls = list(fake_ha.calls)
    _score_result_match(result, raw.get("expected_result"), dsa_result, "DSA result")
    _score_reply_expectations(result)
    _score_ha_expectations(result, inventory)
    return result


async def _run_dsa_time_case(case: TestCase, settings: dict[str, Any]) -> CaseResult:
    started_at = time.perf_counter()
    raw = case.raw
    resolver = FakeTimezoneResolver(_dict_or_empty(raw.get("timezone_map")) or _default_timezone_map())
    fixed_utc = settings["fixed_utc"]
    temp_dir = tempfile.TemporaryDirectory(prefix="orchestrator-dsa-time-")
    agent = CurrentTimeDomainAgent(
        timezone=settings["timezone"],
        location=settings["location"],
        cache_dir=Path(temp_dir.name),
        now_factory=lambda zone: fixed_utc.astimezone(zone),
        timezone_resolver=resolver,
    )
    result = CaseResult(case=case)
    try:
        dsa_result = await agent.run_task(_conversation(raw, settings, f"dsa-time-{case.name}"), _task(raw), {})
    finally:
        await agent.close()
        temp_dir.cleanup()
    result.duration_seconds = time.perf_counter() - started_at
    result.task_results = [{"task": _task(raw), "result": dsa_result}]
    result.replies = [_str_or_default(dsa_result.get("text"), "")]
    _score_result_match(result, raw.get("expected_result"), dsa_result, "DSA result")
    _score_reply_expectations(result)
    return result


async def _run_dsa_wikipedia_case(case: TestCase, settings: dict[str, Any]) -> CaseResult:
    started_at = time.perf_counter()
    raw = case.raw
    client = FakeWikipediaClient(_wikipedia_articles(raw))
    agent = WikipediaDomainAgent(
        model=settings["dsa_model"],
        ollama_url=settings["ollama_url"],
        client=client,
    )
    result = CaseResult(case=case)
    try:
        dsa_result = await agent.run_task(_conversation(raw, settings, f"dsa-wikipedia-{case.name}"), _task(raw), {})
    finally:
        await agent.close()
    result.duration_seconds = time.perf_counter() - started_at
    result.task_results = [{"task": _task(raw), "result": dsa_result, "queries": list(client.queries)}]
    result.replies = [_str_or_default(dsa_result.get("text"), "")]
    _score_result_match(result, raw.get("expected_result"), dsa_result, "DSA result")
    _score_reply_expectations(result)
    return result


async def _run_dsa_weather_case(case: TestCase, settings: dict[str, Any]) -> CaseResult:
    started_at = time.perf_counter()
    raw = case.raw
    current = CurrentWeather(
        location=settings["location"],
        provider="fake_weather",
        observed_at=settings["fixed_utc"].astimezone(ZoneInfo(settings["timezone"])),
        station_name=settings["location"],
        temperature_c=15.7,
        humidity_percent=90.0,
        pressure_hpa=1012.3,
        wind_speed_kmh=10.8,
        wind_direction_deg=270,
        precipitation_mm=1.4,
    )
    agent = WeatherDomainAgent(
        model=settings["dsa_model"],
        ollama_url=settings["ollama_url"],
        location=settings["location"],
        cache_dir=Path(tempfile.gettempdir()),
        providers=[FakeWeatherProvider(current)],
        astronomy_refresher=StaticAstronomyRefresher(_astronomy_snapshot(settings)),
    )
    result = CaseResult(case=case)
    try:
        dsa_result = await agent.run_task(_conversation(raw, settings, f"dsa-weather-{case.name}"), _task(raw), {})
    finally:
        await agent.close()
    result.duration_seconds = time.perf_counter() - started_at
    result.task_results = [{"task": _task(raw), "result": dsa_result}]
    result.replies = [_str_or_default(dsa_result.get("text"), "")]
    _score_result_match(result, raw.get("expected_result"), dsa_result, "DSA result")
    _score_reply_expectations(result)
    return result


async def _run_agent_conversation(agent: OrchestratorAgent, raw: dict[str, Any], settings: dict[str, Any], result: CaseResult) -> None:
    messages = _required_string_list(raw, "messages", result.case.name)
    endpoint = FakeAgentChannel([UserMessage(text=message) for message in messages])
    conversation = _conversation(raw, settings, f"case-{result.case.name}")
    await agent.run_agent_conversation(conversation.conversation, endpoint)
    result.replies = _sent_text_messages(endpoint.sent)


def _composite_domain_agents(
    raw: dict[str, Any],
    settings: dict[str, Any],
    fake_ha: Any,
    cache_dir: Path,
    traces: list[dict[str, Any]],
) -> dict[str, Any]:
    fixed_utc = settings["fixed_utc"]
    agents: dict[str, Any] = {
        "home_assistant": HomeAssistantDomainAgent(
            model=settings["dsa_model"],
            ollama_url=settings["ollama_url"],
            connection=fake_ha,
        ),
        "media_player": MediaPlayerDomainAgent(
            model=settings["dsa_model"],
            connection=fake_ha,
            ollama_client=FakeWeatherOllamaClient(),
        ),
        "time": CurrentTimeDomainAgent(
            timezone=settings["timezone"],
            location=settings["location"],
            cache_dir=cache_dir,
            now_factory=lambda zone: fixed_utc.astimezone(zone),
            timezone_resolver=FakeTimezoneResolver(_dict_or_empty(raw.get("timezone_map")) or _default_timezone_map()),
        ),
        "wikipedia": WikipediaDomainAgent(
            model=settings["dsa_model"],
            ollama_url=settings["ollama_url"],
            client=FakeWikipediaClient(_wikipedia_articles(raw)),
        ),
    }
    return {domain: TracingDomainAgent(domain, agent, traces) for domain, agent in agents.items()}


def _collect_orchestrator_observations(result: CaseResult, ollama: RecordingOllamaClient, task_traces: list[dict[str, Any]]) -> None:
    result.model_requests = ollama.requests
    result.model_responses = ollama.responses
    result.task_results = task_traces
    result.tasks = [trace["task"] for trace in task_traces]
    result.planning_contexts = _planning_contexts(ollama.requests)


def _score_orchestrator_expectations(result: CaseResult) -> None:
    expected_tasks = _list_or_empty(result.case.raw.get("expected_tasks"))
    if len(result.tasks) < len(expected_tasks):
        result.failures.append(f"expected at least {len(expected_tasks)} task(s), got {len(result.tasks)}")
    for index, expected_task in enumerate(expected_tasks):
        if index >= len(result.tasks):
            break
        if not _partial_match(expected_task, result.tasks[index]):
            result.failures.append(f"task #{index + 1} mismatch expected={expected_task!r} actual={result.tasks[index]!r}")

    for expected_context in _list_or_empty(result.case.raw.get("expected_planning_context")):
        message_index = expected_context.get("message_index") if isinstance(expected_context, dict) else None
        if not isinstance(message_index, int) or message_index >= len(result.planning_contexts):
            result.failures.append(f"missing planning context for message index {message_index!r}")
            continue
        context = result.planning_contexts[message_index]
        salient_entities = context.get("salient_entities", [])
        for expected_entity in _string_list(expected_context.get("salient_entities", [])):
            if expected_entity not in salient_entities:
                result.failures.append(f"message index {message_index} missing salient entity {expected_entity!r}")


def _score_reply_expectations(result: CaseResult) -> None:
    expectations = result.case.raw.get("expected_replies")
    if expectations is None:
        confirmation = result.case.raw.get("confirmation")
        expectations = [confirmation] if isinstance(confirmation, dict) else []
    expectations = _list_or_empty(expectations)
    if not expectations:
        return
    if len(result.replies) < len(expectations):
        result.failures.append(f"expected at least {len(expectations)} replie(s), got {len(result.replies)}")
    for index, expectation in enumerate(expectations):
        if index >= len(result.replies):
            break
        reply = result.replies[index]
        if isinstance(expectation, str):
            if reply != expectation:
                result.failures.append(f"reply #{index + 1} expected {expectation!r}, got {reply!r}")
            continue
        if isinstance(expectation, dict):
            exact = expectation.get("exact")
            if isinstance(exact, str) and reply != exact:
                result.failures.append(f"reply #{index + 1} expected {exact!r}, got {reply!r}")
            contains_all = _string_list(expectation.get("contains_all", []))
            normalized_reply = _normalized(reply)
            for expected_text in contains_all:
                if _normalized(expected_text) not in normalized_reply:
                    result.failures.append(f"reply #{index + 1} does not contain {expected_text!r}: {reply!r}")


def _score_ha_expectations(result: CaseResult, inventory: Any) -> None:
    expected_calls = _parse_expected_calls(result.case.raw)
    expected_effects = _parse_expected_effects(result.case.raw)
    if not expected_calls and not expected_effects:
        return
    scenario = agent_tool_eval.Scenario(
        name=result.case.name,
        messages=tuple(_required_string_list(result.case.raw, "messages", result.case.name)) if "messages" in result.case.raw else ("",),
        expected_calls=expected_calls,
        expected_effects=expected_effects,
        reply_expectations=(),
        area=_str_or_default(result.case.raw.get("area"), None),
        strict=bool(result.case.raw.get("strict", False)),
    )
    ha_result = agent_tool_eval.ScenarioResult(scenario=scenario)
    ha_result.actual_calls.extend(result.actual_calls)
    agent_tool_eval._score_expected_effects(ha_result, inventory)

    actual_index = 0
    matched_actual_indexes = set()
    for expected_index, expected in enumerate(expected_calls):
        match_index = agent_tool_eval._find_matching_call(expected, ha_result.actual_calls, actual_index, inventory)
        if match_index is None:
            message = f"missing expected HA call #{expected_index + 1}: {expected.tool} {json.dumps(expected.arguments, ensure_ascii=False)}"
            if expected.optional:
                result.warnings.append(f"optional {message}")
            else:
                result.failures.append(message)
            continue
        matched_actual_indexes.add(match_index)
        actual_index = match_index + 1

    if scenario.strict:
        for index, actual in enumerate(ha_result.actual_calls):
            if index not in matched_actual_indexes:
                result.failures.append(f"unexpected HA call #{index + 1}: {actual.tool} {json.dumps(actual.arguments, ensure_ascii=False)}")
    result.failures.extend(ha_result.failures)
    result.warnings.extend(ha_result.warnings)


def _score_result_match(result: CaseResult, expected: Any, actual: dict[str, Any], label: str) -> None:
    if expected is not None and not _partial_match(expected, actual):
        result.failures.append(f"{label} mismatch expected={expected!r} actual={actual!r}")


def _partial_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        if set(expected) == {"$any"} and isinstance(expected["$any"], list):
            return any(_partial_match(candidate, actual) for candidate in expected["$any"])
        if not isinstance(actual, dict):
            return False
        for key, expected_value in expected.items():
            if key not in actual or not _partial_match(expected_value, actual[key]):
                return False
        return True
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) < len(expected):
            return False
        return all(_partial_match(expected_item, actual[index]) for index, expected_item in enumerate(expected))
    return expected == actual


def _conversation(
    raw: dict[str, Any],
    settings: dict[str, Any],
    conversation_id: str,
) -> AgentExecutionContext:
    area = _str_or_default(raw.get("area"), settings["area"])
    user = _str_or_default(raw.get("user"), settings["user"])
    medium = _str_or_default(raw.get("medium"), "voice")
    return AgentExecutionContext(
        conversation=ConversationContext(
            conversation_id=conversation_id,
            input_session_id=f"behavior-{conversation_id}",
            medium=ConversationMedium(medium),
            area=area,
            user=user,
            user_settings=_user_settings_for(
                user,
                _dict_or_empty(settings.get("users")),
            ),
        ),
    )


def _task(raw: dict[str, Any]) -> dict[str, Any]:
    task = raw.get("task")
    if not isinstance(task, dict):
        raise ValueError("case.task must be a mapping")
    return copy.deepcopy(task)


def _user_settings_for(user: str, users: dict[str, Any]) -> dict[str, Any]:
    settings = users.get(user)
    if settings is None:
        normalized_user = user.casefold()
        for candidate_user, candidate_settings in users.items():
            if isinstance(candidate_user, str) and candidate_user.casefold() == normalized_user:
                settings = candidate_settings
                break
    return copy.deepcopy(settings) if isinstance(settings, dict) else {}


def _home_assistant_inventory() -> Any:
    return agent_tool_eval._build_inventory({})


def _astronomy_snapshot(settings: dict[str, Any]) -> AstronomySnapshot:
    return AstronomySnapshot(
        location=settings["location"],
        last_pull_date=settings["fixed_utc"].isoformat().replace("+00:00", "Z"),
        records={
            "today": AstronomyRecord(
                date=settings["fixed_utc"].date().isoformat(),
                sunrise="04:40",
                sunset="21:10",
                moonrise="22:53",
                moonset="07:59",
                moon_phase="FULL_MOON",
                day_length="16:30",
            ),
            "june_solstice": AstronomyRecord(
                date=f"{settings['fixed_utc'].year}-06-21",
                sunrise="04:36",
                sunset="21:12",
                moonrise="13:30",
                moonset="00:18",
                moon_phase="WAXING_GIBBOUS",
                day_length="16:36",
            ),
            "december_solstice": AstronomyRecord(
                date=f"{settings['fixed_utc'].year}-12-21",
                sunrise="07:54",
                sunset="15:44",
                moonrise="12:01",
                moonset="03:33",
                moon_phase="FIRST_QUARTER",
                day_length="07:50",
            ),
        },
    )


def _parse_expected_calls(raw: dict[str, Any]) -> tuple[Any, ...]:
    return agent_tool_eval._parse_expected_calls(raw.get("expected_calls", []), _str_or_default(raw.get("id"), "case"))


def _parse_expected_effects(raw: dict[str, Any]) -> tuple[Any, ...]:
    return agent_tool_eval._parse_expected_effects(raw.get("expected_effects", []), _str_or_default(raw.get("id"), "case"))


def _wikipedia_articles(raw: dict[str, Any]) -> dict[str, WikipediaArticle]:
    raw_articles = raw.get("wikipedia_articles", _default_wikipedia_articles())
    if not isinstance(raw_articles, dict):
        raise ValueError("wikipedia_articles must be a mapping")
    articles = {}
    for key, value in raw_articles.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise ValueError("wikipedia article fixtures must be mappings keyed by query")
        articles[key] = WikipediaArticle(
            language=_str_or_default(value.get("language"), "pl"),
            title=_str_or_default(value.get("title"), key),
            extract=_str_or_default(value.get("extract"), ""),
            description=value.get("description") if isinstance(value.get("description"), str) else None,
            page_url=value.get("page_url") if isinstance(value.get("page_url"), str) else None,
            birth_year=value.get("birth_year") if isinstance(value.get("birth_year"), int) else None,
            coordinates=value.get("coordinates") if isinstance(value.get("coordinates"), dict) else None,
        )
    return articles


def _default_wikipedia_articles() -> dict[str, dict[str, Any]]:
    return {
        "Albert Einstein": {
            "language": "pl",
            "title": "Albert Einstein",
            "extract": "Albert Einstein (ur. 14 marca 1879, zm. 18 kwietnia 1955) był fizykiem teoretykiem.",
            "wikibase_item": "Q937",
            "birth_year": 1879,
        },
        "Jacksonville": {
            "language": "en",
            "title": "Jacksonville, Florida",
            "extract": "Jacksonville is the most populous city proper in the U.S. state of Florida.",
            "wikibase_item": "Q16568",
            "coordinates": {"lat": 30.3322, "lon": -81.6557},
        },
        "John Henry": {
            "language": "en",
            "title": "John Henry (folklore)",
            "extract": "John Henry is an American folk hero. He is said to have worked as a steel-driving man. His story is told in many songs.",
        },
    }


def _default_timezone_map() -> dict[str, str]:
    return {
        "Jacksonville": "America/New_York",
        "Florida": "America/New_York",
        "Floryda": "America/New_York",
        "Wrocław": "Europe/Warsaw",
    }


def _sent_text_messages(events: list[Any]) -> list[str]:
    messages = []
    current: list[str] = []
    for event in events:
        if isinstance(event, AssistantMessageStarted):
            current = []
        elif isinstance(event, AssistantTextChunk):
            current.append(event.text)
        elif isinstance(event, AssistantMessageCompleted):
            messages.append("".join(current))
    return messages


def _planning_contexts(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contexts = []
    for request in requests:
        if request.get("format") != "json":
            continue
        payload = _request_payload(request)
        if payload:
            contexts.append(_dict_or_empty(payload.get("active_context")))
    return contexts


def _request_payload(request: dict[str, Any] | None) -> dict[str, Any]:
    if request is None:
        return {}
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        return {}
    last_message = messages[-1]
    if not isinstance(last_message, dict):
        return {}
    content = last_message.get("content")
    if not isinstance(content, str):
        return {}
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _response_content(response: dict[str, Any] | None) -> str:
    if response is None:
        return ""
    message = response.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _response_content_as_json(response: dict[str, Any] | None) -> Any:
    content = _response_content(response)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return content


def _print_case_start(case: TestCase, index: int, total: int) -> None:
    print(f"[RUN ] {index}/{total} {case.name}", flush=True)


def _print_transcript(result: CaseResult) -> None:
    print(f"=== case: {result.case.name} ===", flush=True)
    messages = _required_string_list(result.case.raw, "messages", result.case.name) if "messages" in result.case.raw else []
    if messages:
        for message_index, user_message in enumerate(messages):
            print(f"> user: {user_message}", flush=True)
            plan_request, plan_response = _model_turn(result, message_index * 2)
            final_request, final_response = _model_turn(result, message_index * 2 + 1)
            final_payload = _request_payload(final_request)
            if plan_request is not None:
                print("< orchestrator context:", flush=True)
                print(_format_json(_dict_or_empty(_request_payload(plan_request).get("active_context"))), flush=True)
            if plan_response is not None:
                effective_plan = final_payload.get("plan", _response_content_as_json(plan_response))
                print("< orchestrator plan:", flush=True)
                print(_format_json(effective_plan), flush=True)
            if final_request is not None:
                print("< DSA results:", flush=True)
                print(_format_json(final_payload.get("task_results", [])), flush=True)
            reply = result.replies[message_index] if message_index < len(result.replies) else _response_content(final_response)
            if reply:
                print(f"< reply: {reply}", flush=True)
    else:
        if result.task_results:
            print("< DSA result:", flush=True)
            print(_format_json(result.task_results[0].get("result", result.task_results[0])), flush=True)
        for reply in result.replies:
            print(f"< reply: {reply}", flush=True)
    if result.actual_calls:
        print("< service calls:", flush=True)
        print(_format_json([call.__dict__ for call in result.actual_calls]), flush=True)
    print(flush=True)


def _model_turn(result: CaseResult, index: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    request = result.model_requests[index] if index < len(result.model_requests) else None
    response = result.model_responses[index] if index < len(result.model_responses) else None
    return request, response


def _print_case_result(result: CaseResult, *, verbose: bool) -> None:
    status = "PASS" if result.passed else "FAIL"
    print(
        f"[{status}] {result.case.name} duration={result.duration_seconds:.2f}s "
        f"tasks={len(result.tasks)} calls={len(result.actual_calls)}",
        flush=True,
    )
    if result.failures or verbose:
        for failure in result.failures:
            print(f"  - {failure}", flush=True)
        for warning in result.warnings:
            print(f"  warning: {warning}", flush=True)
        if verbose:
            print(f"  replies={result.replies!r}", flush=True)
            print(f"  tasks={result.tasks!r}", flush=True)
    print(flush=True)


def _print_run_summary(results: list[CaseResult], settings: dict[str, Any]) -> None:
    passed = sum(1 for result in results if result.passed)
    duration = sum(result.duration_seconds for result in results)
    status = "PASS" if passed == len(results) else "FAIL"
    print("Summary", flush=True)
    print(f"  orchestrator_model: {settings['orchestrator_model']}", flush=True)
    print(f"  dsa_model: {settings['dsa_model']}", flush=True)
    print(f"  ollama_url: {settings['ollama_url']}", flush=True)
    print(f"  result: {status} {passed}/{len(results)}", flush=True)
    print(f"  total_duration: {duration:.2f}s", flush=True)
    print("  tests:", flush=True)
    for result in results:
        case_status = "PASS" if result.passed else "FAIL"
        print(f"    {case_status} {result.case.name} {result.duration_seconds:.2f}s", flush=True)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as config_file:
        value = yaml.safe_load(config_file)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def _required_string(raw_mapping: dict[str, Any], key: str, context: str) -> str:
    value = raw_mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value


def _required_string_list(raw_mapping: dict[str, Any], key: str, context: str) -> list[str]:
    value = raw_mapping.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{context}.{key} must be a list of non-empty strings")
    return value


def _string_list(value: Any) -> list[str]:
    return value if isinstance(value, list) and all(isinstance(item, str) for item in value) else []


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str_or_default(value: Any, default: str | None) -> str:
    return value if isinstance(value, str) and value else (default or "")


def _parse_datetime(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed


def _format_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _normalized(value: str) -> str:
    value = value.replace("Ł", "L").replace("ł", "l")
    without_marks = "".join(
        character
        for character in unicodedata.normalize("NFKD", value.strip().casefold())
        if not unicodedata.combining(character)
    )
    return " ".join(without_marks.split())


if __name__ == "__main__":
    raise SystemExit(main())
