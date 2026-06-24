from __future__ import annotations

import copy
from collections.abc import Mapping

from ai_server.domain_agents.interfaces import DomainAgent, DomainTask
from ai_server.utils.text import ascii_fold, normalize_text


KnownUtteranceTasks = dict[str, DomainTask]


def collect_known_utterance_tasks(domain_agents: Mapping[str, DomainAgent]) -> KnownUtteranceTasks:
    tasks: KnownUtteranceTasks = {}
    for domain_name, domain_agent in domain_agents.items():
        for utterance, task in domain_agent.known_utterances().items():
            if not isinstance(utterance, str) or not utterance.strip():
                raise ValueError(f"known utterance for {domain_name} must be a non-empty string")
            if not isinstance(task, dict):
                raise ValueError(f"known utterance task for {domain_name}.{utterance!r} must be a mapping")
            key = _known_utterance_key(utterance)
            if key in tasks:
                raise ValueError(f"duplicate known utterance after normalization: {utterance!r}")
            tasks[key] = task
    return tasks


def known_utterance_task(user_input: str, known_utterance_tasks: Mapping[str, DomainTask]) -> DomainTask | None:
    task = known_utterance_tasks.get(_known_utterance_key(user_input))
    if task is None:
        return None
    task = copy.deepcopy(task)
    command = task.get("command")
    if isinstance(command, dict) and "query" in command:
        command["query"] = user_input
    return task


def _known_utterance_key(value: str) -> str:
    return ascii_fold(normalize_text(value))
