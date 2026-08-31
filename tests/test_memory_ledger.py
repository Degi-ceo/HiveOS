import pytest

from hive.memory.ledger import MemoryLedger
from hive.memory.mnemosyne_projector import MnemosyneProjector
from hive.memory.obsidian_projector import ObsidianShadowProjector


def test_ledger_versions_and_enqueues_projections_atomically(tmp_path):
    ledger = MemoryLedger(tmp_path / "ledger.db", clock=lambda: 100.0)
    first = ledger.remember(kind="lesson", stable_key="fix:task", content="Errors fail.", source="test", idempotency_key="evt-1")
    second = ledger.remember(kind="lesson", stable_key="fix:task", content="Errors never finish tasks.", source="test", idempotency_key="evt-2")
    assert first.memory_id == second.memory_id
    assert second.version == 2
    assert ledger.get_current(first.memory_id).content == "Errors never finish tasks."
    assert len(ledger.pending_projections("mnemosyne")) == 2
    assert len(ledger.pending_projections("obsidian")) == 2


def test_ledger_idempotency_prevents_duplicate_versions_and_outbox(tmp_path):
    ledger = MemoryLedger(tmp_path / "ledger.db")
    one = ledger.remember(kind="fact", stable_key="owner", content="Kamil", source="user", idempotency_key="same")
    two = ledger.remember(kind="fact", stable_key="owner", content="changed", source="user", idempotency_key="same")
    assert one == two
    assert len(ledger.pending_projections("obsidian")) == 1


def test_shadow_projector_writes_versioned_note_and_manifest(tmp_path):
    ledger = MemoryLedger(tmp_path / "ledger.db")
    memory = ledger.remember(kind="lesson", stable_key="fix:task", content="Errors fail tasks.", source="test", idempotency_key="evt-1")
    projector = ObsidianShadowProjector(ledger, tmp_path / "Hive-Shadow")

    result = projector.project_pending()

    assert [item.state for item in result] == ["applied"]
    note = tmp_path / "Hive-Shadow" / "40 Lessons" / f"{memory.memory_id}.md"
    assert "hive_memory_id" in note.read_text(encoding="utf-8")
    assert "Errors fail tasks." in note.read_text(encoding="utf-8")
    assert (tmp_path / "Hive-Shadow" / "_System" / "manifests" / f"{memory.memory_id}.json").exists()
    assert ledger.pending_projections("obsidian") == []


def test_shadow_projector_preserves_manual_edits_for_reconciliation(tmp_path):
    ledger = MemoryLedger(tmp_path / "ledger.db")
    first = ledger.remember(kind="fact", stable_key="owner", content="Kamil", source="user", idempotency_key="evt-1")
    projector = ObsidianShadowProjector(ledger, tmp_path / "Hive-Shadow")
    projector.project_pending()
    note = tmp_path / "Hive-Shadow" / "50 Knowledge" / f"{first.memory_id}.md"
    note.write_text("manual note", encoding="utf-8")
    ledger.remember(kind="fact", stable_key="owner", content="Kamil Side Hustle", source="user", idempotency_key="evt-2")

    results = projector.project_pending()

    assert [item.state for item in results] == ["conflict"]
    assert note.read_text(encoding="utf-8") == "manual note"
    assert len(ledger.pending_projections("obsidian")) == 1


class _FakeMnemosyne:
    def __init__(self):
        self.remembered = []
        self.invalidated = []
        self.fail_invalidation = False

    def remember(self, content, **kwargs):
        self.remembered.append((content, kwargs))
        return f"mnemo-{len(self.remembered)}"

    def invalidate(self, memory_id, replacement_id=None):
        self.invalidated.append((memory_id, replacement_id))
        return not self.fail_invalidation


def test_mnemosyne_projector_supersedes_old_projection(tmp_path):
    ledger = MemoryLedger(tmp_path / "ledger.db")
    sink = _FakeMnemosyne()
    projector = MnemosyneProjector(ledger, sink)
    first = ledger.remember(kind="fact", stable_key="owner", content="Kamil", source="user", idempotency_key="evt-1")
    projector.project_pending()
    second = ledger.remember(kind="fact", stable_key="owner", content="Kamil Side Hustle", source="user", idempotency_key="evt-2")

    result = projector.project_pending()

    assert [item.state for item in result] == ["applied"]
    assert sink.invalidated == [("mnemo-1", "mnemo-2")]
    assert sink.remembered[1][1]["metadata"]["hive_version"] == second.version
    assert ledger.projection_binding("mnemosyne", first.memory_id, 2) == "mnemo-2"


