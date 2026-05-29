from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from aiohttp import ClientSession, ClientTimeout

from ai_server.domain_agents.interfaces import DomainTask
from ai_server.interfaces import Conversation


DEFAULT_LANGUAGES = ("pl", "en")
USER_AGENT = "piotr-ai-server/1.0 (http://localhost; local-admin@localhost)"


class WikipediaDomainAgent:
    def __init__(
        self,
        *,
        languages: tuple[str, ...] = DEFAULT_LANGUAGES,
        client: "WikipediaClient | None" = None,
    ) -> None:
        if not languages:
            raise ValueError("WikipediaDomainAgent languages must not be empty")
        self._languages = languages
        self._client = client or WikipediaClient(languages=languages)
        self._logger = logging.getLogger(f"{__name__}.WikipediaDomainAgent[{','.join(languages)}]")

    async def run_task(
        self,
        conversation: Conversation,
        task: DomainTask,
        active_context: dict[str, Any],
    ) -> dict[str, Any]:
        del conversation, active_context
        command = task.get("command", {})
        command = command if isinstance(command, dict) else {}
        topic = _topic_from_command(command)
        if not topic:
            return _clarification_result("Czego mam poszukać w Wikipedii?")

        intent = _intent_from_command(command)
        fact = _fact_from_command(command)
        try:
            article = await self._client.summary_for_query(topic)
        except LookupError:
            return {
                "status": "not_found",
                "text": f"Nie znalazłem w Wikipedii artykułu dla: {topic}.",
                "needs_clarification": False,
                "clarification_question": None,
                "entities": [],
            }

        if intent == "coordinates" or fact == "coordinates":
            result = _coordinates_result(article)
        elif fact == "birth_year":
            result = _birth_year_result(article)
        elif intent == "where_is" or fact == "location":
            result = _where_is_result(article)
        else:
            result = _summary_result(article)

        self._logger.debug("Wikipedia task topic=%r intent=%s fact=%s title=%r", topic, intent, fact, article.title)
        return result

    async def close(self) -> None:
        await self._client.close()


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
        for language in self._languages:
            title = await self._search_title(language, query)
            if title is None:
                continue
            article = await self._summary(language, title)
            if article is not None:
                return article
        raise LookupError(query)

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _search_title(self, language: str, query: str) -> str | None:
        params = urlencode({"q": query, "limit": "1"})
        response = await self._fetch_json(f"https://api.wikimedia.org/core/v1/wikipedia/{language}/search/page?{params}")
        if not isinstance(response, dict):
            return None
        pages = response.get("pages")
        if not isinstance(pages, list) or not pages:
            return None
        first_page = pages[0]
        if not isinstance(first_page, dict):
            return None
        title = first_page.get("title")
        return title if isinstance(title, str) and title else None

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
        facts = await self._wikidata_facts(wikibase_item if isinstance(wikibase_item, str) else "")
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

    async def _wikidata_facts(self, wikibase_item: str) -> dict[str, Any]:
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
        return {
            "birth_year": _wikidata_birth_year(claims),
            "coordinates": _wikidata_coordinates(claims),
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
