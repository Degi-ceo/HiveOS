"""Small, policy-free values shared across the tool-dispatch boundary.

Workers need to represent a supervisor's already-authorized tool outcome, but
must not import :mod:`hive.tools.executor`: importing that module also loads the
protected approval gate. Keep this vocabulary dependency-free so a sandboxed
worker can use the IPC proxy without gaining access to approval policy.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from hive.core.types import ToolResult


class DispatchStatus(str, enum.Enum):
    OK = "ok"
    PENDING = "pending_approval"
    ERROR = "error"


@dataclass(slots=True)
class ToolDispatch:
    status: DispatchStatus
    result: ToolResult | None = None
    approval_id: str | None = None
    error: str | None = None