def test_mnemosyne_projector_retries_invalidation_without_duplicate_remember(tmp_path):
    ledger = MemoryLedger(tmp_path / "ledger.db")
    sink = _FakeMnemosyne()
    projector = MnemosyneProjector(ledger, sink)
    ledger.remember(kind="fact", stable_key="owner", content="Kamil", source="user", idempotency_key="evt-1")
    projector.project_pending()
    ledger.remember(kind="fact", stable_key="owner", content="Kamil Side Hustle", source="user", idempotency_key="evt-2")
    sink.fail_invalidation = True
    assert projector.project_pending()[0].state == "pending"

    sink.fail_invalidation = False
    assert projector.project_pending()[0].state == "applied"
    assert len(sink.remembered) == 2


def test_local_provider_projects_deliberate_memory_to_shadow_vault(tmp_path):
    from hive.memory.local import LocalMemoryProvider

    ledger = MemoryLedger(tmp_path / "state.sqlite")
    provider = LocalMemoryProvider(
        tmp_path / "state.sqlite",
        ledger=ledger,
        shadow_root=tmp_path / "hive vault" / "Hive-Shadow",
    )

    provider.learn("fact", "owner", "Kamil", "user")

    rows = provider.recall("owner")
    assert rows and rows[0]["content"] == "Kamil"
    assert ledger.pending_projections("obsidian") == []
    assert any((tmp_path / "hive vault" / "Hive-Shadow").rglob("*.md"))


def test_mnemosyne_provider_projects_ledger_versions_and_shadow(tmp_path):
    from hive.memory.mnemosyne_provider import HiveMnemosyneProvider, _HiveMnemosyneInner

    ledger = MemoryLedger(tmp_path / "state.sqlite")
    inner = _HiveMnemosyneInner()
    sink = _FakeMnemosyne()
    inner._beam = sink
    provider = HiveMnemosyneProvider(
        inner,
        ledger=ledger,
        shadow_root=tmp_path / "hive vault" / "Hive-Shadow",
    )

    provider.learn("fact", "owner", "Kamil", "user")
    provider.learn("fact", "owner", "Kamil Side Hustle", "user")

    assert len(sink.remembered) == 2
    assert sink.invalidated == [("mnemo-1", "mnemo-2")]
    assert ledger.pending_projections("mnemosyne") == []
    assert ledger.pending_projections("obsidian") == []
    assert any((tmp_path / "hive vault" / "Hive-Shadow").rglob("*.md"))
def test_shadow_projector_recovers_after_note_written_before_manifest(tmp_path, monkeypatch):
    ledger = MemoryLedger(tmp_path / "ledger.db")
    ledger.remember(kind="lesson", stable_key="crash-safe", content="Retry safely.",
                    source="test", idempotency_key="evt-crash")
    projector = ObsidianShadowProjector(ledger, tmp_path / "Hive-Shadow")
    original_write = projector._atomic_write
    writes = [0]

    def fail_manifest_once(path, content):
        writes[0] += 1
        if writes[0] == 2:
            raise OSError("simulated crash before manifest replacement")
        original_write(path, content)

    monkeypatch.setattr(projector, "_atomic_write", fail_manifest_once)
    assert [item.state for item in projector.project_pending()] == ["pending"]
    assert len(ledger.pending_projections("obsidian")) == 1

    monkeypatch.setattr(projector, "_atomic_write", original_write)
    assert [item.state for item in projector.project_pending()] == ["applied"]
    assert ledger.pending_projections("obsidian") == []

