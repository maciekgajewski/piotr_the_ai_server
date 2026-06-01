from pathlib import Path
from typing import Any

from ai_server.config import AgentConfig, DEFAULT_CACHE_DIR, ServerConfig
from ai_server.domain_agents.current_time import CurrentTimeDomainAgent
from ai_server.domain_agents.home_assistant import HomeAssistantDomainAgent
from ai_server.domain_agents.interfaces import DomainAgent, DomainTask
from ai_server.domain_agents.weather import WeatherDomainAgent
from ai_server.domain_agents.wikipedia import WikipediaDomainAgent
from ai_server.home_assistant import HomeAssistantConnection


def create_domain_agents(
    config: AgentConfig,
    ollama_url: str,
    *,
    home_assistant_connection: HomeAssistantConnection | None = None,
    server_config: ServerConfig = ServerConfig(),
    cache_dir: Path = Path(DEFAULT_CACHE_DIR).expanduser(),
) -> dict[str, DomainAgent]:
    raw_domain_agents = config.options.get("domain_agents", {})
    if not isinstance(raw_domain_agents, dict):
        raise ValueError("agent.domain_agents must be a mapping")

    domain_agents: dict[str, DomainAgent] = {}
    for domain, raw_options in raw_domain_agents.items():
        if not isinstance(raw_options, dict):
            raise ValueError(f"agent.domain_agents.{domain} must be a mapping")
        if domain == "home_assistant":
            if home_assistant_connection is None:
                raise ValueError("agent.domain_agents.home_assistant requires home_assistant config")
            domain_agents[domain] = HomeAssistantDomainAgent(
                model=_domain_agent_model(config.options, raw_options, domain),
                fallback_model=_domain_agent_fallback_model(config.options, raw_options, domain),
                fallback_backoff_seconds=_domain_agent_fallback_backoff_seconds(config.options, raw_options, domain),
                ollama_url=ollama_url,
                connection=home_assistant_connection,
            )
            continue
        if domain == "time":
            domain_agents[domain] = CurrentTimeDomainAgent(
                timezone=_optional_domain_string(raw_options, domain, "timezone", server_config.timezone),
                location=_optional_domain_string(raw_options, domain, "location", server_config.location),
                cache_dir=_domain_cache_dir(raw_options, domain, cache_dir),
            )
            continue
        if domain == "wikipedia":
            domain_agents[domain] = WikipediaDomainAgent(
                languages=_domain_languages(raw_options, domain),
            )
            continue
        if domain == "weather":
            domain_agents[domain] = WeatherDomainAgent(
                model=_domain_agent_model(config.options, raw_options, domain),
                fallback_model=_domain_agent_fallback_model(config.options, raw_options, domain),
                fallback_backoff_seconds=_domain_agent_fallback_backoff_seconds(config.options, raw_options, domain),
                ollama_url=ollama_url,
                location=_optional_domain_string(raw_options, domain, "location", server_config.location),
                cache_dir=_domain_cache_dir(raw_options, domain, cache_dir),
            )
            continue
        domain_agents[domain] = _UnsupportedConfiguredDomainAgent(domain)

    return domain_agents


class _UnsupportedConfiguredDomainAgent:
    def __init__(self, domain: str) -> None:
        self._domain = domain

    async def run_task(self, conversation, task: DomainTask, active_context: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": "unsupported_domain",
            "text": f"Domain agent is not implemented: {self._domain}",
            "needs_clarification": False,
            "clarification_question": None,
            "entities": [],
        }

    async def close(self) -> None:
        pass


def _domain_agent_model(agent_options: dict[str, Any], domain_options: dict[str, Any], domain: str) -> str:
    model = domain_options.get("model", agent_options.get("model"))
    if not isinstance(model, str) or not model:
        raise ValueError(f"agent.domain_agents.{domain}.model must be a non-empty string")
    return model


def _domain_agent_fallback_model(agent_options: dict[str, Any], domain_options: dict[str, Any], domain: str) -> str | None:
    fallback_model = domain_options.get("fallback_model", agent_options.get("fallback_model"))
    if fallback_model is None:
        return None
    if not isinstance(fallback_model, str) or not fallback_model:
        raise ValueError(f"agent.domain_agents.{domain}.fallback_model must be a non-empty string when provided")
    return fallback_model


def _domain_agent_fallback_backoff_seconds(agent_options: dict[str, Any], domain_options: dict[str, Any], domain: str) -> float:
    value = domain_options.get("fallback_backoff_seconds", agent_options.get("fallback_backoff_seconds", 300.0))
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"agent.domain_agents.{domain}.fallback_backoff_seconds must be a positive number")
    return float(value)


def _optional_domain_string(domain_options: dict[str, Any], domain: str, key: str, default: str | None) -> str | None:
    value = domain_options.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"agent.domain_agents.{domain}.{key} must be a non-empty string when provided")
    return value


def _domain_cache_dir(domain_options: dict[str, Any], domain: str, default: Path) -> Path:
    value = domain_options.get("cache_dir")
    if value is None:
        return default
    if not isinstance(value, str) or not value:
        raise ValueError(f"agent.domain_agents.{domain}.cache_dir must be a non-empty string when provided")
    return Path(value).expanduser()


def _domain_languages(domain_options: dict[str, Any], domain: str) -> tuple[str, ...]:
    raw_languages = domain_options.get("languages", domain_options.get("language", ("pl", "en")))
    if isinstance(raw_languages, str) and raw_languages:
        return (raw_languages,)
    if (
        isinstance(raw_languages, list)
        and raw_languages
        and all(isinstance(language, str) and language for language in raw_languages)
    ):
        return tuple(raw_languages)
    if isinstance(raw_languages, tuple) and raw_languages and all(isinstance(language, str) and language for language in raw_languages):
        return raw_languages
    raise ValueError(f"agent.domain_agents.{domain}.languages must be a non-empty string or list of strings")


__all__ = ["DomainAgent", "DomainTask", "create_domain_agents"]
