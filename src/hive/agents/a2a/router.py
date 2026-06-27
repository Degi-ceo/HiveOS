"""
router.py — routes A2A envelopes to local handlers or remote URIs (SPRINT_6 P-D).

Local handlers are async callables registered by name. Remote URIs are
string handlers prefixed with ``http://`` or ``https://``; routing to a
remote URI returns the URL itself so the caller can dispatch via A2AClient.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from hive.agents.a2a.envelope import A2AError, A2AResponse

A2AHandler = Callable[[dict[str, Any]], Awaitable[Any]]

_LOCAL: dict[str, A2AHandler] = {}
_REMOTES: dict[str, str] = {}


class A2ARoutingError(Exception):
    """Raised when an envelope cannot be routed to a local or remote handler."""


def register(method: str, handler: A2AHandler) -> None:
    """Register a local async handler for ``method`` (e.g. ``"researcher.run"``)."""
    _LOCAL[method] = handler


def register_remote(method: str, uri: str) -> None:
    """Register a remote URI for ``method``. Routing returns the URI string."""
    if not uri.startswith(("http://", "https://")):
        raise A2ARoutingError(f"remote uri must be http(s): got {uri!r}")
    _REMOTES[method] = uri


def unregister(method: str) -> None:
    """Remove a method from both local and remote registries (no-op if absent)."""
    _LOCAL.pop(method, None)
    _REMOTES.pop(method, None)


def registered_methods() -> list[str]:
    """Return the sorted union of locally and remotely registered method names."""
    return sorted(set(_LOCAL) | set(_REMOTES))


async def route(request_id: str, method: str,
                params: dict[str, Any]) -> A2AResponse:
    """Route an envelope: local first, then remote, else -32601 method_not_found."""
    if method in _LOCAL:
        try:
            result = await _LOCAL[method](params)
            return A2AResponse(id=request_id, result=result, error=None)
        except Exception as exc:  # noqa: BLE001 - normalise to envelope error
            return A2AResponse(
                id=request_id,
                error=A2AError(code=-32603, message=f"internal: {exc}",
                               data={"type": type(exc).__name__}),
            )
    if method in _REMOTES:
        return A2AResponse(
            id=request_id, result={"remote_uri": _REMOTES[method]}, error=None,
        )
    return A2AResponse(
        id=request_id,
        error=A2AError(code=-32601, message=f"method not found: {method}",
                       data={"method": method}),
    )
