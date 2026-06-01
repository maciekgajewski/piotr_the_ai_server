from __future__ import annotations

from ai_server.domain_agents.interfaces import DomainTask
from ai_server.utils.text import normalize_text


KNOWN_UTTERANCE_TASKS: dict[str, DomainTask] = {
    normalize_text("Która godzina?"): {
        "id": "t1",
        "domain": "time",
        "command": {"query": "Która godzina?"},
        "depends_on": [],
        "status": "ready",
        "clarification_question": None,
    },
}
