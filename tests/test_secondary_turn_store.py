from hive.gateway.channels.secondary_turn_store import CONFIRMED, REQUIRES_REVIEW, SecondaryChannelTurnStore


def test_secondary_store_deduplicates_and_confirms_without_ids_or_content(tmp_path):
    store = SecondaryChannelTurnStore(tmp_path / "state.sqlite")
    assert store.accept(provider="slack", event_id="private-event")
    assert not store.accept(provider="slack", event_id="private-event")
    worker = store.claim_processing(provider="slack", event_id="private-event")
    assert worker and store.begin_delivery(provider="slack", event_id="private-event", worker_id=worker)
    assert store.confirm_delivery(provider="slack", event_id="private-event", worker_id=worker, receipt="private-receipt")
    assert store.summary()[CONFIRMED] == 1
    assert "private" not in str(store._db.execute("SELECT * FROM secondary_channel_turns").fetchone())
    store.close()


def test_secondary_store_restart_quarantines_without_replay(tmp_path):
    path = tmp_path / "state.sqlite"
    store = SecondaryChannelTurnStore(path)
    assert store.accept(provider="discord", event_id="event")
    store.close()
    store = SecondaryChannelTurnStore(path)
    assert store.recover_after_restart() == 1
    assert store.summary()[REQUIRES_REVIEW] == 1
    assert store.claim_processing(provider="discord", event_id="event") is None
    store.close()
