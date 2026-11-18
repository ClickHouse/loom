"""Search proofs retain no text and renew live chunks between document checks."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app.source_access import Admitted
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.domain.serving import Serving
from server.knowledge.store import source_candidates as store

NS = "acl-" + "1" * 24
URI = "gdrive:file#11111111-1111-4111-8111-111111111111"
MODEL = "fake-model"


def key(item):
    row = item.candidate
    return (
        row["namespace"],
        row["source_uri"],
        row["chunk_idx"],
        row["doc_version"],
        row["revision"],
    )


@pytest.fixture
def world(monkeypatch):
    items = tuple(
        Admitted(
            "team",
            {
                "namespace": NS,
                "source_uri": URI,
                "chunk_idx": index,
                "doc_version": 9,
                "revision": 0,
            },
            {
                "namespace": NS,
                "source_uri": URI,
                "tags": [],
                "version": 10,
                "revision": 0,
                "ingested_at": datetime(2025, 1, 1, tzinfo=UTC),
            },
            serving=Serving(False, 0, 0, 9),
        )
        for index in range(2)
    )
    events = []

    async def documents(candidates):
        assert candidates == [{"namespace": NS, "source_uri": URI}]
        events.append("documents")
        return [items[0]]

    async def keys(org, model, selected, *, embedded_only=True):
        assert (org, model) == ("acme", MODEL)
        events.append("chunks")
        return list(selected)

    plan = SimpleNamespace(
        org="acme",
        failed=False,
        unread=False,
        current=lambda _item: True,
        check=AsyncMock(side_effect=documents),
    )
    monkeypatch.setattr(store, "chunk_keys", AsyncMock(side_effect=keys))
    return plan, items, events


def proof(world, items=None):
    from server.knowledge.app.source_search_proofs import SearchProofs

    plan, initial, _events = world
    return SearchProofs(plan, MODEL, initial if items is None else tuple(items))


async def test_search_proof_checks_document_then_live_model_keys_then_documents(world):
    checked = proof(world)
    await checked.check()
    assert world[2] == ["documents", "chunks", "documents"]
    store.chunk_keys.assert_awaited_once_with(
        "acme", MODEL, [key(item) for item in world[1]], embedded_only=True
    )
    assert not checked.invalid


@pytest.mark.parametrize("lost", ["missing", "duplicate", "unexpected", "backend"])
async def test_bad_live_key_evidence_invalidates_the_whole_proof_monotonically(world, lost):
    checked = proof(world)
    selected = [key(item) for item in world[1]]
    store.chunk_keys.side_effect = None
    if lost == "backend":
        store.chunk_keys.side_effect = RuntimeError("private backend information")
    else:
        store.chunk_keys.return_value = {
            "missing": selected[:1],
            "duplicate": [*selected, selected[0]],
            "unexpected": [*selected[:1], (NS, URI, 99, 9, 0)],
        }[lost]
    with pytest.raises(LostCorpus, match="^document access changed$"):
        await checked.check()
    assert checked.invalid and not world[0].unread
    calls = store.chunk_keys.await_count
    store.chunk_keys.side_effect = None
    store.chunk_keys.return_value = selected
    with pytest.raises(LostCorpus):
        await checked.check()
    assert store.chunk_keys.await_count == calls


async def test_permission_loss_during_key_probe_is_detected_by_final_document_renewal(world):
    checked = proof(world)

    async def revoke(_org, _model, selected, *, embedded_only=True):
        world[0].check.side_effect = None
        world[0].check.return_value = []
        return list(selected)

    store.chunk_keys.side_effect = revoke
    with pytest.raises(LostCorpus):
        await checked.check()
    assert checked.invalid


async def test_expected_document_and_chunk_identities_do_not_alias_mutable_input(world):
    checked = proof(world)
    world[1][0].candidate["doc_version"] = 100
    world[1][0].document["version"] = 100
    with pytest.raises(LostCorpus):
        await checked.check()


async def test_metadata_only_head_version_can_keep_older_serving_chunks(world):
    checked = proof(world)
    assert world[1][0].document["version"] > world[1][0].candidate["doc_version"]
    await checked.check()


@pytest.mark.parametrize("change", ["revision", "chunks_version", "chunks_revision", "deleted"])
async def test_constructor_requires_an_initially_served_chunk_relationship(world, change):
    first = world[1][0]
    if change == "revision":
        bad = replace(first, candidate={**first.candidate, "revision": 99})
    else:
        bad = replace(
            first, serving=first.serving._replace(**{change: True if change == "deleted" else 99})
        )
    checked = proof(world, [bad])
    with pytest.raises(LostCorpus):
        await checked.check()
    assert checked.invalid
    world[0].check.assert_not_awaited()
    store.chunk_keys.assert_not_awaited()


async def test_inconsistent_document_evidence_never_chooses_the_last_identity(world):
    first, second = world[1]
    bad = replace(second, document={**second.document, "version": 99})
    checked = proof(world, [first, bad])
    with pytest.raises(LostCorpus):
        await checked.check()
    world[0].check.assert_not_awaited()
    store.chunk_keys.assert_not_awaited()


async def test_key_batches_are_bounded_and_document_renewal_follows_all_batches(world):
    first = world[1][0]
    many = [
        replace(first, candidate={**first.candidate, "chunk_idx": index}) for index in range(65)
    ]
    checked = proof(world, many)
    await checked.check()
    assert [len(call.args[2]) for call in store.chunk_keys.await_args_list] == [64, 1]
    assert world[2] == ["documents", "chunks", "chunks", "documents"]


async def test_empty_proof_does_no_conditional_io_even_after_plan_failure(world):
    world[0].failed = world[0].unread = True
    checked = proof(world, [])
    await checked.check()
    world[0].check.assert_not_awaited()
    store.chunk_keys.assert_not_awaited()


async def test_key_probe_cancellation_propagates_without_a_second_document_read(world):
    checked = proof(world)
    store.chunk_keys.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await checked.check()
    assert world[0].check.await_count == 1


async def test_full_check_sequences_are_serialized(world):
    checked = proof(world)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(_org, _model, selected, *, embedded_only=True):
        entered.set()
        await release.wait()
        return list(selected)

    store.chunk_keys.side_effect = pause
    first = asyncio.create_task(checked.check())
    await entered.wait()
    second = asyncio.create_task(checked.check())
    await asyncio.sleep(0)
    assert world[0].check.await_count == 1
    release.set()
    await asyncio.gather(first, second)
    assert world[0].check.await_count == 4
