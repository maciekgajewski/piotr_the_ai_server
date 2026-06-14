from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Annotated, Any, Callable
from urllib.parse import quote, urlencode

from aiohttp import ClientSession, ClientTimeout

from ai_server.agent_loop import AgentCallableSet, AgentLoop, AgentLoopConfig, AgentLoopOllamaConnection
from ai_server.domain_agents.interfaces import DomainTask
from ai_server.interfaces import Conversation


DEFAULT_LANGUAGES = ("pl", "en")
USER_AGENT = "piotr-ai-server/1.0 (http://localhost; local-admin@localhost)"
SYSTEM_PROMPT = """
You are a Wikipedia/Wikidata domain-specific agent for a Polish voice assistant.
You receive exactly one structured task from the orchestrator.
Use the available read-only tools to search sources, inspect summaries, and inspect Wikidata facts.
Do not answer from memory. Do not invent facts, titles, URLs, or identifiers.
Your first assistant action must be a tool call to search_wikipedia.
Do not return final JSON before at least one source tool has returned.
If tool results do not contain enough information, say that the source data is insufficient or ask a clarification question.
Keep the design source-oriented: Wikipedia and Wikidata are the current sources, but other encyclopedic sources may be added later.

Recommended flow:
1. Call search_wikipedia for the user's entity/topic, not for the requested property.
   If the user asks for a property, search the article subject and use summaries/facts for the property.
2. Call get_wikipedia_summary for the best candidate.
3. Use fields already returned by get_wikipedia_summary, such as coordinates, when they answer the question.
4. Call get_wikidata_facts only when get_wikipedia_summary returned a wikibase_item for the selected article.
5. Return final JSON only after the needed tool calls. Never return not_found before search_wikipedia returned no usable candidates.

Return only compact valid JSON with this shape:
{
  "status": "ok|not_found|failed|needs_clarification",
  "text": "short Polish user-facing answer",
  "needs_clarification": false,
  "clarification_question": null,
  "entities": ["stable source entity ids"],
  "title": "optional selected article title",
  "language": "optional selected article language",
  "url": "optional selected source URL",
  "sources": []
}

Use status="needs_clarification" only when another user turn is needed.
Use status="not_found" when source searches found no relevant article.
When status is "ok", include at least one entity such as "wikipedia.pl.Albert Einstein" when an article was used.
Set final_reply_mode="verbatim" so the orchestrator preserves the text.
"""


