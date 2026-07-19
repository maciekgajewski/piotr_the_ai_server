from __future__ import annotations

import itertools
import uuid


class ProcessIdFactory:
    """Issue opaque IDs that cannot repeat during this process lifetime."""

    def __init__(self) -> None:
        self._process_nonce = uuid.uuid4().hex
        self._sequence = itertools.count()

    def new_id(self, prefix: str | None = None) -> str:
        value = f"{self._process_nonce}-{next(self._sequence)}"
        return f"{prefix}-{value}" if prefix else value


PROCESS_ID_FACTORY = ProcessIdFactory()


def new_id(prefix: str | None = None) -> str:
    return PROCESS_ID_FACTORY.new_id(prefix)
