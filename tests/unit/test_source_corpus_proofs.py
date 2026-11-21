"""Synthesis retains immutable copy identities and a monotonic invalidation latch."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.app.source_access import Admitted
from server.knowledge.domain.serving import Serving


@pytest.fixture
def world():
    item = Admitted(
        "team",
        {"namespace": "acl-" + "1" * 24, "source_uri": "gdrive:file#source"},
        {
            "namespace": "acl-" + "1" * 24,
            "source_uri": "gdrive:file#source",
            "tags": [],
            "version": 10,
            "revision": 0,
            "ingested_at": datetime(2025, 1, 1, tzinfo=UTC),
        },
        serving=Serving(False, 0, 0, 9),
    )
    fresh = replace(item, candidate=dict(item.candidate), document=dict(item.document))
    plan = SimpleNamespace(
        failed=False,
        unread=False,
        current=lambda _item: True,
        check=AsyncMock(return_value=[fresh]),
    )
    return plan, item, fresh


def bundle(world, items=None):
    from server.knowledge.app import source_corpus

    plan, item, _fresh = world
    return source_corpus.Proofs(plan, tuple(items if items is not None else [item]))


async def test_current_copy_and_recorded_serving_pair_validate_without_text(world):
    plan, item, _fresh = world
    proof = bundle(world)
    await proof.check()
    assert not proof.invalid
    plan.check.assert_awaited_once_with([item.candidate])


@pytest.mark.parametrize(
    "change",
    [
        "namespace",
        "source_uri",
        "public_namespace",
        "version",
        "revision",
        "ingested_at",
        "serving",
        "denied",
        "duplicate",
        "expired",
        "failed",
        "unread",
    ],
)
async def test_lost_proof_invalidates_monotonically_and_cannot_be_readmitted(world, change):
    from server.knowledge.app import source_corpus

    plan, _item, fresh = world
    proof = bundle(world)
    if change in {"namespace", "source_uri"}:
        fresh.candidate[change] = "other"
    elif change == "public_namespace":
        plan.check.return_value = [replace(fresh, namespace="other")]
    elif change in {"version", "revision"}:
        fresh.document[change] += 1
    elif change == "ingested_at":
        fresh.document[change] = datetime(2025, 1, 2, tzinfo=UTC)
    elif change == "serving":
        plan.check.return_value = [replace(fresh, serving=Serving(False, 0, 0, 10))]
    elif change == "denied":
        plan.check.return_value = []
    elif change == "duplicate":
        plan.check.return_value = [fresh, fresh]
    elif change == "expired":
        plan.current = lambda _item: False
    else:
        setattr(plan, change, True)
    with pytest.raises(source_corpus.LostCorpus):
        await proof.check()
    assert proof.invalid
    calls = plan.check.await_count
    plan.check.return_value = [fresh]
    plan.failed = plan.unread = False
    plan.current = lambda _item: True
    with pytest.raises(source_corpus.LostCorpus):
        await proof.check()
    assert plan.check.await_count == calls


async def test_expected_identity_is_snapshotted_instead_of_aliasing_mutable_metadata(world):
    from server.knowledge.app import source_corpus

    _plan, item, fresh = world
    proof = bundle(world)
    item.document["version"] = fresh.document["version"] = 11
    with pytest.raises(source_corpus.LostCorpus):
        await proof.check()


async def test_empty_mirror_bundle_keeps_ordinary_work_despite_unread_mirror_plan(world):
    plan, _item, _fresh = world
    plan.failed = plan.unread = True
    proof = bundle(world, [])
    await proof.check()
    assert not proof.invalid
    plan.check.assert_not_awaited()


async def test_conditional_backend_error_is_sanitized_and_does_not_mark_unread(world):
    from server.knowledge.app import source_corpus

    plan, _item, _fresh = world
    plan.check.side_effect = RuntimeError("private backend detail")
    with pytest.raises(source_corpus.LostCorpus) as caught:
        await bundle(world).check()
    assert "private" not in str(caught.value) and caught.value.__suppress_context__
    assert plan.failed and not plan.unread


async def test_cancellation_propagates(world):
    plan, _item, _fresh = world
    plan.check.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await bundle(world).check()
    assert not plan.unread


async def test_last_await_expiry_invalidates_earlier_batches_too(world):
    from server.knowledge.app import source_corpus

    plan, item, _fresh = world
    items = [
        replace(item, candidate={**item.candidate, "source_uri": f"gdrive:{i}#source"})
        for i in range(129)
    ]
    calls = 0

    async def check(candidates):
        nonlocal calls
        calls += 1
        assert len(candidates) <= 64
        if calls == 3:
            plan.current = lambda selected: selected.candidate["source_uri"] != "gdrive:0#source"
        return [replace(item, candidate=candidate) for candidate in candidates]

    plan.check.side_effect = check
    with pytest.raises(source_corpus.LostCorpus):
        await bundle(world, items).check()
    assert calls == 3 and not plan.unread


async def test_waiting_validation_cannot_restore_a_corpus_after_observed_revocation(world):
    from server.knowledge.app import source_corpus

    plan, _item, _fresh = world
    started, finish = asyncio.Event(), asyncio.Event()
    proof = bundle(world)

    async def revoked(_candidates):
        started.set()
        await finish.wait()
        return []

    plan.check.side_effect = revoked
    first = asyncio.create_task(proof.check())
    await started.wait()
    second = asyncio.create_task(proof.check())
    finish.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, source_corpus.LostCorpus) for result in results)
    assert proof.invalid and plan.check.await_count == 1
