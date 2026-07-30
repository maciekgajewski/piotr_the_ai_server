from __future__ import annotations

from collections.abc import Mapping

from ai_server.domain_agents.interfaces import DomainAgent
from ai_server.orchestrator.messages import RoutedTask
from ai_server.utils.text import ascii_fold, normalize_text


KnownUtteranceDomains = dict[str, str]


def collect_known_utterance_domains(domain_agents: Mapping[str, DomainAgent]) -> KnownUtteranceDomains:
    domains: KnownUtteranceDomains = {}
    for domain_name, domain_agent in domain_agents.items():
        for utterance in domain_agent.known_utterances():
            if not isinstance(utterance, str) or not utterance.strip():
                raise ValueError(f"known utterance for {domain_name} must be a non-empty string")
            key = _known_utterance_key(utterance)
            if key in domains:
                raise ValueError(f"duplicate known utterance after normalization: {utterance!r}")
            domains[key] = domain_name
    return domains


def known_utterance_task(user_input: str, known_utterance_domains: Mapping[str, str]) -> RoutedTask | None:
    domain = known_utterance_domains.get(_known_utterance_key(user_input))
    if domain is None:
        return None
    return {
        "id": "t1",
        "domain": domain,
        "request": user_input,
        "depends_on": [],
    }


def _known_utterance_key(value: str) -> str:
    return ascii_fold(normalize_text(value))
