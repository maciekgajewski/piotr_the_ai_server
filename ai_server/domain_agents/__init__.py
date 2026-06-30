from pathlib import Path
from typing import Any

from ai_server.config import AgentConfig, DEFAULT_CACHE_DIR, DEFAULT_DATA_DIR, ProcessingUpdatesConfig, ServerConfig
from ai_server.domain_agents.current_time import CurrentTimeDomainAgent
from ai_server.domain_agents.home_assistant import HomeAssistantDomainAgent
from ai_server.domain_agents.interfaces import DomainAgent, DomainTask, QueryCapability
from ai_server.domain_agents.media_player import MediaPlayerDomainAgent
from ai_server.domain_agents.system_status import SystemStatusCollector, SystemStatusDomainAgent, SystemStatusOptions, SystemStatusStore
from ai_server.domain_agents.weather import WeatherDomainAgent
from ai_server.domain_agents.wikipedia import WikipediaDomainAgent
from ai_server.home_assistant import HomeAssistantConnection
from ai_server.utils import JsonFileStore


def create_domain_agents(
    config: AgentConfig,
    ollama_url: str,
    *,
    home_assistant_connection: HomeAssistantConnection | None = None,
    server_config: ServerConfig = ServerConfig(),
    processing_updates: ProcessingUpdatesConfig = ProcessingUpdatesConfig(),
    cache_dir: Path = Path(DEFAULT_CACHE_DIR).expanduser(),
    data_store: JsonFileStore | None = None,
) -> dict[str, DomainAgent]:
    raw_domain_agents = config.options.get("domain_agents", {})
    if not isinstance(raw_domain_agents, dict):
        raise ValueError("agent.domain_agents must be a mapping")

    data_store = data_store or JsonFileStore(Path(DEFAULT_DATA_DIR).expanduser())
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
                processing_update_interval_seconds=processing_updates.interval_seconds,
            )
            continue
        if domain == "time":
            domain_agents[domain] = CurrentTimeDomainAgent(
                timezone=_optional_domain_string(raw_options, domain, "timezone", server_config.timezone),
                location=_optional_domain_string(raw_options, domain, "location", server_config.location),
                cache_dir=_domain_cache_dir(raw_options, domain, cache_dir),
            )
            continue
        if domain == "media_player":
            if home_assistant_connection is None:
                raise ValueError("agent.domain_agents.media_player requires home_assistant config")
            domain_agents[domain] = MediaPlayerDomainAgent(
                model=_domain_agent_model(config.options, raw_options, domain),
                fallback_model=_domain_agent_fallback_model(config.options, raw_options, domain),
                fallback_backoff_seconds=_domain_agent_fallback_backoff_seconds(config.options, raw_options, domain),
                ollama_url=ollama_url,
                connection=home_assistant_connection,
                liked_songs_media_id=_optional_domain_string(raw_options, domain, "liked_songs_media_id", "Liked Songs") or "Liked Songs",
                liked_songs_media_type=_optional_domain_string(raw_options, domain, "liked_songs_media_type", "playlist") or "playlist",
                default_music_media_id=_optional_domain_string(raw_options, domain, "default_music_media_id", "Liked Songs") or "Liked Songs",
                default_music_media_type=_optional_domain_string(raw_options, domain, "default_music_media_type", "playlist") or "playlist",
                default_music_name=_optional_domain_string(raw_options, domain, "default_music_name", "muzykę ze Spotify") or "muzykę ze Spotify",
                processing_update_interval_seconds=processing_updates.interval_seconds,
            )
            continue
        if domain == "wikipedia":
            domain_agents[domain] = WikipediaDomainAgent(
                model=_domain_agent_model(config.options, raw_options, domain),
                fallback_model=_domain_agent_fallback_model(config.options, raw_options, domain),
                fallback_backoff_seconds=_domain_agent_fallback_backoff_seconds(config.options, raw_options, domain),
                ollama_url=ollama_url,
                languages=_domain_languages(raw_options, domain),
                processing_update_interval_seconds=processing_updates.interval_seconds,
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
                data_store=data_store,
                ipgeolocation_api_key=_optional_domain_string(raw_options, domain, "ipgeolocation_api_key", None),
                processing_update_interval_seconds=processing_updates.interval_seconds,
            )
            continue
        if domain == "system_status":
            options = _system_status_options(raw_options, domain)
            status_store = SystemStatusStore(data_store)
            collector = SystemStatusCollector(
                store=status_store,
                options=options,
                home_assistant=home_assistant_connection,
            )
            domain_agents[domain] = SystemStatusDomainAgent(
                model=_domain_agent_model(config.options, raw_options, domain),
                fallback_model=_domain_agent_fallback_model(config.options, raw_options, domain),
                fallback_backoff_seconds=_domain_agent_fallback_backoff_seconds(config.options, raw_options, domain),
                ollama_url=ollama_url,
                collector=collector,
                max_short_report_issues=options.max_short_report_issues,
                processing_update_interval_seconds=processing_updates.interval_seconds,
            )
            continue
        domain_agents[domain] = _UnsupportedConfiguredDomainAgent(domain)

    return domain_agents


class _UnsupportedConfiguredDomainAgent:
    def __init__(self, domain: str) -> None:
        self._domain = domain

    def known_utterances(self) -> dict[str, DomainTask]:
        return {}

    def query_capabilities(self) -> dict[str, QueryCapability]:
        return {}

    def query_capabilities_prompt(self) -> str:
        return ""

    def planning_prompt(self) -> str:
        return ""

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
    model = domain_options.get("model", agent_options.get("cloud_model"))
    if not isinstance(model, str) or not model:
        raise ValueError(f"agent.domain_agents.{domain}.model must be a non-empty string")
    return model


