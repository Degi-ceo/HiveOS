"""
client.py — A2A HTTP client (SPRINT_6 P-D, issue #72).

Minimal JSON-RPC-style HTTP client with timeout + retry on transient errors
(5xx, connection errors). On a 2xx with an envelope error response, returns
the response (callers check ``response.is_error()``).
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from hive.agents.a2a.envelope import A2ARequest, A2AResponse


class A2AConnectionError(Exception):
    """Raised when the client cannot reach the remote A2A endpoint after retries."""


class A2AClient:
    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        backoff: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise A2AConnectionError(f"endpoint must be http(s): got {endpoint!r}")
        self._endpoint = endpoint
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._backoff = max(0.0, float(backoff))
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "A2AClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def call(self, method: str, params: dict[str, Any] | None = None,
                   *, request_id: str | None = None) -> A2AResponse:
        """Send an A2A request and return the parsed response envelope.

        Retries on connection errors and 5xx; returns 4xx envelopes as-is.
        """
        req = A2ARequest(method=method, params=params or {}, id=request_id or "")
        body = req.model_dump_json()
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.post(self._endpoint, content=body,
                                               headers={"Content-Type": "application/json"})
            except httpx.HTTPError as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff * (attempt + 1))
                    continue
                raise A2AConnectionError(
                    f"a2a call failed after {self._max_retries + 1} attempts: {exc}"
                ) from exc
            if resp.status_code < 500:
                return A2AResponse.model_validate(resp.json())
            if attempt >= self._max_retries:
                raise A2AConnectionError(f"server error {resp.status_code}")
            await asyncio.sleep(self._backoff * (attempt + 1))
        raise A2AConnectionError(  # pragma: no cover — defensive; loop above always exits via return/raise
            f"a2a call exhausted {self._max_retries + 1} attempts without a final envelope"
        )
