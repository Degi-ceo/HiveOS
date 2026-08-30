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
