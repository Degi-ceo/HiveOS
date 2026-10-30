"""The operator memory view should show facts rather than storage envelopes."""

import asyncio
import json

from hive.surfaces.cli import _memory_search


def test_memory_search_renders_trusted_mnemosyne_fact(monkeypatch, capsys):
    envelope = "[hive-memory-v2] " + json.dumps({
        "kind": "owner-memory", "topic": "Smoke marker", "content": "Persisted fact",
        "trust": "trusted", "revision": "private-revision",
    })

    class FakeMemory:
        def recall(self, query, limit):
            assert query == "Smoke marker"
            assert limit == 20
            return [{"content": envelope, "source": "owner-cli", "veracity": "stated"}]

    class FakeHive:
        memory = FakeMemory()

        async def aclose(self):
            pass

    monkeypatch.setattr("hive.runtime.HiveOS.build", lambda **_kwargs: FakeHive())
    assert asyncio.run(_memory_search("Smoke marker")) == 0
    output = capsys.readouterr().out
    assert "[trusted] Smoke marker: Persisted fact" in output
    assert "hive-memory-v2" not in output
    assert "private-revision" not in output


def test_memory_search_strips_control_characters_and_unknown_trust(monkeypatch, capsys):
    class FakeMemory:
        def recall(self, _query, limit):
            assert limit == 20
            return [{"topic": "first\x1b[31m\nline", "content": "safe\r\nvalue", "trust": "trusted\x1b[0m"}]

    class FakeHive:
        memory = FakeMemory()

        async def aclose(self):
            pass

    monkeypatch.setattr("hive.runtime.HiveOS.build", lambda **_kwargs: FakeHive())
    assert asyncio.run(_memory_search("first")) == 0
    output = capsys.readouterr().out
    assert "[unknown]" in output
    assert "safe  value" in output
    assert "\x1b" not in output


def test_memory_search_preserves_every_line_of_legacy_or_local_content(monkeypatch, capsys):
    class FakeMemory:
        def recall(self, _query, limit):
            assert limit == 20
            return [{"topic": "Local", "content": "[fact] Local: first\nsecond", "trust": "untrusted"}]

    class FakeHive:
        memory = FakeMemory()

        async def aclose(self):
            pass

    monkeypatch.setattr("hive.runtime.HiveOS.build", lambda **_kwargs: FakeHive())
    assert asyncio.run(_memory_search("Local")) == 0
    output = capsys.readouterr().out
    assert "first second" in output
