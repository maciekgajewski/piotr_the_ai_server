import asyncio
import json
from typing import Any

from ai_server.agent_loop import AgentReply
from ai_server.domain_agents.wikipedia import (
    DEFAULT_LANGUAGES,
    WikipediaArticle,
    WikipediaClient,
    WikipediaDomainAgent,
    WikipediaDomainToolSet,
    WikipediaSearchResult,
    _parse_domain_reply,
)
from ai_server.interfaces import Conversation


def test_wikipedia_domain_agent_runs_agent_loop_and_parses_json_reply() -> None:
    client = FakeWikipediaClient()
    loop_factory = FakeLoopFactory(
        json.dumps(
            {
                "status": "ok",
                "text": "Albert Einstein urodził się w 1879 roku.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": ["wikipedia.pl.Albert Einstein"],
                "title": "Albert Einstein",
                "language": "pl",
                "url": "https://pl.wikipedia.org/wiki/Albert_Einstein",
            },
            ensure_ascii=False,
        )
    )
    agent = WikipediaDomainAgent(
        model="qwen3:4b",
        ollama_url="http://ollama:11434",
        fallback_model="qwen3:4b-fallback",
        fallback_backoff_seconds=120,
        client=client,
        loop_factory=loop_factory.factory,
        ollama_connection=FakeOllamaConnection(),
    )
    conversation = Conversation(conversation_id="conversation-1", attributes={"medium": "voice"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "wikipedia", "command": {"topic": "Albert Einstein", "fact": "birth year"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["text"] == "Albert Einstein urodził się w 1879 roku."
    assert result["entities"] == ["wikipedia.pl.Albert Einstein"]
    assert result["final_reply_mode"] == "verbatim"
    assert loop_factory.config.model == "qwen3:4b"
    assert loop_factory.config.ollama_url == "http://ollama:11434"
    assert loop_factory.config.fallback_model == "qwen3:4b-fallback"
    assert loop_factory.config.fallback_backoff_seconds == 120
    assert isinstance(loop_factory.tools, WikipediaDomainToolSet)
    payload = json.loads(loop_factory.loop.user_message)
    assert payload["task"]["command"]["topic"] == "Albert Einstein"


def test_wikipedia_domain_agent_preserves_source_title_terms_in_reply_text() -> None:
    result = _parse_domain_reply(
        json.dumps(
            {
                "status": "ok",
                "text": "Jacksonville znajduje się w stanie Floroda.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": ["wikipedia.en.Jacksonville, Florida"],
                "title": "Jacksonville, Florida",
            },
            ensure_ascii=False,
        )
    )

    assert result["text"].endswith("Źródło: Jacksonville, Florida.")


def test_wikipedia_domain_agent_rejects_non_json_agent_reply() -> None:
    agent = WikipediaDomainAgent(
        model="qwen3:4b",
        ollama_url="http://ollama:11434",
        client=FakeWikipediaClient(),
        loop_factory=FakeLoopFactory("to nie jest json").factory,
        ollama_connection=FakeOllamaConnection(),
    )
    conversation = Conversation(conversation_id="conversation-1", attributes={"medium": "voice"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "wikipedia", "command": {"topic": "Albert Einstein"}},
            {},
        )
    )

    assert result["status"] == "failed"
    assert result["text"] == "Nie mogę teraz przygotować odpowiedzi z Wikipedii."


def test_wikipedia_toolset_search_summary_and_wikidata_facts() -> None:
    article = WikipediaArticle(
        language="pl",
        title="Albert Einstein",
        extract="Albert Einstein (ur. 14 marca 1879, zm. 18 kwietnia 1955) był fizykiem teoretykiem.",
        description="fizyk teoretyk",
        page_url="https://pl.wikipedia.org/wiki/Albert_Einstein",
        wikibase_item="Q937",
        birth_year=1879,
    )
    client = FakeWikipediaClient(article)
    toolset = WikipediaDomainToolSet(client)

    search = asyncio.run(toolset.search_wikipedia("Einstein", limit=3))
    summary = asyncio.run(toolset.get_wikipedia_summary("Albert Einstein", "pl"))
    facts = asyncio.run(toolset.get_wikidata_facts("Q937", property_ids=["P569"]))

    assert search["status"] == "ok"
    assert search["results"][0]["title"] == "Albert Einstein"
    assert summary["status"] == "ok"
    assert summary["article"]["wikibase_item"] == "Q937"
    assert facts["status"] == "ok"
    assert facts["facts"]["birth_year"] == 1879
    assert client.calls == [
        ("search", {"query": "Einstein", "language": None, "limit": 3}),
        ("summary", {"title": "Albert Einstein", "language": "pl"}),
        ("wikidata_facts", {"wikibase_item": "Q937", "property_ids": ["P569"], "limit": 24}),
    ]


def test_wikipedia_toolset_finds_claims_by_property_query_after_summary() -> None:
    article = WikipediaArticle(
        language="pl",
        title="Szwajcaria",
        extract="Szwajcaria jest państwem w Europie.",
        description="państwo w Europie",
        page_url="https://pl.wikipedia.org/wiki/Szwajcaria",
        wikibase_item="Q39",
    )
    client = FakeWikipediaClient(article)
    toolset = WikipediaDomainToolSet(client)

    before_summary = asyncio.run(toolset.find_wikidata_claims("Q39", "GDP"))
    summary = asyncio.run(toolset.get_wikipedia_summary("Szwajcaria", "pl"))
    claims = asyncio.run(toolset.find_wikidata_claims("Q39", "GDP", language="en", limit=5))

    assert before_summary["status"] == "needs_summary"
    assert summary["status"] == "ok"
    assert claims["status"] == "ok"
    assert claims["result"]["property_candidates"][0]["property_id"] == "P2131"
    assert claims["result"]["claims"][0]["property"]["label"] == "nominal GDP"
    assert claims["result"]["claims"][0]["values"][0]["value"] == {
        "amount": "+824734000000",
        "unit": "http://www.wikidata.org/entity/Q4917",
    }
    assert client.calls == [
        ("summary", {"title": "Szwajcaria", "language": "pl"}),
        ("wikidata_claims_by_property_query", {"wikibase_item": "Q39", "property_query": "GDP", "language": "en", "limit": 5}),
    ]


def test_wikipedia_client_resolves_property_query_against_item_claims() -> None:
    client = FakeFetchWikipediaClient(
        {
            "https://www.wikidata.org/wiki/Special:EntityData/Q39.json": {
                "entities": {
                    "Q39": {
                        "labels": {"en": {"value": "Switzerland"}},
                        "descriptions": {},
                        "aliases": {},
                        "claims": {
                            "P2131": [
                                {
                                    "rank": "normal",
                                    "mainsnak": {
                                        "datatype": "quantity",
                                        "datavalue": {
                                            "value": {
                                                "amount": "+824734000000",
                                                "unit": "http://www.wikidata.org/entity/Q4917",
                                            }
                                        },
                                    },
                                    "qualifiers": {
                                        "P585": [
                                            {
                                                "datatype": "time",
                                                "datavalue": {
                                                    "value": {
                                                        "time": "+2024-01-01T00:00:00Z",
                                                        "precision": 9,
                                                        "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
                                                    }
                                                },
                                            }
                                        ]
                                    },
                                }
                            ]
                        },
                    }
                }
            },
            "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=GDP&language=en&type=property&format=json&limit=1": {
                "search": [
                    {
                        "id": "P2131",
                        "label": "nominal GDP",
                        "description": "nominal gross domestic product",
                        "concepturi": "https://www.wikidata.org/wiki/Property:P2131",
                    }
                ]
            },
        }
    )

    result = asyncio.run(client.wikidata_claims_by_property_query("Q39", "GDP", language="en", limit=1))

    assert result["property_candidates"] == [
        {
            "property_id": "P2131",
            "label": "nominal GDP",
            "description": "nominal gross domestic product",
            "language": "en",
            "url": "https://www.wikidata.org/wiki/Property:P2131",
        }
    ]
    claim = result["claims"][0]
    assert claim["property_id"] == "P2131"
    assert claim["property"]["label"] == "nominal GDP"
    assert claim["values"][0]["value"]["amount"] == "+824734000000"
    assert claim["values"][0]["qualifiers"]["P585"][0]["value"]["time"] == "+2024-01-01T00:00:00Z"


def test_wikipedia_domain_agent_asks_for_missing_topic_from_model() -> None:
    agent = WikipediaDomainAgent(
        model="qwen3:4b",
        ollama_url="http://ollama:11434",
        client=FakeWikipediaClient(),
        loop_factory=FakeLoopFactory(
            json.dumps(
                {
                    "status": "needs_clarification",
                    "text": "Czego mam poszukać w Wikipedii?",
                    "needs_clarification": True,
                    "clarification_question": "Czego mam poszukać w Wikipedii?",
                    "entities": [],
                },
                ensure_ascii=False,
            )
        ).factory,
        ollama_connection=FakeOllamaConnection(),
    )
    conversation = Conversation(conversation_id="conversation-1", attributes={"medium": "voice"})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "wikipedia", "command": {}},
            {},
        )
    )

    assert result["status"] == "needs_clarification"
    assert result["clarification_question"] == "Czego mam poszukać w Wikipedii?"