class WikipediaDomainAgent:
    def __init__(
        self,
        *,
        model: str,
        ollama_url: str,
        languages: tuple[str, ...] = DEFAULT_LANGUAGES,
        client: "WikipediaClient | None" = None,
        fallback_model: str | None = None,
        fallback_backoff_seconds: float = 300.0,
        ollama_connection: AgentLoopOllamaConnection | None = None,
        loop_factory: Callable[..., AgentLoop] = AgentLoop,
    ) -> None:
        if not languages:
            raise ValueError("WikipediaDomainAgent languages must not be empty")
        self._model = model
        self._ollama_url = ollama_url
        self._fallback_model = fallback_model
        self._fallback_backoff_seconds = fallback_backoff_seconds
        self._languages = languages
        self._client = client or WikipediaClient(languages=languages)
        self._ollama_connection = ollama_connection or AgentLoopOllamaConnection(base_url=ollama_url)
        self._owns_ollama_connection = ollama_connection is None
        self._loop_factory = loop_factory
        self._logger = logging.getLogger(f"{__name__}.WikipediaDomainAgent[{model}:{','.join(languages)}]")

    async def run_task(
        self,
        conversation: Conversation,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        task_id = task.get("id", "unknown")
        logger = logging.getLogger(f"{__name__}.WikipediaDomainAgent[{self._model}:{conversation.conversation_id}:{task_id}]")
        toolset = WikipediaDomainToolSet(
            self._client,
            logger_name=f"{__name__}.WikipediaDomainToolSet[{conversation.conversation_id}:{task_id}]",
        )
        loop_config = AgentLoopConfig(
            model=self._model,
            ollama_url=self._ollama_url,
            fallback_model=self._fallback_model,
            fallback_backoff_seconds=self._fallback_backoff_seconds,
            options={"num_predict": 512, "temperature": 0, "num_ctx": 4096},
            keep_alive="1h",
        )
        payload = {
            "task": task,
            "active_context": active_context,
            "conversation": {
                "user": conversation.user,
                "area": conversation.area,
                "user_settings": conversation.user_settings,
            },
        }
        logger.debug("running Wikipedia DSA task=%s active_context=%s", task, active_context)
        async with self._loop_factory(
            config=loop_config,
            system_prompt=SYSTEM_PROMPT,
            tools=toolset,
            ollama_connection=self._ollama_connection,
        ) as loop:
            reply = await loop.send_user_message(json.dumps(payload, ensure_ascii=False))
        logger.debug("Wikipedia DSA raw reply=%r end_conversation=%s", reply.reply_text, reply.end_conversation)
        if reply.end_conversation:
            return _failed_result("Nie mogę teraz sprawdzić Wikipedii.")
        try:
            return _parse_domain_reply(reply.reply_text)
        except ValueError:
            logger.debug("rejecting non-JSON Wikipedia DSA reply=%r", reply.reply_text)
            return _failed_result("Nie mogę teraz przygotować odpowiedzi z Wikipedii.")

    async def close(self) -> None:
        await self._client.close()
        if self._owns_ollama_connection:
            await self._ollama_connection.close()


class WikipediaDomainToolSet(AgentCallableSet):
    def __init__(self, client: "WikipediaClient", *, logger_name: str | None = None) -> None:
        self._client = client
        self._known_wikibase_items: set[str] = set()
        self._logger = logging.getLogger(logger_name or f"{__name__}.{type(self).__name__}")

    @AgentCallableSet.tool(
        description=(
            "Search Wikipedia articles in the configured source languages. "
            "Use an entity or article-topic query, not words for the requested property."
        )
    )
    async def search_wikipedia(
        self,
        query: Annotated[str, "Natural language search query or article topic."],
        language: Annotated[str | None, "Optional Wikipedia language code such as pl or en."] = None,
        limit: Annotated[int, "Maximum number of candidates to return across languages."] = 5,
    ) -> dict[str, Any]:
        if not query.strip():
            return {"status": "needs_clarification", "message": "Search query is empty.", "results": []}
        results = await self._client.search(query.strip(), language=language, limit=max(1, min(limit, 10)))
        self._logger.info("search_wikipedia query=%r language=%r results=%s", query, language, len(results))
        return {"status": "ok" if results else "not_found", "results": [result.to_json() for result in results]}

    @AgentCallableSet.tool(description="Fetch a Wikipedia article summary by title and language.")
    async def get_wikipedia_summary(
        self,
        title: Annotated[str, "Exact article title from search_wikipedia."],
        language: Annotated[str, "Wikipedia language code from search_wikipedia."],
    ) -> dict[str, Any]:
        article = await self._client.summary(language=language, title=title)
        if article is None:
            return {"status": "not_found", "message": f"No summary found for {language}:{title}."}
        if article.wikibase_item:
            self._known_wikibase_items.add(article.wikibase_item)
        self._logger.info("get_wikipedia_summary language=%s title=%r wikibase_item=%r", language, title, article.wikibase_item)
        return {"status": "ok", "article": article.to_json()}

    @AgentCallableSet.tool(description="Fetch simplified Wikidata facts for a Wikidata item.")
    async def get_wikidata_facts(
        self,
        wikibase_item: Annotated[str, "Wikidata item id such as Q937."],
        property_ids: Annotated[list[str] | None, "Optional Wikidata property ids to include, such as P569 or P625."] = None,
        limit: Annotated[int, "Maximum number of properties to return when property_ids is omitted."] = 24,
    ) -> dict[str, Any]:
        if not wikibase_item.strip():
            return {"status": "needs_clarification", "message": "Wikidata item id is empty."}
        if wikibase_item.strip() not in self._known_wikibase_items:
            return {
                "status": "needs_summary",
                "message": "Call get_wikipedia_summary first and use the wikibase_item returned by that tool.",
            }
        facts = await self._client.wikidata_facts(
            wikibase_item.strip(),
            property_ids=property_ids,
            limit=max(1, min(limit, 50)),
        )
        self._logger.info("get_wikidata_facts item=%s properties=%s", wikibase_item, len(facts.get("claims", [])))
        return {"status": "ok" if facts else "not_found", "facts": facts}


class WikipediaClient:
    def __init__(
        self,
        *,
        languages: tuple[str, ...] = DEFAULT_LANGUAGES,
        session: ClientSession | None = None,
    ) -> None:
        self._languages = languages
        self._session = session
        self._owns_session = session is None
        self._logger = logging.getLogger(f"{__name__}.WikipediaClient[{','.join(languages)}]")

    async def summary_for_query(self, query: str) -> "WikipediaArticle":
        for result in await self.search(query, limit=1):
            article = await self.summary(language=result.language, title=result.title)
            if article is not None:
                return article
        raise LookupError(query)

    async def search(self, query: str, *, language: str | None = None, limit: int = 5) -> list["WikipediaSearchResult"]:
        languages = _preferred_languages(language, self._languages)
        results: list[WikipediaSearchResult] = []
        for source_language in languages:
            results.extend(await self._search(source_language, query, max(1, limit - len(results))))
            if len(results) >= limit:
                break
        return results[:limit]

    async def summary(self, *, language: str, title: str) -> "WikipediaArticle | None":
        return await self._summary(language, title)

    async def wikidata_facts(
        self,
        wikibase_item: str,
        *,
        property_ids: list[str] | None = None,
        limit: int = 24,
    ) -> dict[str, Any]:
        return await self._wikidata_facts(wikibase_item, property_ids=property_ids, limit=limit)

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _search_title(self, language: str, query: str) -> str | None:
        results = await self._search(language, query, 1)
        return results[0].title if results else None

    async def _search(self, language: str, query: str, limit: int) -> list["WikipediaSearchResult"]:
        params = urlencode({"q": query, "limit": "1"})
        if limit != 1:
            params = urlencode({"q": query, "limit": str(limit)})
        response = await self._fetch_json(f"https://api.wikimedia.org/core/v1/wikipedia/{language}/search/page?{params}")
        if not isinstance(response, dict):
            return []
        pages = response.get("pages")
        if not isinstance(pages, list) or not pages:
            return []
        results = []
        for page in pages:
            if not isinstance(page, dict):
                continue
            title = page.get("title")
            if not isinstance(title, str) or not title:
                continue
            key = page.get("key")
            page_url = f"https://{language}.wikipedia.org/wiki/{quote(key if isinstance(key, str) and key else title)}"
            results.append(
                WikipediaSearchResult(
                    language=language,
                    title=title,
                    description=page.get("description") if isinstance(page.get("description"), str) else None,
                    excerpt=page.get("excerpt") if isinstance(page.get("excerpt"), str) else None,
                    page_url=page_url,
                )
            )
        return results

    async def _summary(self, language: str, title: str) -> "WikipediaArticle | None":
        response = await self._fetch_json(f"https://{language}.wikipedia.org/api/rest_v1/page/summary/{quote(title)}")
        if not isinstance(response, dict) or response.get("type") == "disambiguation":
            return None
        raw_title = response.get("title")
        extract = response.get("extract")
        if not isinstance(raw_title, str) or not isinstance(extract, str) or not extract:
            return None

        content_urls = response.get("content_urls", {})
        desktop = content_urls.get("desktop", {}) if isinstance(content_urls, dict) else {}
        page_url = desktop.get("page") if isinstance(desktop, dict) else None
        coordinates = response.get("coordinates")
        wikibase_item = response.get("wikibase_item")
        facts = await self._wikidata_facts(wikibase_item if isinstance(wikibase_item, str) else "", property_ids=["P569", "P625"])
        return WikipediaArticle(
            language=language,
            title=raw_title,
            extract=extract,
            description=response.get("description") if isinstance(response.get("description"), str) else None,
            page_url=page_url if isinstance(page_url, str) else None,
            wikibase_item=wikibase_item if isinstance(wikibase_item, str) else None,
            birth_year=facts.get("birth_year") if isinstance(facts.get("birth_year"), int) else None,
            coordinates=_coordinates_from_summary(coordinates) or _coordinates_from_wikidata(facts.get("coordinates")),
        )

    async def _wikidata_facts(
        self,
        wikibase_item: str,
        *,
        property_ids: list[str] | None = None,
        limit: int = 24,
    ) -> dict[str, Any]:
        if not wikibase_item:
            return {}
        try:
            response = await self._fetch_json(f"https://www.wikidata.org/wiki/Special:EntityData/{quote(wikibase_item)}.json")
        except Exception:
            self._logger.debug("failed to fetch Wikidata facts item=%s", wikibase_item, exc_info=True)
            return {}
        if not isinstance(response, dict):
            return {}
        entities = response.get("entities")
        entity = entities.get(wikibase_item) if isinstance(entities, dict) else None
        claims = entity.get("claims") if isinstance(entity, dict) else None
        if not isinstance(claims, dict):
            return {}
        labels = _wikidata_language_values(entity.get("labels"))
        descriptions = _wikidata_language_values(entity.get("descriptions"))
        aliases = _wikidata_aliases(entity.get("aliases"))
        return {
            "id": wikibase_item,
            "labels": labels,
            "descriptions": descriptions,
            "aliases": aliases,
            "birth_year": _wikidata_birth_year(claims),
            "coordinates": _wikidata_coordinates(claims),
            "claims": _simplify_wikidata_claims(claims, property_ids=property_ids, limit=limit),
        }

    async def _fetch_json(self, url: str) -> Any:
        session = self._session
        if session is None:
            timeout = ClientTimeout(total=10)
            session = ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT})
            self._session = session
        async with session.get(url) as response:
            if response.status >= 400:
                raise RuntimeError(f"Wikipedia request failed with status {response.status}")
            return await response.json()


