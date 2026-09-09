"""Task-local correlation context for autonomous HiveOS runs."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_RUN_ID: ContextVar[str] = ContextVar("hive_run_id", default="")


def new_run_id() -> str:
    """Return a globally unique identifier for one heartbeat tick."""
    return str(uuid.uuid4())


def current_run_id() -> str:
    """Return the run bound to the current async task, or an empty string."""
    return _RUN_ID.get()


@contextmanager
def bind_run_id(run_id: str) -> Iterator[str]:
    """Bind ``run_id`` for this execution context and restore it afterwards."""
    normalized = str(run_id or "")
    token = _RUN_ID.set(normalized)
    try:
        yield normalized
    finally:
        _RUN_ID.reset(token)