def test_wikipedia_client_defaults_to_polish_then_english() -> None:
    client = WikipediaClient()

    assert client._languages == ("pl", "en")
    assert DEFAULT_LANGUAGES == ("pl", "en")


class FakeWikipediaClient:
    def __init__(self, article: WikipediaArticle | None = None) -> None:
        self._article = article
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def search(self, query: str, *, language: str | None = None, limit: int = 5) -> list[WikipediaSearchResult]:
        self.calls.append(("search", {"query": query, "language": language, "limit": limit}))
        if self._article is None:
            return []
        return [
            WikipediaSearchResult(
                language=self._article.language,
                title=self._article.title,
                description=self._article.description,
                page_url=self._article.page_url,
            )
        ]

    async def summary(self, *, language: str, title: str) -> WikipediaArticle | None:
        self.calls.append(("summary", {"title": title, "language": language}))
        if self._article is None or self._article.language != language or self._article.title != title:
            return None
        return self._article

    async def wikidata_facts(
        self,
        wikibase_item: str,
        *,
        property_ids: list[str] | None = None,
        limit: int = 24,
    ) -> dict[str, Any]:
        self.calls.append(
            ("wikidata_facts", {"wikibase_item": wikibase_item, "property_ids": property_ids, "limit": limit})
        )
        if self._article is None or self._article.wikibase_item != wikibase_item:
            return {}
        return {
            "id": wikibase_item,
            "birth_year": self._article.birth_year,
            "coordinates": self._article.coordinates,
            "claims": [{"property_id": "P569", "values": [{"datatype": "time", "value": {"time": "+1879-03-14T00:00:00Z"}}]}],
        }

    async def wikidata_claims_by_property_query(
        self,
        wikibase_item: str,
        property_query: str,
        *,
        language: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "wikidata_claims_by_property_query",
                {"wikibase_item": wikibase_item, "property_query": property_query, "language": language, "limit": limit},
            )
        )
        if self._article is None or self._article.wikibase_item != wikibase_item:
            return {}
        if property_query.casefold() != "gdp":
            return {"id": wikibase_item, "property_query": property_query, "property_candidates": [], "claims": []}
        property_info = {
            "property_id": "P2131",
            "label": "nominal GDP",
            "description": "nominal gross domestic product",
            "language": language or "en",
            "url": "https://www.wikidata.org/wiki/Property:P2131",
        }
        return {
            "id": wikibase_item,
            "property_query": property_query,
            "property_candidates": [property_info],
            "claims": [
                {
                    "property_id": "P2131",
                    "property": property_info,
                    "values": [
                        {
                            "datatype": "quantity",
                            "value": {
                                "amount": "+824734000000",
                                "unit": "http://www.wikidata.org/entity/Q4917",
                            },
                            "rank": "normal",
                            "qualifiers": {
                                "P585": [
                                    {
                                        "datatype": "time",
                                        "value": {
                                            "time": "+2024-01-01T00:00:00Z",
                                            "precision": 9,
                                            "calendar": "http://www.wikidata.org/entity/Q1985727",
                                        },
                                    }
                                ]
                            },
                        }
                    ],
                }
            ],
        }

    async def close(self) -> None:
        pass


class FakeFetchWikipediaClient(WikipediaClient):
    def __init__(self, responses: dict[str, Any]) -> None:
        super().__init__(languages=("pl", "en"))
        self._responses = responses

    async def _fetch_json(self, url: str) -> Any:
        response = self._responses.get(url)
        if response is None:
            raise AssertionError(f"Unexpected URL: {url}")
        return response


class FakeLoopFactory:
    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.config = None
        self.tools = None
        self.loop = None

    def factory(self, config, system_prompt, tools, ollama_connection, **kwargs):
        self.config = config
        self.tools = tools
        self.loop = FakeLoop(self.reply_text)
        return self.loop


class FakeLoop:
    def __init__(self, reply_text: str) -> None:
        self._reply_text = reply_text
        self.user_message = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass

    async def send_user_message(self, message: str) -> AgentReply:
        self.user_message = message
        return AgentReply(reply_text=self._reply_text, end_conversation=False)


class FakeOllamaConnection:
    async def close(self) -> None:
        pass
