from hive.memory.ledger import MemoryLedger


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
