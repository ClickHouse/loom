"""Only exact admitted chunk keys reach text reads or survive their final check."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app import source_search as app
from server.knowledge.app.source_access import Admitted

MIRROR = "acl-" + "1" * 24
SID = "11111111-1111-4111-8111-111111111111"


def item(i):
    row = {
        "namespace": MIRROR,
        "source_uri": f"gdrive:{i}#" + SID,
        "chunk_idx": 0,
        "doc_version": 10,
        "revision": 0,
        "distance": 0.2,
        "lexical": 3.0,
        "matched": ["object"],
    }
    return Admitted("docs", row, {"version": 10})


@pytest.fixture
def world(monkeypatch):
    plan = SimpleNamespace(org="acme", failed=False, unread=False)
    pools = {"semantic": [item(0)], "lexical": [item(0)]}
    calls = []

    async def recheck(_plan, retained):
        calls.append("check")
        return retained

    async def chunks(org, model, keys, *, embedded_only=True):
        calls.append((org, model, keys))
        return [
            {
                "namespace": ns,
                "source_uri": uri,
                "chunk_idx": idx,
                "doc_version": version,
                "revision": revision,
                "content": "checked content",
            }
            for ns, uri, idx, version, revision in keys
        ]

    monkeypatch.setattr(app, "recheck", AsyncMock(side_effect=recheck))
    monkeypatch.setattr(app.store, "chunks", AsyncMock(side_effect=chunks), raising=False)
    return plan, pools, calls


async def test_hydration_checks_before_and_after_one_shared_exact_read(world):
    plan, pools, calls = world
    pools["lexical"][0].candidate["lexical"] = 7.0
    out = await app.hydrate(plan, pools, model="test-model")
    assert calls == [
        "check",
        ("acme", "test-model", [(MIRROR, "gdrive:0#" + SID, 0, 10, 0)]),
        "check",
    ]
    assert out["semantic"][0].row["content"] == "checked content"
    assert out["semantic"][0].admitted.candidate is pools["semantic"][0].candidate
    assert out["lexical"][0].admitted.candidate["lexical"] == 7.0
    assert not plan.failed and not plan.unread


async def test_hydration_does_not_read_text_when_recheck_denies_every_candidate(world, monkeypatch):
    plan, pools, _calls = world
    monkeypatch.setattr(app, "recheck", AsyncMock(return_value={"semantic": []}))
    assert await app.hydrate(plan, pools, model="test-model") == {"semantic": []}
    app.store.chunks.assert_not_awaited()


async def test_hydration_drops_newly_denied_content_and_uses_final_document_metadata(
    world, monkeypatch
):
    plan, pools, _calls = world
    final = Admitted("docs", pools["semantic"][0].candidate, {"version": 11})
    monkeypatch.setattr(app, "recheck", AsyncMock(side_effect=[pools, {"semantic": [final]}]))
    out = await app.hydrate(plan, pools, model="test-model")
    assert list(out) == ["semantic"]
    assert out["semantic"][0].admitted.document == {"version": 11}


@pytest.mark.parametrize(
    "failure", ["read", "duplicate", "unexpected", "newer_revision", "late_read"]
)
async def test_candidate_hydration_failure_discards_all_pools_without_unread(
    world, monkeypatch, failure
):
    plan, pools, _calls = world
    if failure == "late_read":
        pools = {"semantic": [item(i) for i in range(65)]}
    original = app.store.chunks.side_effect
    count = 0

    async def broken(org, model, keys, *, embedded_only=True):
        nonlocal count
        count += 1
        if failure == "read" or (failure == "late_read" and count == 2):
            raise RuntimeError("private title")
        rows = await original(org, model, keys, embedded_only=embedded_only)
        if failure == "duplicate":
            rows.append(dict(rows[0]))
        elif failure == "unexpected":
            rows[0]["source_uri"] = "unrequested"
        elif failure == "newer_revision":
            rows[0]["revision"] = 1
        return rows

    monkeypatch.setattr(app.store, "chunks", AsyncMock(side_effect=broken))
    assert await app.hydrate(plan, pools, model="test-model") == {}
    assert plan.failed and not plan.unread


async def test_document_hydration_requests_chunks_without_embedding_eligibility(world):
    plan, pools, _calls = world
    out = await app.hydrate(plan, pools, model="test-model", embedded_only=False)
    assert out["semantic"][0].row["content"] == "checked content"
    assert app.store.chunks.call_args.kwargs == {"embedded_only": False}


async def test_missing_exact_chunk_is_omitted_without_fetching_its_replacement(world, monkeypatch):
    plan, pools, _calls = world
    monkeypatch.setattr(app.store, "chunks", AsyncMock(return_value=[]))
    assert await app.hydrate(plan, pools, model="test-model") == {"semantic": [], "lexical": []}
    assert app.store.chunks.await_count == 1
    assert not plan.failed and not plan.unread


async def test_hydration_reads_at_most_64_unique_admitted_keys_per_batch(world):
    plan, _pools, calls = world
    out = await app.hydrate(plan, {"semantic": [item(i) for i in range(130)]}, model="test-model")
    assert len(out["semantic"]) == 130
    assert [len(c[2]) for c in calls if isinstance(c, tuple)] == [64, 64, 2]