@dataclass(frozen=True)
class WikipediaArticle:
    language: str
    title: str
    extract: str
    description: str | None = None
    page_url: str | None = None
    wikibase_item: str | None = None
    birth_year: int | None = None
    coordinates: dict[str, float] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": "wikipedia",
            "language": self.language,
            "title": self.title,
            "extract": self.extract,
            "description": self.description,
            "url": self.page_url,
            "wikibase_item": self.wikibase_item,
            "birth_year": self.birth_year,
            "coordinates": self.coordinates,
            "entity": f"wikipedia.{self.language}.{self.title}",
        }


@dataclass(frozen=True)
class WikipediaSearchResult:
    language: str
    title: str
    description: str | None = None
    excerpt: str | None = None
    page_url: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "source": "wikipedia",
            "language": self.language,
            "title": self.title,
            "description": self.description,
            "excerpt": self.excerpt,
            "url": self.page_url,
            "entity": f"wikipedia.{self.language}.{self.title}",
        }


def _parse_domain_reply(content: str) -> dict[str, Any]:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("Wikipedia DSA reply must be valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("Wikipedia DSA reply must be a JSON object")
    status = raw.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("Wikipedia DSA reply status must be a non-empty string")
    text = raw.get("text")
    if not isinstance(text, str):
        raise ValueError("Wikipedia DSA reply text must be a string")
    needs_clarification = raw.get("needs_clarification", status == "needs_clarification")
    if not isinstance(needs_clarification, bool):
        raise ValueError("Wikipedia DSA reply needs_clarification must be a boolean")
    clarification_question = raw.get("clarification_question")
    if clarification_question is not None and not isinstance(clarification_question, str):
        raise ValueError("Wikipedia DSA reply clarification_question must be a string or null")
    entities = raw.get("entities", [])
    if not isinstance(entities, list) or any(not isinstance(entity, str) for entity in entities):
        raise ValueError("Wikipedia DSA reply entities must be a list of strings")

    parsed = dict(raw)
    parsed["needs_clarification"] = needs_clarification
    parsed["clarification_question"] = clarification_question
    parsed["entities"] = entities
    parsed.setdefault("final_reply_mode", "verbatim")
    return parsed


def _topic_from_command(command: dict[str, Any]) -> str:
    for key in ("topic", "query", "title"):
        value = command.get(key)
        if isinstance(value, str) and value:
            return _clean_query(value)
    return ""


def _intent_from_command(command: dict[str, Any]) -> str:
    value = command.get("intent")
    if isinstance(value, str) and value:
        return _normalize_intent(value)
    raw_query = _raw_command_text(command)
    normalized_query = raw_query.casefold()
    ascii_query = _ascii_fold(normalized_query)
    if "wspolrzed" in ascii_query or "coordinates" in normalized_query:
        return "coordinates"
    if normalized_query.startswith(("gdzie jest", "where is")):
        return "where_is"
    return "summary"


def _fact_from_command(command: dict[str, Any]) -> str:
    fact = command.get("fact")
    if isinstance(fact, str) and fact:
        normalized_fact = _ascii_fold(fact.casefold())
        if "birth" in normalized_fact or "urod" in normalized_fact:
            return "birth_year"
        if "coordinate" in normalized_fact or "wspolrzed" in normalized_fact:
            return "coordinates"
        if "where" in normalized_fact or "location" in normalized_fact or "gdzie" in normalized_fact:
            return "location"
    raw_query = _ascii_fold(_raw_command_text(command).casefold())
    if "birth" in raw_query or "urod" in raw_query:
        return "birth_year"
    if "coordinate" in raw_query or "wspolrzed" in raw_query:
        return "coordinates"
    return ""


def _normalize_intent(intent: str) -> str:
    normalized_intent = _ascii_fold(intent.casefold())
    if normalized_intent in {"lookup_fact", "fact"}:
        return "fact"
    if normalized_intent in {"summary", "summarize", "article_summary"}:
        return "summary"
    if normalized_intent in {"coordinates", "coordinate_lookup"}:
        return "coordinates"
    if normalized_intent in {"where_is", "location"}:
        return "where_is"
    return normalized_intent


def _summary_result(article: WikipediaArticle) -> dict[str, Any]:
    summary = _first_sentences(article.extract, 2)
    return _ok_result(
        text=summary,
        article=article,
        data={"summary": summary},
    )


def _birth_year_result(article: WikipediaArticle) -> dict[str, Any]:
    year = article.birth_year or _extract_birth_year(article.extract)
    if year is None:
        return _ok_result(
            text=f"Nie znalazłem roku urodzenia w krótkim opisie artykułu {article.title}.",
            article=article,
            data={"fact": "birth_year", "value": None},
        )
    return _ok_result(
        text=f"{article.title} urodził się w {year} roku.",
        article=article,
        data={"fact": "birth_year", "value": year},
    )


def _coordinates_result(article: WikipediaArticle) -> dict[str, Any]:
    if article.coordinates is None:
        return _ok_result(
            text=f"Nie znalazłem współrzędnych w krótkim opisie artykułu {article.title}.",
            article=article,
            data={"fact": "coordinates", "value": None},
        )
    latitude = article.coordinates["lat"]
    longitude = article.coordinates["lon"]
    return _ok_result(
        text=f"Współrzędne {article.title} to {latitude:.4f}, {longitude:.4f}.",
        article=article,
        data={"fact": "coordinates", "value": {"lat": latitude, "lon": longitude}},
    )


def _where_is_result(article: WikipediaArticle) -> dict[str, Any]:
    sentence = _first_sentences(article.extract, 1)
    return _ok_result(
        text=sentence,
        article=article,
        data={"fact": "location", "value": sentence},
    )


def _ok_result(*, text: str, article: WikipediaArticle, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "ok",
        "text": text,
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [f"wikipedia.{article.language}.{article.title}"],
        "title": article.title,
        "language": article.language,
        "url": article.page_url,
        **data,
    }


def _clarification_result(question: str) -> dict[str, Any]:
    return {
        "status": "needs_clarification",
        "text": question,
        "needs_clarification": True,
        "clarification_question": question,
        "entities": [],
    }


def _failed_result(text: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "text": text,
        "needs_clarification": False,
        "clarification_question": None,
        "entities": [],
    }


def _preferred_languages(language: str | None, default_languages: tuple[str, ...]) -> tuple[str, ...]:
    if language is None:
        return default_languages
    return tuple(dict.fromkeys((language, *default_languages)))


def _clean_query(query: str) -> str:
    cleaned = query.strip(" ?.!").strip()
    for prefix in (
        "kim był ",
        "kim byla ",
        "kim była ",
        "who was ",
        "gdzie jest ",
        "where is ",
        "what are the coordinates of ",
        "jakie są współrzędne ",
        "jakie sa wspolrzedne ",
    ):
        if cleaned.casefold().startswith(prefix):
            return cleaned[len(prefix) :].strip(" ?.!").strip()
    return cleaned


def _raw_command_text(command: dict[str, Any]) -> str:
    for key in ("query", "topic", "title"):
        value = command.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_birth_year(extract: str) -> int | None:
    for pattern in (
        r"\bur\.\s*(?:\d{1,2}\s+\w+\s+)?(\d{4})\b",
        r"\burodz\w*\s+(?:\d{1,2}\s+\w+\s+)?(\d{4})\b",
        r"\bborn\s+(?:on\s+)?(?:\d{1,2}\s+\w+\s+)?(\d{4})\b",
        r"\((?:\w+\s+)?(?:\d{1,2}\s+\w+\s+)?(\d{4})\s*[–-]",
    ):
        match = re.search(pattern, extract, flags=re.IGNORECASE)
        if match is not None:
            return int(match.group(1))
    return None


def _coordinates_from_summary(coordinates: Any) -> dict[str, float] | None:
    if not isinstance(coordinates, dict):
        return None
    latitude = coordinates.get("lat")
    longitude = coordinates.get("lon")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    return {"lat": float(latitude), "lon": float(longitude)}


def _coordinates_from_wikidata(coordinates: Any) -> dict[str, float] | None:
    if not isinstance(coordinates, dict):
        return None
    latitude = coordinates.get("latitude")
    longitude = coordinates.get("longitude")
    if not isinstance(latitude, (int, float)) or not isinstance(longitude, (int, float)):
        return None
    return {"lat": float(latitude), "lon": float(longitude)}


def _wikidata_birth_year(claims: dict[str, Any]) -> int | None:
    claim = _first_claim_datavalue(claims, "P569")
    value = claim.get("value") if isinstance(claim, dict) else None
    time_value = value.get("time") if isinstance(value, dict) else None
    if not isinstance(time_value, str):
        return None
    match = re.match(r"[+-](\d{4})-", time_value)
    return int(match.group(1)) if match is not None else None


def _wikidata_coordinates(claims: dict[str, Any]) -> dict[str, Any] | None:
    claim = _first_claim_datavalue(claims, "P625")
    value = claim.get("value") if isinstance(claim, dict) else None
    return value if isinstance(value, dict) else None


def _first_claim_datavalue(claims: dict[str, Any], property_id: str) -> dict[str, Any] | None:
    property_claims = claims.get(property_id)
    if not isinstance(property_claims, list) or not property_claims:
        return None
    mainsnak = property_claims[0].get("mainsnak") if isinstance(property_claims[0], dict) else None
    datavalue = mainsnak.get("datavalue") if isinstance(mainsnak, dict) else None
    return datavalue if isinstance(datavalue, dict) else None


def _wikidata_language_values(raw_values: Any) -> dict[str, str]:
    if not isinstance(raw_values, dict):
        return {}
    values = {}
    for language, raw_value in raw_values.items():
        if not isinstance(language, str) or not isinstance(raw_value, dict):
            continue
        value = raw_value.get("value")
        if isinstance(value, str) and value:
            values[language] = value
    return values


def _wikidata_aliases(raw_aliases: Any) -> dict[str, list[str]]:
    if not isinstance(raw_aliases, dict):
        return {}
    aliases = {}
    for language, raw_values in raw_aliases.items():
        if not isinstance(language, str) or not isinstance(raw_values, list):
            continue
        values = []
        for raw_value in raw_values:
            value = raw_value.get("value") if isinstance(raw_value, dict) else None
            if isinstance(value, str) and value:
                values.append(value)
        if values:
            aliases[language] = values
    return aliases


def _simplify_wikidata_claims(
    claims: dict[str, Any],
    *,
    property_ids: list[str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    selected_property_ids = property_ids or list(claims.keys())[:limit]
    simplified = []
    for property_id in selected_property_ids[:limit]:
        raw_property_claims = claims.get(property_id)
        if not isinstance(raw_property_claims, list):
            continue
        values = []
        for raw_claim in raw_property_claims[:3]:
            mainsnak = raw_claim.get("mainsnak") if isinstance(raw_claim, dict) else None
            if not isinstance(mainsnak, dict):
                continue
            datavalue = mainsnak.get("datavalue")
            if not isinstance(datavalue, dict):
                continue
            values.append(
                {
                    "datatype": mainsnak.get("datatype"),
                    "value": _simplify_wikidata_value(datavalue.get("value")),
                }
            )
        if values:
            simplified.append({"property_id": property_id, "values": values})
    return simplified


def _simplify_wikidata_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    if "id" in value and isinstance(value.get("id"), str):
        return {"entity_id": value["id"]}
    if "time" in value and isinstance(value.get("time"), str):
        return {
            "time": value.get("time"),
            "precision": value.get("precision"),
            "calendar": value.get("calendarmodel"),
        }
    if "latitude" in value and "longitude" in value:
        return {
            "latitude": value.get("latitude"),
            "longitude": value.get("longitude"),
            "precision": value.get("precision"),
            "globe": value.get("globe"),
        }
    if "amount" in value:
        return {"amount": value.get("amount"), "unit": value.get("unit")}
    if "text" in value:
        return {"text": value.get("text"), "language": value.get("language")}
    return value


def _first_sentences(text: str, limit: int) -> str:
    protected_text = text.strip().replace("U.S.", "U<dot>S<dot>").replace("e.g.", "e<dot>g<dot>")
    sentences = re.split(r"(?<=[.!?])\s+", protected_text)
    sentences = [sentence.replace("<dot>", ".") for sentence in sentences]
    selected = [sentence for sentence in sentences if sentence][:limit]
    return " ".join(selected) if selected else text.strip()


def _ascii_fold(text: str) -> str:
    return (
        text.replace("ą", "a")
        .replace("ć", "c")
        .replace("ę", "e")
        .replace("ł", "l")
        .replace("ń", "n")
        .replace("ó", "o")
        .replace("ś", "s")
        .replace("ź", "z")
        .replace("ż", "z")
    )
