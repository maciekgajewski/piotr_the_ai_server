import asyncio

from ai_server.domain_agents.wikipedia import DEFAULT_LANGUAGES, WikipediaArticle, WikipediaDomainAgent, WikipediaClient
from ai_server.interfaces import Conversation


def test_wikipedia_domain_agent_extracts_birth_year() -> None:
    client = FakeWikipediaClient(
        WikipediaArticle(
            language="en",
            title="Albert Einstein",
            extract="Albert Einstein (14 March 1879 – 18 April 1955) was a German-born theoretical physicist.",
            page_url="https://en.wikipedia.org/wiki/Albert_Einstein",
        )
    )
    agent = WikipediaDomainAgent(client=client)
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "wikipedia", "command": {"topic": "Albert Einstein", "fact": "birth year"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["value"] == 1879
    assert result["text"] == "Albert Einstein urodził się w 1879 roku."
    assert client.queries == ["Albert Einstein"]


def test_wikipedia_domain_agent_answers_where_is() -> None:
    client = FakeWikipediaClient(
        WikipediaArticle(
            language="en",
            title="Jacksonville, Florida",
            extract="Jacksonville is the most populous city proper in the U.S. state of Florida. It is located on the Atlantic coast of northeastern Florida.",
            page_url="https://en.wikipedia.org/wiki/Jacksonville,_Florida",
        )
    )
    agent = WikipediaDomainAgent(client=client)
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "wikipedia", "command": {"intent": "where_is", "topic": "Jacksonville"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["fact"] == "location"
    assert result["text"] == "Jacksonville is the most populous city proper in the U.S. state of Florida."


def test_wikipedia_domain_agent_returns_coordinates() -> None:
    client = FakeWikipediaClient(
        WikipediaArticle(
            language="en",
            title="Jacksonville, Florida",
            extract="Jacksonville is a city in Florida.",
            coordinates={"lat": 30.3322, "lon": -81.6557},
        )
    )
    agent = WikipediaDomainAgent(client=client)
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "wikipedia", "command": {"query": "what are the coordinates of Jacksonville"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["value"] == {"lat": 30.3322, "lon": -81.6557}
    assert result["text"] == "Współrzędne Jacksonville, Florida to 30.3322, -81.6557."
    assert client.queries == ["Jacksonville"]


def test_wikipedia_domain_agent_summarizes_article() -> None:
    client = FakeWikipediaClient(
        WikipediaArticle(
            language="en",
            title="John Henry (folklore)",
            extract=(
                "John Henry is an American folk hero. "
                "He is said to have worked as a steel-driving man. "
                "His story is told in many songs."
            ),
            page_url="https://en.wikipedia.org/wiki/John_Henry_(folklore)",
        )
    )
    agent = WikipediaDomainAgent(client=client)
    conversation = Conversation(conversation_id="conversation-1", attributes={})

    result = asyncio.run(
        agent.run_task(
            conversation,
            {"id": "t1", "domain": "wikipedia", "command": {"query": "kim był John Henry?"}},
            {},
        )
    )

    assert result["status"] == "ok"
    assert result["summary"] == "John Henry is an American folk hero. He is said to have worked as a steel-driving man."
    assert result["entities"] == ["wikipedia.en.John Henry (folklore)"]
    assert client.queries == ["John Henry"]


def test_wikipedia_domain_agent_asks_for_missing_topic() -> None:
    agent = WikipediaDomainAgent(client=FakeWikipediaClient(None))
    conversation = Conversation(conversation_id="conversation-1", attributes={})

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
    def __init__(self, article: WikipediaArticle | None) -> None:
        self._article = article
        self.queries = []

    async def summary_for_query(self, query: str) -> WikipediaArticle:
        self.queries.append(query)
        if self._article is None:
            raise LookupError(query)
        return self._article

    async def close(self) -> None:
        pass
