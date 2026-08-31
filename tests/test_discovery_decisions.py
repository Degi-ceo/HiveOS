from hive.memory.discovery_decisions import DiscoveryDecisionStore
import pytest

def test_record_is_idempotent_and_latest(tmp_path):
    store = DiscoveryDecisionStore(tmp_path / "state.sqlite", clock=lambda: 10.0)
    first = store.record(capability_key="memory", phase="discovery", outcome="found", idempotency_key="same", candidate_name="Mnemosyne", candidate_source="github")
    duplicate = store.record(capability_key="memory", phase="discovery", outcome="found", idempotency_key="same", candidate_name="ignored")
    assert first == duplicate
    assert store.latest("memory") == first

def test_adoption_requires_verified_immutable_pin(tmp_path):
    store = DiscoveryDecisionStore(tmp_path / "state.sqlite")
    with pytest.raises(ValueError):
        store.record(capability_key="memory", phase="adoption", outcome="adopted", idempotency_key="bad", audit_status="passed")
    item = store.record(capability_key="memory", phase="adoption", outcome="adopted", idempotency_key="good", audit_status="passed", pinned_version="3.15.1", rationale="verified")
    assert item.pinned_version == "3.15.1"

def test_record_bounds_sensitive_fields(tmp_path):
    store = DiscoveryDecisionStore(tmp_path / "state.sqlite")
    item = store.record(capability_key="x", phase="discovery", outcome="found", idempotency_key="x", candidate_url="a" * 1000)
    assert len(item.candidate_url) == 500


def test_list_is_scoped_and_ordered(tmp_path):
    clock = iter((10.0, 20.0, 30.0)).__next__
    store = DiscoveryDecisionStore(tmp_path / "state.sqlite", clock=clock)
    first = store.record(capability_key="memory", phase="discovery", outcome="found", idempotency_key="first")
    second = store.record(capability_key="memory", phase="discovery", outcome="no_match", idempotency_key="second")
    store.record(capability_key="tools", phase="discovery", outcome="found", idempotency_key="other")

    assert store.list_for_capability("memory") == [second, first]
def test_record_strips_url_query_and_credentials(tmp_path):
    store = DiscoveryDecisionStore(tmp_path / "state.sqlite")
    clean = store.record(capability_key="x", phase="discovery", outcome="found", idempotency_key="clean", candidate_url="https://example.com/repo?token=private#fragment")
    rejected = store.record(capability_key="y", phase="discovery", outcome="found", idempotency_key="rejected", candidate_url="https://user:password@example.com/repo")

    assert clean.candidate_url == "https://example.com/repo"
    assert rejected.candidate_url == ""
