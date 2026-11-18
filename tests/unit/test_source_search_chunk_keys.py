"""Search proof probes return exact live keys without fetching text."""

from __future__ import annotations

import pytest

from server.knowledge.store import source_candidates as store
from tests.unit.ch_fake import RecordingCH

KEY = ("acl-" + "1" * 24, "gdrive:file#source", 3, 9, 0)


def row(key=KEY):
    return dict(
        zip(("namespace", "source_uri", "chunk_idx", "doc_version", "revision"), key, strict=True)
    )


@pytest.mark.parametrize("embedded_only", [False, True])
async def test_key_probe_scopes_exact_live_model_keys_and_never_fetches_text(
    monkeypatch, embedded_only
):
    ch = RecordingCH().on("FROM loom.chunks", [row()])
    ch.install(monkeypatch, "server.knowledge.store.source_candidates")
    assert await store.chunk_keys("acme", "fake-model", [KEY], embedded_only=embedded_only) == [KEY]
    call = ch.calls[-1]
    assert call.params == {
        "org": "acme",
        "nss": [KEY[0]],
        "model": "fake-model",
        "keys": [KEY],
        "limit": 2,
    }
    selected = call.sql.split(" FROM ", 1)[0]
    assert "content" not in selected and "context" not in selected and "tags" not in selected
    assert "embedding_model = {model:String}" in call.sql
    assert ("embedding_dim > 0" in call.sql) is embedded_only
    assert "doc_version" in selected and "AS revision" in selected
    assert "org = {org:String}" in call.sql and "LIMIT {limit:UInt32}" in call.sql


async def test_duplicate_physical_keys_are_preserved_for_proof_ambiguity_detection(monkeypatch):
    ch = RecordingCH().on("FROM loom.chunks", [row(), row()])
    ch.install(monkeypatch, "server.knowledge.store.source_candidates")
    assert await store.chunk_keys("acme", "fake-model", [KEY]) == [KEY, KEY]


async def test_empty_key_probe_has_no_database_io(monkeypatch):
    ch = RecordingCH()
    ch.install(monkeypatch, "server.knowledge.store.source_candidates")
    assert await store.chunk_keys("acme", "fake-model", []) == []
    assert ch.calls == []


async def test_key_probe_rejects_more_than_one_bounded_batch(monkeypatch):
    ch = RecordingCH()
    ch.install(monkeypatch, "server.knowledge.store.source_candidates")
    with pytest.raises(ValueError):
        await store.chunk_keys(
            "acme", "fake-model", [(*KEY[:2], index, *KEY[3:]) for index in range(65)]
        )
    assert ch.calls == []
