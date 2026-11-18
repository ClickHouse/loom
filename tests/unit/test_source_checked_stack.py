"""Checked stack fallback rebuilds every knowledge-derived field from ordinary rows."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auth.reader_identity import ReaderIdentity
from server.core import pg, request_work
from server.knowledge.app import pg_serving, search, source_access, source_search_passes
from server.knowledge.app.search import ChunkHit
from server.knowledge.app.source_search import Hydrated
from server.knowledge.domain.access_list import AccessList
from server.knowledge.store import source_candidates
from server.memory.api.models_lifecycle import MemoryStackInput, _StackEntry
from server.memory.app import stack
from server.settings import settings

NS = "acl-" + "1" * 24
SID = "11111111-1111-4111-8111-111111111111"
URI = "gdrive:file#" + SID


def hit(name, *, tags=(), content=None, distance=0.5):
    return ChunkHit("docs", name, 0, content or name, list(tags), distance, score=1 - distance)


@pytest.fixture
def world(monkeypatch):
    token = request_work._current.set(None)
    request_work.begin()
    assert request_work.mark_mirrored()
    clock = SimpleNamespace(at=datetime(2025, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(source_access, "now", lambda: clock.at)
    monkeypatch.setattr(settings, "knowledge_source_acl_stale_seconds", 5)
    reader = ReaderIdentity("lee", frozenset({"lee@example.com"}), frozenset())
    plan = source_access.ReadPlan(
        "acme",
        sources={(NS, SID): "docs"},
        listing_starts={(NS, SID): clock.at},
        reader=reader,
        marked=True,
        _bindings=((NS, SID, "docs"),),
    )
    item = source_access.Admitted(
        "docs",
        {
            "namespace": NS,
            "source_uri": URI,
            "chunk_idx": 0,
            "doc_version": 10,
            "revision": 0,
            "distance": 0.05,
            "lexical": 0.0,
            "matched": [],
        },
        {
            "namespace": NS,
            "source_uri": URI,
            "version": 10,
            "revision": 0,
            "ingested_at": clock.at,
            "tags": ["source-id:" + SID],
        },
        AccessList(emails=("lee@example.com",)),
        reader_emails=reader.emails,
    )
    state = SimpleNamespace(
        admit=True,
        revoked=False,
        chunk_gone=False,
        first=[hit("first-a"), hit("first-b")],
        wide=[hit("wide-a"), hit("wide-b")],
        queries=[],
        embeddings=[],
        prefix_calls=0,
        first_error=None,
        wide_error=None,
        content="private",
    )

    async def check(candidates, **_kwargs):
        return [item] if candidates and state.admit and not state.revoked else []

    async def prepare(org, namespaces):
        assert org == "acme" and namespaces == ["docs"]
        assert request_work.mark_mirrored()
        return plan

    async def discover(_plan, query, **_kwargs):
        assert _plan is plan and query.namespaces == ("docs",)
        return {"semantic": [item]} if state.admit and not state.revoked else {}

    async def hydrate(_plan, pools, **_kwargs):
        return {
            leg: [Hydrated(entry, {"content": state.content}) for entry in entries]
            for leg, entries in pools.items()
        }

    async def keys(org, model, keys, *, embedded_only=True):
        assert org == "acme" and model == "fake"
        return [] if state.chunk_gone else keys

    async def profile(_input, fill):
        state.prefix_calls += 1
        fill.add(
            _StackEntry(
                section="profile",
                memory_key="profile.render",
                memory_type="profile",
                value="prefix",
                tokens=1,
            )
        )

    class Embedder:
        model_name = "fake"

        async def embed(self, text):
            state.embeddings.append(text)
            return [1.0]

    async def ranked(query):
        assert query.keys == query.tokens == []
        assert query.namespaces == ("docs",)
        state.queries.append(query.top_k)
        error = state.first_error if query.top_k == 2 else state.wide_error
        if error is not None:
            raise error
        return state.first if query.top_k == 2 else state.wide

    monkeypatch.setattr(plan, "check", check)
    monkeypatch.setattr(source_access, "prepare", prepare)
    monkeypatch.setattr(source_search_passes, "discover", discover)
    monkeypatch.setattr(source_search_passes, "hydrate", hydrate)
    monkeypatch.setattr(source_candidates, "chunk_keys", keys)
    monkeypatch.setattr(stack, "get_client", lambda: SimpleNamespace())
    monkeypatch.setattr(stack, "_stack_profile", profile)
    monkeypatch.setattr(stack, "_stack_pinned", AsyncMock())
    monkeypatch.setattr(stack, "_stack_recent", AsyncMock(return_value=""))
    monkeypatch.setattr("server.embedding.get_embedder", Embedder)
    monkeypatch.setattr(search, "hybrid_search", ranked)
    monkeypatch.setattr(pg, "enabled", lambda: False)
    yield SimpleNamespace(state=state, plan=plan, clock=clock)
    request_work._current.reset(token)


def request(**changes):
    return MemoryStackInput(
        org="acme",
        namespace="docs",
        agent="reader",
        seed_query="question",
        procedure_limit=0,
        include_knowledge=True,
        knowledge_top_k=2,
        **changes,
    )


@pytest.mark.parametrize("admit", [False, True])
async def test_marked_stack_uses_exact_separate_widths_and_one_seed_embedding(world, admit):
    world.state.admit = admit
    output = await stack.handle_memory_stack(request())
    assert world.state.queries == [2, 8] and world.state.embeddings == ["question"]
    values = [entry.value for entry in output.entries if entry.section == "knowledge"]
    assert values == (["private", "first-a"] if admit else ["first-a", "first-b"])
    assert world.state.prefix_calls == 1


@pytest.mark.parametrize("loss", ["revoked", "chunk_gone", "failed", "unread"])
async def test_later_loss_rebuilds_the_whole_stack_without_retrieval_or_prefix_replay(world, loss):
    checked = await stack.run_checked_stack(request(), world.plan)
    assert checked.output.entries[-2].value == "private"
    if loss in {"failed", "unread"}:
        setattr(world.plan, loss, True)
    else:
        setattr(world.state, loss, True)
    actual = await checked.current()
    assert [entry.value for entry in actual.entries] == ["prefix", "first-a", "first-b"]
    assert checked.lost and checked.fresh()
    assert await checked.current() == actual and await checked.ordinary() == actual
    assert world.state.queries == [2, 8] and world.state.prefix_calls == 1
    assert world.state.embeddings == ["question"]


async def test_private_token_overflow_cannot_leave_the_ordinary_stack_truncated(world):
    world.state.content = "private" * 200
    checked = await stack.run_checked_stack(request(token_budget=200), world.plan)
    assert checked.output.truncated and len(checked.output.entries) == 1
    world.state.revoked = True
    actual = await checked.current()
    assert not actual.truncated and actual.section_counts["knowledge"] == 2
    world.state.admit = False
    baseline = await stack.run_checked_stack(request(token_budget=200), world.plan)
    assert actual.model_dump() == (await baseline.current()).model_dump()


async def test_suppressed_mixed_widening_uses_saved_complete_ordinary_wider_pass(world):
    world.state.first = [
        hit("rule", tags=("source-kind:assistant_turn", "agent:other")),
        hit("first-b"),
    ]
    checked = await stack.run_checked_stack(request(), world.plan)
    assert [entry.value for entry in checked.output.entries] == ["prefix", "private", "first-b"]
    world.state.revoked = True
    output = await checked.current()
    assert [entry.value for entry in output.entries] == ["prefix", "wide-a", "wide-b"]
    assert world.state.queries == [2, 8]


async def test_optional_wider_failure_keeps_the_first_ordinary_pass_without_retry(world, caplog):
    world.state.first = [
        hit("rule", tags=("source-kind:assistant_turn", "agent:other")),
        hit("first-b"),
    ]
    world.state.wide_error = RuntimeError("private backend marker")
    checked = await stack.run_checked_stack(request(), world.plan)
    world.state.revoked = True
    output = await checked.current()
    assert [entry.value for entry in output.entries] == ["prefix", "first-b"]
    assert world.state.queries == [2, 8]
    assert "private backend marker" not in caplog.text


async def test_first_failure_is_not_rescued_by_wide_success(world):
    world.state.first_error = RuntimeError("private first failure")
    output = await stack.handle_memory_stack(request())
    assert [entry.value for entry in output.entries] == ["prefix"]
    assert world.state.queries == [2]


async def test_pure_outer_freshness_detects_expiry_after_another_pool_waits(world):
    checked = await stack.run_checked_stack(request(), world.plan)
    assert checked.fresh()
    world.clock.at += timedelta(seconds=6)
    assert not checked.fresh()
    output = await checked.current()
    assert [entry.value for entry in output.entries] == ["prefix", "first-a", "first-b"]


async def test_wider_cancellation_propagates(world):
    world.state.wide_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await stack.handle_memory_stack(request())


async def test_wrong_org_plan_refuses_before_the_first_provider(world):
    with pytest.raises(ValueError, match="document read scope changed"):
        await stack.run_checked_stack(request(), replace(world.plan, org="other"))
    assert world.state.prefix_calls == 0 and not world.state.queries and not world.state.embeddings


async def test_retired_ordinary_fallback_and_shadow_disappear_from_the_same_snapshot(
    world, monkeypatch
):
    world.state.first = [hit("gdrive:file")]
    calls = 0

    async def keep(_org, hits):
        nonlocal calls
        calls += 1
        kept = hits if calls == 1 else [h for h in hits if h.source_uri != "gdrive:file"]
        return kept, len(hits) - len(kept)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_serving, "keep_served", keep)
    checked = await stack.run_checked_stack(request(), world.plan)
    assert [entry.value for entry in checked.output.entries] == ["prefix", "private"]
    world.state.revoked = True
    assert [entry.value for entry in (await checked.current()).entries] == ["prefix"]
    assert world.state.queries == [2, 8]


async def test_fresh_serving_uses_physical_rows_and_the_actual_refill_query(world, monkeypatch):
    old = replace(hit("gdrive:file", content="old"), doc_version=10, revision=0)
    new = replace(old, content="current", doc_version=11, revision=1)
    queries, candidates = [], []
    calls = 0

    async def ranked(query):
        queries.append(query.top_k)
        return [old, new]

    async def keep(_org, hits):
        nonlocal calls
        calls += 1
        # A headless first read admits both versions; publication races its refill.
        kept = hits if calls <= 2 else [h for h in hits if h.doc_version == 11]
        return kept, len(hits) - len(kept)

    original = source_search_passes.discover

    async def discover(plan, query, **kwargs):
        candidates.append(query.top_k)
        return await original(plan, query, **kwargs)

    monkeypatch.setattr(search, "hybrid_search", ranked)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_serving, "keep_served", keep)
    monkeypatch.setattr(source_search_passes, "discover", discover)
    checked = await stack.run_checked_stack(request(), world.plan)
    assert queries == [2, 4, 8]
    assert candidates == [4, 8]
    # The live new raw row still shadows the source family, while the old physical
    # fallback is gone. Logical-key membership must not retain the old object.
    assert [entry.value for entry in checked.output.entries] == ["prefix"]
