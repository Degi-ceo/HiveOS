"""hive.agents.a2a — A2A protocol envelope (SPRINT_6 P-D, issue #72)."""
from hive.agents.a2a.client import A2AClient, A2AConnectionError
from hive.agents.a2a.envelope import A2AError, A2ARequest, A2AResponse
from hive.agents.a2a.router import (
    A2ARoutingError,
    register,
    register_remote,
    registered_methods,
    route,
    unregister,
)

__all__ = [
    "A2AClient",
    "A2AConnectionError",
    "A2AError",
    "A2ARequest",
    "A2AResponse",
    "A2ARoutingError",
    "register",
    "register_remote",
    "registered_methods",
    "route",
    "unregister",
]
