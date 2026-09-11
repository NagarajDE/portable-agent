"""
ENGINE / tools / registry -- the ONLY place tool names map to their factories.

A factory is `Callable[[dict], Tool]`: it takes the pack's per-tool `params` and returns a
constructed Tool. Adapters self-register at import time by calling `register(...)`. Nothing
here imports the adapters (that would be a cycle) -- the orchestrator imports the builtin
adapter modules once, on demand, so registration has happened before the first build.
"""
from __future__ import annotations

from typing import Callable

from engine.tools.base import Tool

ToolFactory = Callable[[dict], Tool]


class UnknownToolError(KeyError):
    """Raised when a pack references a tool `type` that no adapter has registered."""


_REGISTRY: dict[str, ToolFactory] = {}


def register(name: str, factory: ToolFactory) -> None:
    """Register (or replace) the factory for a tool type. Called at adapter import time."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("tool type name must be a non-empty string")
    _REGISTRY[name.strip().lower()] = factory


def known_tools() -> list[str]:
    """Sorted list of registered tool types -- for discovery / error messages."""
    return sorted(_REGISTRY)


def build_tool(tool_type: str, params: dict | None = None) -> Tool:
    """Construct a Tool by its registered type. Raises UnknownToolError (with the known
    types) so a typo in a pack's config fails clearly rather than silently doing nothing."""
    key = (tool_type or "").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise UnknownToolError(
            f"unknown tool type {tool_type!r}; registered types: {known_tools() or '(none)'}")
    return factory(dict(params or {}))
