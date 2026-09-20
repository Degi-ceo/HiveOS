"""M11 operator delegation projections remain read-only and redacted."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from starlette.testclient import TestClient

from hive.core.config import HiveConfig
from hive.gateway.app import create_app
from hive.runtime import HiveOS
from hive.surfaces import cli


class _Router:
    async def complete(self, *args, **kwargs):
        raise AssertionError("operator delegation views must not invoke a model")

    async def aclose(self):
        return None


def test_delegation_gateway_is_authenticated_read_only_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_STATE_HOST_ID", "m11-gateway-machine")
    monkeypatch.setenv("HIVE_PRODUCTION", "false")
    cfg = replace(
        HiveConfig.from_env(root=tmp_path, load_dotenv=False),
        secret="agent-secret", host="127.0.0.1",
    )
    hive = HiveOS.build(cfg, router=_Router())
    root = hive.delegation_ledger.create(
        parent_run_id="private-root-run", child_run_id="coordinator-run", role="coordinator",
    )
    assert hive.delegation_ledger.claim(root.id) == 1
    hive.delegation_ledger.create(
        parent_run_id="coordinator-run", child_run_id="private-research-run", role="researcher",
        parent_delegation_id=root.id,
    )
    try:
        with TestClient(create_app(hive)) as client:
            assert client.get("/delegations").status_code == 401
            listed = client.get("/delegations", headers={"X-Hive-Token": "agent-secret"})
            assert listed.status_code == 200
            assert root.id in {item["delegation_id"] for item in listed.json()["delegations"]}
            detail = client.get(f"/delegations/{root.id}", headers={"X-Hive-Token": "agent-secret"})
            assert detail.status_code == 200 and detail.json()["children"] == 1
            tree = client.get(f"/delegations/{root.id}/tree", headers={"X-Hive-Token": "agent-secret"})
            assert tree.status_code == 200 and tree.json()["node_count"] == 2
            assert client.post("/delegations", headers={"X-Hive-Token": "agent-secret"}).status_code == 405
            rendered = str({"list": listed.json(), "detail": detail.json(), "tree": tree.json()})
            for private in ("private-root-run", "private-research-run", "agent-secret"):
                assert private not in rendered
    finally:
        asyncio.run(hive.aclose())


def test_delegation_cli_uses_normal_token_only_for_reading(monkeypatch, capsys):
    cfg = SimpleNamespace(secret="agent-secret")
    calls = []

    def request(_cfg, method, path, *, credential, body=None, approver=False):
        calls.append((method, path, credential, body, approver))
        if path.endswith("/tree"):
            return {"root": {"id": "delegation-1", "role": "coordinator", "state": "running",
                             "depth": 0, "attempts": 1, "max_attempts": 1, "children": []},
                    "node_count": 1, "truncated": False}
        return {"delegations": [{"delegation_id": "delegation-1", "role": "coordinator",
                                  "state": "running", "depth": 0, "attempts": 1,
                                  "max_attempts": 1, "children": 0}]}

    monkeypatch.setattr(HiveConfig, "from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(cli, "_gateway_request", request)
    assert cli.main(["agents"]) == 0
    assert cli.main(["agents", "tree", "delegation-1"]) == 0
    assert calls == [
        ("GET", "/delegations", "agent-secret", None, False),
        ("GET", "/delegations/delegation-1/tree", "agent-secret", None, False),
    ]
    assert "HiveOS Delegation Tree" in capsys.readouterr().out
