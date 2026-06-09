"""HiveOS — the system that hosts the agent Hive. Python-first runtime."""
from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "0.2.0-dev"
__all__ = ["HiveOS"]

if TYPE_CHECKING:  # type hints only; no runtime import
    from hive.runtime import HiveOS


def __getattr__(name: str):
    """Lazy export (PEP 562): `from hive import HiveOS` works, while plain
    `import hive` (and `import hive.core`) stays side-effect free — importing the
    runtime would pull in every layer and break the core-is-a-leaf invariant."""
    if name == "HiveOS":
        from hive.runtime import HiveOS
        return HiveOS
    raise AttributeError(f"module 'hive' has no attribute {name!r}")
