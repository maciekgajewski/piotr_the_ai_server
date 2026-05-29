from typing import Any

from ai_server.config import AgentConfig
from ai_server.domain_agents.home_assistant import HomeAssistantDomainAgent
from ai_server.domain_agents.interfaces import DomainAgent, DomainTask
from ai_server.home_assistant import HomeAssistantConnection


def create_domain_agents(
    config: AgentConfig,
    ollama_url: str,
    *,
    home_assistant_connection: HomeAssistantConnection | None = None,
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
                ollama_url=ollama_url,
                connection=home_assistant_connection,
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


__all__ = ["DomainAgent", "DomainTask", "create_domain_agents"]
