"""Command registry — declarative table of every `hive` subcommand.

The registry is populated lazily inside `__init__.py:_populate_registry()`
once the handler symbols exist at module level. Keeping the registry in a
separate module lets tests and tooling introspect the CLI surface
without importing the full `__init__.py` (which has heavy I/O deps).

Handlers are referenced by NAME (string), not by direct callable, so that
test-time `monkeypatch.setattr(cli, "_version", ...)` (which rebinds the
module-level symbol) takes effect at dispatch time. This is the same
pattern used in the original monolithic `__init__.py:main()`.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CommandSpec:
    name: str
    help: str
    handler_name: str
    args: tuple = ()
    subcommands: dict = field(default_factory=dict)
    category: str = "general"


REGISTRY: dict[str, CommandSpec] = {}


def register(spec: CommandSpec) -> None:
    REGISTRY[spec.name] = spec
