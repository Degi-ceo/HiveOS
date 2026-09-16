"""Task-local correlation context for autonomous HiveOS runs."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_RUN_ID: ContextVar[str] = ContextVar("hive_run_id", default="")
_DELEGATION_ID: ContextVar[str] = ContextVar("hive_delegation_id", default="")


def new_run_id() -> str:
    """Return a globally unique identifier for one heartbeat tick."""
    return str(uuid.uuid4())


def current_run_id() -> str:
    """Return the run bound to the current async task, or an empty string."""
    return _RUN_ID.get()


def current_delegation_id() -> str:
    """Return the active specialist delegation, if the task is a leaf worker."""
    return _DELEGATION_ID.get()


@contextmanager
def bind_run_id(run_id: str) -> Iterator[str]:
    """Bind ``run_id`` for this execution context and restore it afterwards."""
    normalized = str(run_id or "")
    token = _RUN_ID.set(normalized)
    try:
        yield normalized
    finally:
        _RUN_ID.reset(token)


@contextmanager
def bind_delegation_id(delegation_id: str) -> Iterator[str]:
    """Bind one opaque delegation ID while a leaf specialist executes."""
    normalized = str(delegation_id or "")
    token = _DELEGATION_ID.set(normalized)
    try:
        yield normalized
    finally:
        _DELEGATION_ID.reset(token)
