"""List proofs retain lookahead families and renew shadows before the final barrier."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app.source_access import Admitted
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.store import source_documents as store

NS = "acl-" + "1" * 24
SID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def world(monkeypatch):
    item = Admitted(
        "team",
        {"namespace": NS, "source_uri": "gdrive:file#" + SID},
        {
            "namespace": NS,
            "source_uri": "gdrive:file#" + SID,
            "tags": ["source-id:" + SID],
            "version": 10,
            "revision": 0,
            "ingested_at": datetime(2025, 1, 1, tzinfo=UTC),
        },
    )
    events = []

    async def documents(candidates):
        events.append(("documents", len(candidates)))
        return [
            replace(item, candidate=dict(candidate), document={**item.document, **candidate})
            for candidate in candidates
        ]

    async def shadows(org, pairs, *, include_superseded):
        assert org == "acme" and include_superseded
        assert all(ns == "team" and "#" not in uri for ns, uri in pairs)
        events.append(("shadows", len(pairs)))
        return set()

    plan = SimpleNamespace(
        org="acme",
        failed=False,
        unread=False,
        current=lambda _: True,
        current_evidence=lambda _: True,
        check=AsyncMock(side_effect=documents),
    )
    monkeypatch.setattr(store, "ordinary_families", AsyncMock(side_effect=shadows))
    return plan, item, events


def bundle(world, items=None):
    from server.knowledge.app.source_list_proofs import ListProofs

    plan, item, _events = world
    return ListProofs(plan, True, tuple([item] if items is None else items))


async def test_shadow_barrier_uses_public_families_between_two_document_renewals(world):
    proof = bundle(world)
    await proof.check()
    assert world[2] == [("documents", 1), ("shadows", 1), ("documents", 1)]
    assert proof.fresh() and not proof.invalid
    store.ordinary_families.assert_awaited_once_with(
        "acme", [("team", "gdrive:file")], include_superseded=True
    )


async def test_all_selected_families_include_lookahead_and_use_bounded_shadow_batches(world):
    _plan, item, events = world
    items = [
        replace(
            item,
            candidate={**item.candidate, "source_uri": f"gdrive:{i}#" + SID},
            document={**item.document, "source_uri": f"gdrive:{i}#" + SID},
        )
        for i in range(129)
    ]
    proof = bundle(world, items)
    await proof.check()
    assert events == [
        (kind, size) for kind in ("documents", "shadows", "documents") for size in (64, 64, 1)
    ]
    assert proof.fresh()


@pytest.mark.parametrize("change", ["shadow", "backend", "denied", "expired"])
async def test_any_lost_selected_family_invalidates_the_whole_list_monotonically(world, change):
    plan, _item, _events = world
    proof = bundle(world)

    async def changed(*_args, **_kwargs):
        if change == "backend":
            raise RuntimeError("private shadow backend")
        if change == "denied":
            plan.check.side_effect = None
            plan.check.return_value = []
        if change == "expired":
            plan.current = lambda _: False
        return {("team", "gdrive:file")} if change == "shadow" else set()

    store.ordinary_families.side_effect = changed
    with pytest.raises(LostCorpus, match="^document access changed$") as caught:
        await proof.check()
    assert proof.invalid and not proof.fresh() and not plan.unread
    assert plan.failed == (change == "backend")
    assert "private" not in str(caught.value)
    calls = plan.check.await_count
    with pytest.raises(LostCorpus):
        await proof.check()
    assert plan.check.await_count == calls


@pytest.mark.parametrize("phase", ["first_documents", "shadows", "last_documents"])
async def test_unchecked_freshness_loss_prevents_an_inflight_initial_check_return(world, phase):
    plan, _item, _events = world
    proof = bundle(world)
    entered, release = asyncio.Event(), asyncio.Event()
    documents = plan.check.side_effect
    calls = 0

    async def pause(candidates):
        nonlocal calls
        calls += 1
        if phase == "first_documents" or (phase == "last_documents" and calls == 2):
            entered.set()
            await release.wait()
        return await documents(candidates)

    async def shadow(*_args, **_kwargs):
        if phase == "shadows":
            entered.set()
            await release.wait()
        return set()

    plan.check.side_effect = pause
    store.ordinary_families.side_effect = shadow
    task = asyncio.create_task(proof.check())
    await entered.wait()
    try:
        assert not proof.fresh()
    finally:
        release.set()
        results = await asyncio.gather(task, return_exceptions=True)
    assert len(results) == 1 and isinstance(results[0], LostCorpus)
    assert proof.invalid


async def test_cancelled_shadow_probe_propagates_and_cannot_certify_a_first_check(world):
    proof = bundle(world)
    store.ordinary_families.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await proof.check()
    assert not proof.fresh()
    assert not world[0].failed and not world[0].unread


async def test_empty_ordinary_proof_needs_no_conditional_state_or_queries(world):
    plan, _item, events = world
    plan.failed = plan.unread = True
    proof = bundle(world, [])
    assert proof.fresh()
    await proof.check()
    assert proof.fresh() and events == []
    store.ordinary_families.assert_not_awaited()