def test_projection_claims_are_exclusive_and_fenced(tmp_path):
    clock = [100.0]
    db_path = tmp_path / "ledger.db"
    first = MemoryLedger(db_path, clock=lambda: clock[0])
    second = MemoryLedger(db_path, clock=lambda: clock[0])
    first.remember(kind="fact", stable_key="owner", content="Kamil", source="test",
                   idempotency_key="evt-claim", targets=("mnemosyne",))

    claimed = first.claim_pending_projections("mnemosyne", worker_id="worker-a")

    assert len(claimed) == 1
    assert second.claim_pending_projections("mnemosyne", worker_id="worker-b") == []
    assert first.mark_projected(claimed[0]["operation_id"], worker_id="worker-b") is False
    assert first.mark_projected(claimed[0]["operation_id"], worker_id="worker-a") is True
    assert first.pending_projections("mnemosyne") == []


def test_expired_external_projection_is_quarantined_not_replayed(tmp_path):
    clock = [100.0]
    db_path = tmp_path / "ledger.db"
    first = MemoryLedger(db_path, clock=lambda: clock[0])
    second = MemoryLedger(db_path, clock=lambda: clock[0])
    first.remember(kind="fact", stable_key="owner", content="Kamil", source="test",
                   idempotency_key="evt-unsafe", targets=("mnemosyne",))
    assert len(first.claim_pending_projections("mnemosyne", worker_id="worker-a", lease_seconds=5)) == 1

    clock[0] = 106.0

    assert second.claim_pending_projections("mnemosyne", worker_id="worker-b") == []
    row = second._db.execute("SELECT state FROM memory_projection_outbox").fetchone()
    assert row["state"] == "requires_review"


def test_expired_obsidian_projection_is_recovered_and_stale_worker_is_fenced(tmp_path):
    clock = [100.0]
    db_path = tmp_path / "ledger.db"
    first = MemoryLedger(db_path, clock=lambda: clock[0])
    second = MemoryLedger(db_path, clock=lambda: clock[0])
    first.remember(kind="fact", stable_key="owner", content="Kamil", source="test",
                   idempotency_key="evt-safe", targets=("obsidian",))
    first_claim = first.claim_pending_projections("obsidian", worker_id="worker-a", lease_seconds=5)[0]

    clock[0] = 106.0
    second_claim = second.claim_pending_projections("obsidian", worker_id="worker-b")[0]

    assert first.mark_projected(first_claim["operation_id"], worker_id="worker-a") is False
    assert second.mark_projected(second_claim["operation_id"], worker_id="worker-b") is True


def test_projection_claim_waits_for_prior_version_of_same_memory(tmp_path):
    ledger = MemoryLedger(tmp_path / "ledger.db")
    first = ledger.remember(kind="fact", stable_key="owner", content="Kamil", source="test",
                            idempotency_key="evt-v1", targets=("obsidian",))
    second = ledger.remember(kind="fact", stable_key="owner", content="Kamil Side Hustle", source="test",
                             idempotency_key="evt-v2", targets=("obsidian",))

    claimed = ledger.claim_pending_projections("obsidian", worker_id="worker-a")

    assert [(item["memory_id"], item["version"]) for item in claimed] == [(first.memory_id, 1)]
    assert ledger.mark_projected(claimed[0]["operation_id"], worker_id="worker-a") is True
    claimed = ledger.claim_pending_projections("obsidian", worker_id="worker-a")
    assert [(item["memory_id"], item["version"]) for item in claimed] == [(second.memory_id, 2)]

def test_shadow_projector_continues_after_one_note_write_failure(tmp_path, monkeypatch):
    ledger = MemoryLedger(tmp_path / "ledger.db")
    ledger.remember(kind="lesson", stable_key="first", content="First.", source="test",
                    idempotency_key="evt-first", targets=("obsidian",))
    ledger.remember(kind="lesson", stable_key="second", content="Second.", source="test",
                    idempotency_key="evt-second", targets=("obsidian",))
    projector = ObsidianShadowProjector(ledger, tmp_path / "Hive-Shadow")
    original_write = projector._atomic_write
    failed = [False]

    def fail_one_note(path, content):
        if not failed[0] and path.suffix == ".md":
            failed[0] = True
            raise OSError("simulated note failure")
        original_write(path, content)

    monkeypatch.setattr(projector, "_atomic_write", fail_one_note)

    assert sorted(item.state for item in projector.project_pending()) == ["applied", "pending"]
    assert len(ledger.pending_projections("obsidian")) == 1
