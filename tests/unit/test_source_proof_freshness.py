"""Composed reads recheck every permission deadline after their last await."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auth.reader_identity import ReaderIdentity
from server.knowledge.app import source_access
from server.knowledge.app.source_corpus import LostCorpus, Proofs
from server.knowledge.app.source_search_proofs import SearchProofs
from server.knowledge.domain.access_list import AccessList
from server.knowledge.store import source_candidates
from server.settings import settings

NS = "acl-" + "1" * 24
SID = "11111111-1111-4111-8111-111111111111"
URI = "gdrive:file#" + SID


@pytest.fixture
def world(monkeypatch):
    clock = SimpleNamespace(at=datetime(2025, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(source_access, "now", lambda: clock.at)
    monkeypatch.setattr(settings, "knowledge_source_acl_stale_seconds", 5)
    reader = ReaderIdentity("lee", frozenset({"lee@example.com"}), frozenset({"example.com"}))
    plan = source_access.ReadPlan(
        "acme",
        sources={(NS, SID): "team"},
        listing_starts={(NS, SID): clock.at},
        reader=reader,
        marked=True,
        _bindings=((NS, SID, "team"),),
    )
    item = source_access.Admitted(
        "team",
        {"namespace": NS, "source_uri": URI, "chunk_idx": 0, "doc_version": 10, "revision": 0},
        {
            "namespace": NS,
            "source_uri": URI,
            "tags": ["source-id:" + SID],
            "version": 10,
            "revision": 0,
            "ingested_at": clock.at,
            "content": "private body must not survive in permission evidence",
        },
        AccessList(groups=("group@example.com",)),
        groups=(("group@example.com", clock.at),),
        reader_emails=reader.emails,
    )
    monkeypatch.setattr(plan, "check", AsyncMock(return_value=[item]))
    return clock, plan, item


async def test_first_bundle_expiry_while_another_checks_is_detected_without_another_await(world):
    clock, plan, item = world
    earlier, later = Proofs(plan, (item,)), Proofs(plan, (item,))
    await earlier.check()
    clock.at += timedelta(seconds=4)
    renewed = replace(item, groups=(("group@example.com", clock.at),))

    async def check(_candidates):
        clock.at += timedelta(seconds=2)
        plan.listing_starts[(NS, SID)] = clock.at
        return [renewed]

    plan.check.side_effect = check
    await later.check()
    assert later.fresh()
    assert not earlier.fresh()
    assert earlier.invalid and not later.invalid
    calls = plan.check.await_count
    clock.at -= timedelta(seconds=6)
    assert not earlier.fresh()
    with pytest.raises(LostCorpus):
        await earlier.check()
    assert plan.check.await_count == calls


@pytest.mark.parametrize("state", ["unchecked", "failed", "unread", "listing", "group", "email"])
async def test_nonempty_freshness_refuses_unchecked_or_lost_evidence_monotonically(world, state):
    clock, plan, item = world
    proof = Proofs(plan, (item,))
    if state != "unchecked":
        await proof.check()
    if state in {"failed", "unread"}:
        setattr(plan, state, True)
    elif state == "listing":
        plan.listing_starts.clear()
    elif state == "group":
        clock.at += timedelta(seconds=6)
        plan.listing_starts[(NS, SID)] = clock.at
    elif state == "email":
        plan.reader = ReaderIdentity("lee", frozenset({"other@example.com"}), frozenset())
    assert not proof.fresh()
    assert proof.invalid
    plan.failed = plan.unread = False
    plan.reader = ReaderIdentity("lee", item.reader_emails, frozenset())
    plan.listing_starts[(NS, SID)] = clock.at
    assert not proof.fresh()


@pytest.mark.parametrize("arm", ["owner", "anyone", "emails", "domains"])
async def test_group_expiry_keeps_each_independent_permission_arm(world, arm):
    clock, plan, item = world
    value = {
        "owner": "owner-alias",
        "anyone": True,
        "emails": ("lee@example.com",),
        "domains": ("example.com",),
    }[arm]
    item = replace(
        item,
        access=AccessList(groups=("group@example.com",), **{arm: value}),
        canonical_owner="lee",
    )
    plan.check.return_value = [item]
    proof = Proofs(plan, (item,))
    await proof.check()
    clock.at += timedelta(seconds=6)
    plan.listing_starts[(NS, SID)] = clock.at
    assert proof.fresh() and not proof.invalid
    assert plan.current(item)


async def test_successful_renewal_replaces_the_immutable_permission_snapshot(world):
    clock, plan, item = world
    proof = Proofs(plan, (item,))
    await proof.check()
    clock.at += timedelta(seconds=4)
    plan.listing_starts[(NS, SID)] = clock.at
    plan.check.return_value = [replace(item, groups=(("group@example.com", clock.at),))]
    await proof.check()
    clock.at += timedelta(seconds=2)
    assert proof.fresh()
    assert not plan.current(item)


async def test_permission_snapshot_does_not_retain_document_or_candidate_dictionaries(world):
    _clock, plan, item = world
    proof = Proofs(plan, (item,))
    await proof.check()
    assert proof.fresh()
    [evidence] = proof._evidence
    assert {f.name for f in fields(evidence)} == {
        "namespace",
        "source_id",
        "access",
        "canonical_owner",
        "groups",
        "reader_emails",
    }
    item.document.clear()
    item.candidate.clear()
    assert proof.fresh()


async def test_empty_ordinary_proof_has_no_mirror_deadline_or_conditional_reads(world):
    _clock, plan, _item = world
    plan.failed = plan.unread = True
    proof = Proofs(plan, ())
    assert proof.fresh()
    await proof.check()
    assert proof.fresh() and not proof.invalid
    plan.check.assert_not_awaited()


async def test_search_freshness_uses_document_evidence_without_repeating_key_probes(
    world, monkeypatch
):
    clock, plan, item = world
    key = (NS, URI, 0, 10, 0)
    query = AsyncMock(return_value=[key])
    monkeypatch.setattr(source_candidates, "chunk_keys", query)
    proof = SearchProofs(plan, "fake-model", (item,))
    await proof.check()
    assert proof.fresh()
    query.assert_awaited_once_with("acme", "fake-model", [key], embedded_only=True)
    clock.at += timedelta(seconds=6)
    assert not proof.fresh() and proof.invalid
    query.assert_awaited_once()


def test_empty_search_freshness_remains_ordinary_only_with_a_failed_plan(world):
    _clock, plan, _item = world
    plan.failed = plan.unread = True
    assert SearchProofs(plan, "", ()).fresh()


@pytest.mark.parametrize("search", [False, True])
async def test_inflight_renewal_cannot_return_success_after_freshness_observes_loss(
    world, monkeypatch, search
):
    _clock, plan, item = world
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(_candidates):
        entered.set()
        await release.wait()
        return [item]

    plan.check.side_effect = wait
    monkeypatch.setattr(
        source_candidates, "chunk_keys", AsyncMock(return_value=[(NS, URI, 0, 10, 0)])
    )
    proof = SearchProofs(plan, "fake-model", (item,)) if search else Proofs(plan, (item,))
    task = asyncio.create_task(proof.check())
    await entered.wait()
    assert not proof.fresh()
    release.set()
    with pytest.raises(LostCorpus):
        await task
    assert proof.invalid


@pytest.mark.parametrize("phase", ["keys", "final_documents"])
async def test_search_is_unchecked_until_the_entire_initial_key_sequence_completes(
    world, monkeypatch, phase
):
    _clock, plan, item = world
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def documents(_candidates):
        nonlocal calls
        calls += 1
        if phase == "final_documents" and calls == 2:
            entered.set()
            await release.wait()
        return [item]

    async def keys(*_args, embedded_only=True):
        if phase == "keys":
            entered.set()
            await release.wait()
        return [(NS, URI, 0, 10, 0)]

    plan.check.side_effect = documents
    monkeypatch.setattr(source_candidates, "chunk_keys", keys)
    proof = SearchProofs(plan, "fake-model", (item,))
    task = asyncio.create_task(proof.check())
    await entered.wait()
    try:
        assert not proof.fresh()
    finally:
        release.set()
        results = await asyncio.gather(task, return_exceptions=True)
    assert len(results) == 1 and isinstance(results[0], LostCorpus)
    assert proof.invalid