def _domain_agent_fallback_model(agent_options: dict[str, Any], domain_options: dict[str, Any], domain: str) -> str | None:
    fallback_model = domain_options.get("fallback_model", agent_options.get("local_model"))
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


def _system_status_options(domain_options: dict[str, Any], domain: str) -> SystemStatusOptions:
    thresholds = domain_options.get("thresholds", {})
    if thresholds is None:
        thresholds = {}
    if not isinstance(thresholds, dict):
        raise ValueError(f"agent.domain_agents.{domain}.thresholds must be a mapping when provided")
    return SystemStatusOptions(
        collection_interval_seconds=_domain_positive_float(
            domain_options,
            domain,
            "collection_interval_seconds",
            SystemStatusOptions.collection_interval_seconds,
        ),
        baseline_alpha=_domain_bounded_float(
            domain_options,
            domain,
            "baseline_alpha",
            SystemStatusOptions.baseline_alpha,
            min_value=0.0,
            max_value=1.0,
        ),
        max_short_report_issues=_domain_positive_int(
            domain_options,
            domain,
            "max_short_report_issues",
            SystemStatusOptions.max_short_report_issues,
        ),
        disk_paths=_domain_string_tuple(domain_options, domain, "disk_paths", SystemStatusOptions.disk_paths),
        home_assistant_entities=_domain_string_tuple(
            domain_options,
            domain,
            "home_assistant_entities",
            SystemStatusOptions.home_assistant_entities,
        ),
        disk_free_warning_percent=_threshold_float(thresholds, domain, "disk_free_warning_percent", SystemStatusOptions.disk_free_warning_percent),
        disk_free_critical_percent=_threshold_float(thresholds, domain, "disk_free_critical_percent", SystemStatusOptions.disk_free_critical_percent),
        inode_free_warning_percent=_threshold_float(thresholds, domain, "inode_free_warning_percent", SystemStatusOptions.inode_free_warning_percent),
        inode_free_critical_percent=_threshold_float(thresholds, domain, "inode_free_critical_percent", SystemStatusOptions.inode_free_critical_percent),
        memory_available_warning_percent=_threshold_float(
            thresholds,
            domain,
            "memory_available_warning_percent",
            SystemStatusOptions.memory_available_warning_percent,
        ),
        memory_available_critical_percent=_threshold_float(
            thresholds,
            domain,
            "memory_available_critical_percent",
            SystemStatusOptions.memory_available_critical_percent,
        ),
        swap_used_warning_percent=_threshold_float(thresholds, domain, "swap_used_warning_percent", SystemStatusOptions.swap_used_warning_percent),
        swap_used_critical_percent=_threshold_float(thresholds, domain, "swap_used_critical_percent", SystemStatusOptions.swap_used_critical_percent),
        load_per_cpu_warning=_threshold_float(thresholds, domain, "load_per_cpu_warning", SystemStatusOptions.load_per_cpu_warning),
        load_per_cpu_critical=_threshold_float(thresholds, domain, "load_per_cpu_critical", SystemStatusOptions.load_per_cpu_critical),
        temperature_warning_c=_threshold_float(thresholds, domain, "temperature_warning_c", SystemStatusOptions.temperature_warning_c),
        temperature_critical_c=_threshold_float(thresholds, domain, "temperature_critical_c", SystemStatusOptions.temperature_critical_c),
        stale_snapshot_seconds=_threshold_float(thresholds, domain, "stale_snapshot_seconds", SystemStatusOptions.stale_snapshot_seconds),
        ha_entity_stale_seconds=_threshold_float(thresholds, domain, "ha_entity_stale_seconds", SystemStatusOptions.ha_entity_stale_seconds),
        baseline_min_samples=_domain_positive_int(domain_options, domain, "baseline_min_samples", SystemStatusOptions.baseline_min_samples),
        baseline_deviation_ratio=_domain_positive_float(
            domain_options,
            domain,
            "baseline_deviation_ratio",
            SystemStatusOptions.baseline_deviation_ratio,
        ),
    )


def _domain_positive_float(domain_options: dict[str, Any], domain: str, key: str, default: float) -> float:
    value = domain_options.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"agent.domain_agents.{domain}.{key} must be a positive number")
    return float(value)


def _domain_bounded_float(
    domain_options: dict[str, Any],
    domain: str,
    key: str,
    default: float,
    *,
    min_value: float,
    max_value: float,
) -> float:
    value = domain_options.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not min_value < value <= max_value:
        raise ValueError(f"agent.domain_agents.{domain}.{key} must be greater than {min_value} and at most {max_value}")
    return float(value)


def _domain_positive_int(domain_options: dict[str, Any], domain: str, key: str, default: int) -> int:
    value = domain_options.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"agent.domain_agents.{domain}.{key} must be a positive integer")
    return value


def _domain_string_tuple(domain_options: dict[str, Any], domain: str, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = domain_options.get(key, default)
    if isinstance(value, tuple) and all(isinstance(item, str) and item for item in value):
        return value
    if isinstance(value, list) and all(isinstance(item, str) and item for item in value):
        return tuple(value)
    raise ValueError(f"agent.domain_agents.{domain}.{key} must be a list of non-empty strings")


def _threshold_float(thresholds: dict[str, Any], domain: str, key: str, default: float) -> float:
    value = thresholds.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValueError(f"agent.domain_agents.{domain}.thresholds.{key} must be a non-negative number")
    return float(value)


__all__ = ["DomainAgent", "DomainTask", "QueryCapability", "create_domain_agents"]
