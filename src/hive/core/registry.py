"""
registry.py — generic decorator registry with per-subclass isolation.

Ported from OpenJarvis's RegistryBase[T] pattern (see
docs/references/OPENJARVIS_REFERENCE.md §3.1). Every extensible component in
HiveOS — providers, agents, tools, memory backends, channels — registers here.
Each typed subclass gets its OWN entry storage via __init_subclass__, so
registrations never leak between registries.

    from hive.core.registry import RegistryBase

    class ToolRegistry(RegistryBase["BaseTool"]):
        pass

    @ToolRegistry.register("read_file")
    class ReadFile(BaseTool): ...
"""
from __future__ import annotations

from typing import Callable, Generic, ItemsView, KeysView, TypeVar

T = TypeVar("T")


class RegistryBase(Generic[T]):
    """Generic registry. Subclass per component type; storage is isolated."""

    _entries: dict[str, T]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Fresh dict per subclass — prevents cross-registry contamination.
        cls._entries = {}

    @classmethod
    def register(cls, key: str) -> Callable[[T], T]:
        """Decorator: register a value (class/func/object) under *key*."""

        def deco(value: T) -> T:
            cls.register_value(key, value)
            return value

        return deco

    @classmethod
    def register_value(cls, key: str, value: T) -> None:
        if key in cls._entries:
            raise ValueError(f"{cls.__name__}: duplicate key {key!r}")
        cls._entries[key] = value

    @classmethod
    def get(cls, key: str) -> T:
        return cls._entries[key]

    @classmethod
    def create(cls, key: str, *args: object, **kwargs: object) -> T:
        """Look up a registered class and instantiate it."""
        entry = cls._entries[key]
        return entry(*args, **kwargs)  # type: ignore[operator]

    @classmethod
    def contains(cls, key: str) -> bool:
        return key in cls._entries

    @classmethod
    def keys(cls) -> KeysView[str]:
        return cls._entries.keys()

    @classmethod
    def items(cls) -> ItemsView[str, T]:
        return cls._entries.items()

    @classmethod
    def clear(cls) -> None:
        """Remove all entries (for tests)."""
        cls._entries.clear()
