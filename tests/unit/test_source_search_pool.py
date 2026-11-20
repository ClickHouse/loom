"""Admission precedes mirror leg limits, duplicate removal and fusion ranks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from server.knowledge.app import source_search as app
from server.knowledge.app.search import HybridSearchQuery
from server.knowledge.app.source_access import Admitted

MIRROR = "acl-" + "1" * 24
SID = "11111111-1111-4111-8111-111111111111"


def row(i, *, uri=None, admitted=True):
    return {
        "namespace": MIRROR,
        "source_uri": (uri or f"gdrive:{i}") + "#" + SID,
        "chunk_idx": 0,
        "doc_version": 1,
        "revision": 0,
        "distance": i / 10000,
        "lexical": 0.0,
        "matched": [],
        "admitted": admitted,
    }


@pytest.fixture
def world(monkeypatch):
    sizes, opened, closed = [], [], []
    plan = SimpleNamespace(
        org="acme",
        sources={(MIRROR, SID): "docs"},
        discovery_sources={(MIRROR, SID): "docs"},
        reader=None,
        discovery_reader=None,
        failed=False,
        unread=False,
    )
    plan.listing_current = lambda _namespace, _source: True
    plan.current = lambda item: plan.listing_current(item.document["namespace"], SID)

    async def check(batch, *, chunks):
        assert chunks
        sizes.append(len(batch))
        return [
            Admitted(
                "docs",
                r,
                {"namespace": MIRROR, "source_uri": r["source_uri"], "tags": ["source-id:" + SID]},
            )
            for r in batch
            if r["admitted"] and not plan.failed
        ]

    plan.check = check
    monkeypatch.setattr(app.pg, "enabled", lambda: False)
    q = HybridSearchQuery("acme", ("docs",), "fake", [1.0], 2, tokens=["token"], keys=["key"])

    def install(factory):
        @asynccontextmanager
        async def candidates(sql, _params):
            leg = (
                "lexical"
                if "lexical DESC" in sql
                else "keys"
                if "length(matched)" in sql
                else "semantic"
            )
            opened.append(leg)
            try:
                yield factory(leg)
            finally:
                closed.append(leg)

        monkeypatch.setattr(app.store, "candidates", candidates)

    return plan, q, sizes, opened, closed, install


async def test_scan_passes_thousands_of_denied_candidates_in_bounded_batches(world):
    plan, q, sizes, opened, closed, install = world

    async def rows(_leg):
        for i in range(6010):
            yield row(i, admitted=i >= 6000)

    install(rows)
    out = await app.collect_leg(plan, q, "semantic", limit=2)
    assert [r.candidate["source_uri"] for r in out] == ["gdrive:6000#" + SID, "gdrive:6001#" + SID]
    assert max(sizes) <= 64 and len(sizes) > 90
    assert opened == closed == ["semantic"]


async def test_each_search_pass_discovers_only_its_requested_original_namespaces(
    world, monkeypatch
):
    plan, q, _sizes, _opened, _closed, _install = world
    other = "acl-" + "2" * 24
    other_sid = "22222222-2222-4222-8222-222222222222"
    plan.discovery_sources[(other, other_sid)] = "other-docs"
    seen = []

    @asynccontextmanager
    async def candidates(_sql, params):
        seen.append(params)

        async def rows():
            yield row(0)

        yield rows()

    monkeypatch.setattr(app.store, "candidates", candidates)
    assert len(await app.collect_leg(plan, q, "semantic", limit=2)) == 1
    assert seen[0]["nss"] == [MIRROR]
    assert seen[0]["source_tags"] == ["source-id:" + SID]


async def test_ordinary_families_and_duplicate_copies_do_not_consume_admitted_pool(world):
    plan, q, _sizes, _opened, closed, install = world

    async def rows(_leg):
        for i, uri in enumerate(("gdrive:a", "gdrive:a", "gdrive:b", "gdrive:c")):
            yield row(i, uri=uri)

    install(rows)
    out = await app.collect_leg(plan, q, "semantic", limit=2, ordinary={("docs", "gdrive:a")})
    assert [r.candidate["source_uri"] for r in out] == ["gdrive:b#" + SID, "gdrive:c#" + SID]
    assert closed == ["semantic"]


async def test_late_candidate_failure_discards_earlier_batches_without_unread(world):
    plan, q, _sizes, _opened, closed, install = world
    check = plan.check

    async def fail(batch, *, chunks):
        if batch[0]["distance"] >= 0.0064:
            plan.failed = True
            return []
        return await check(batch, chunks=chunks)

    plan.check = fail

    async def rows(_leg):
        for i in range(70):
            yield row(i)

    install(rows)
    assert await app.collect_leg(plan, q, "semantic", limit=100) == []
    assert plan.failed and not plan.unread and closed == ["semantic"]


async def test_late_discovery_failure_discards_earlier_batches_and_marks_unread(world):
    plan, q, _sizes, _opened, closed, install = world

    async def rows(_leg):
        for i in range(64):
            yield row(i)
        raise RuntimeError("synthetic private backend error")

    install(rows)
    assert await app.collect_leg(plan, q, "semantic", limit=100) == []
    assert plan.failed and plan.unread and closed == ["semantic"]


async def test_selected_discovery_streams_open_even_after_another_candidate_failure(world):
    plan, q, _sizes, opened, closed, install = world

    async def fail(_batch, *, chunks):
        plan.failed = True
        return []

    plan.check = fail

    async def rows(_leg):
        yield row(0)

    install(rows)
    assert await app.discover(plan, q, legs=("semantic", "lexical", "keys"), limit=2) == {}
    assert sorted(opened) == sorted(closed) == ["keys", "lexical", "semantic"]
    assert plan.failed and not plan.unread


async def test_cancellation_closes_stream_without_becoming_an_unread_failure(world):
    plan, q, _sizes, _opened, closed, install = world

    async def rows(_leg):
        yield row(0)
        raise asyncio.CancelledError

    install(rows)
    with pytest.raises(asyncio.CancelledError):
        await app.collect_leg(plan, q, "semantic", limit=100)
    assert closed == ["semantic"] and not plan.unread


async def test_final_recheck_drops_a_retained_pool_after_permissions_expire(world):
    plan, q, sizes, _opened, _closed, install = world
    check = plan.check

    async def checked(batch, *, chunks):
        if sizes:
            return []
        return await check(batch, chunks=chunks)

    plan.check = checked

    async def rows(_leg):
        yield row(0)

    install(rows)
    out = await app.discover(plan, q, legs=("semantic",), limit=1)
    assert out == {"semantic": []}
    assert not plan.unread


async def test_final_candidate_failure_discards_every_discovery_pool(world):
    plan, q, sizes, _opened, _closed, install = world
    check = plan.check

    async def checked(batch, *, chunks):
        if len(sizes) >= 3:
            plan.failed = True
            return []
        return await check(batch, chunks=chunks)

    plan.check = checked

    async def rows(_leg):
        yield row(0)

    install(rows)
    out = await app.discover(plan, q, legs=("semantic", "lexical", "keys"), limit=1)
    assert out == {} and plan.failed and not plan.unread


async def test_expiry_during_final_other_batch_also_evicts_earlier_rechecked_rows(world):
    plan, q, sizes, _opened, _closed, install = world
    check = plan.check
    expired = False

    async def checked(batch, *, chunks):
        nonlocal expired
        found = await check(batch, chunks=chunks)
        if len(sizes) == 4:
            expired = True
        return found

    plan.check = checked
    plan.listing_current = lambda _namespace, _source: not expired

    async def rows(_leg):
        for i in range(100):
            yield row(i)

    install(rows)
    out = await app.discover(plan, q, legs=("semantic",), limit=100)
    assert expired and out == {"semantic": []}
    assert not plan.unread


async def test_later_discovery_still_opens_after_conditional_source_removal(world):
    plan, q, _sizes, opened, closed, install = world

    async def check(_batch, *, chunks):
        plan.sources = {}
        plan.reader = None
        plan.failed = True
        return []

    plan.check = check

    async def rows(leg):
        if leg == "lexical":
            raise RuntimeError("unconditional backend failure")
        yield row(0)

    install(rows)
    assert await app.collect_leg(plan, q, "semantic", limit=2) == []
    assert await app.collect_leg(plan, q, "lexical", limit=2) == []
    assert opened == closed == ["semantic", "lexical"]
    assert plan.failed and plan.unread
