"""Synthesis discards every generated field after losing a retained mirror proof."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeSynthesizeInput
from server.knowledge.app import source_access, source_corpus
from server.knowledge.app import synthesize as app
from server.knowledge.app.source_access import Admitted
from server.memory.domain import corpus_synthesis as cs

URI = "gdrive:private#native"


@pytest.fixture
def world(monkeypatch):
    import server.llm.client as clients
    from server.settings import settings

    events = []
    item = Admitted(
        "team",
        {"namespace": "acl-" + "1" * 24, "source_uri": "stored"},
        {
            "namespace": "acl-" + "1" * 24,
            "source_uri": "stored",
            "tags": [],
            "version": 1,
            "revision": 1,
            "ingested_at": datetime(2025, 1, 1, tzinfo=UTC),
        },
    )
    plan = SimpleNamespace(
        org="acme", marked=True, failed=False, unread=False, current=lambda _: True
    )
    plan.check = AsyncMock(return_value=[item])
    proofs = source_corpus.Proofs(plan, (item,))
    corpus = source_corpus.Corpus(
        [cs.CorpusChunk(URI, 0, "Private content", ("Notes",), 3, {"page": 2})], 1, proofs
    )

    async def prepare(org, namespaces):
        events.append("prepare")
        assert org == "acme" and namespaces == ["team"]
        return plan

    async def budget(org: str, ns: str, *, user_id: str = ""):
        events.append("budget")
        return False

    class Model:
        model = "fixture"

        def __init__(self):
            self.calls = []

        async def complete(self, system, user, **kwargs):
            self.calls.append((system, user))
            events.append("provider")
            return SimpleNamespace(text=f"Private prose [{URI}#0]")

    model = Model()
    loader = AsyncMock(return_value=corpus)
    monkeypatch.setattr(source_access, "prepare", prepare)
    monkeypatch.setattr(source_corpus, "load", loader)
    monkeypatch.setattr("server.observability.budget.namespace_over_cap", budget)
    monkeypatch.setattr(clients, "get_llm_client", lambda: model)
    monkeypatch.setattr(settings, "memory_ask_synth_model", "")
    monkeypatch.setattr(
        "server.embedding.get_embedder",
        lambda: SimpleNamespace(model_name="fixture"),
    )
    return SimpleNamespace(
        plan=plan, proofs=proofs, corpus=corpus, model=model, loader=loader, events=events
    )


async def synthesize(**kwargs):
    return await app.handle_knowledge_synthesize(
        KnowledgeSynthesizeInput(
            org="acme", namespace="team", source_uris=[URI], task="Read this", **kwargs
        )
    )


async def test_preparation_precedes_budget_and_checked_loader_supplies_counts(world):
    result = await synthesize()
    assert world.events[:2] == ["prepare", "budget"]
    world.loader.assert_awaited_once()
    assert world.loader.call_args.args[:3] == (world.plan, "team", [URI])
    assert result.documents == result.windows == result.chunks_read == 1
    assert result.answer == f"Private prose [{URI}#0]"
    assert result.cited_chunks[0].source_uri == URI
    assert result.cited_chunks[0].revision == 3 and result.cited_chunks[0].locator == {"page": 2}
    assert len(world.model.calls) == 2
    assert world.plan.check.await_count == 5


async def test_preparation_also_precedes_a_budget_abstention(world, monkeypatch):
    budget = AsyncMock(return_value=True)
    monkeypatch.setattr("server.observability.budget.namespace_over_cap", budget)
    result = await synthesize()
    assert world.events == ["prepare"] and result == app._abstain("budget_capped")
    world.loader.assert_not_awaited()
    assert not world.model.calls


async def test_marked_provider_work_suppresses_public_diagnostics(world):
    from server.core.diagnostics import codes_only
    from server.observability.genai import current_stage

    async def complete(*_args, **_kwargs):
        assert codes_only()
        assert current_stage() in {"knowledge_synthesize_map", "knowledge_synthesize_reduce"}
        return SimpleNamespace(text="Private uncited prose")

    world.model.complete = complete
    assert not (await synthesize()).abstain


@pytest.mark.parametrize("boundary", [1, 2, 3, 4, 5])
async def test_proof_loss_before_or_after_each_provider_and_at_final_output_abstains(
    world, boundary
):
    admitted = world.plan.check.return_value
    world.plan.check.side_effect = [admitted] * (boundary - 1) + [[]]
    result = await synthesize()
    assert result == app._abstain("no_chunks")
    assert len(world.model.calls) == (boundary // 2)
    assert world.proofs.invalid


async def test_marked_ordinary_only_corpus_keeps_ordinary_fallback(world):
    world.loader.return_value = source_corpus.Corpus(
        [cs.CorpusChunk("ordinary", 0, "Ordinary")], 1, source_corpus.Proofs(world.plan, ())
    )
    world.plan.failed = True
    result = await synthesize()
    assert not result.abstain and result.documents == 1
    world.plan.check.assert_not_awaited()


async def test_unmarked_corpus_uses_unchanged_ordinary_loader(world, monkeypatch):
    world.plan.marked = False
    ordinary = AsyncMock(return_value=([cs.CorpusChunk("ordinary", 0, "Ordinary")], 1))
    monkeypatch.setattr(app, "_corpus_chunks", ordinary)
    result = await synthesize()
    ordinary.assert_awaited_once()
    world.loader.assert_not_awaited()
    assert not result.abstain and result.documents == 1
    world.plan.check.assert_not_awaited()


@pytest.mark.parametrize("failure", ["revoked", "provider", "cancelled"])
async def test_map_failure_cancels_and_drains_every_owned_sibling(world, failure):
    sibling_started, sibling_cancelled = asyncio.Event(), asyncio.Event()
    calls = 0
    world.corpus.chunks[:] = [cs.CorpusChunk(URI, idx, "Private" * 300) for idx in range(2)]

    async def complete(system, user, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            await sibling_started.wait()
            if failure == "provider":
                raise RuntimeError("provider failed")
            if failure == "cancelled":
                raise asyncio.CancelledError
            world.plan.check.return_value = []
            return SimpleNamespace(text="Private uncited prose")
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    world.model.complete = complete
    if failure == "revoked":
        result = await asyncio.wait_for(synthesize(window_chars=2000), timeout=5)
        assert result == app._abstain("no_chunks")
    else:
        error = RuntimeError if failure == "provider" else asyncio.CancelledError
        with pytest.raises(error):
            await asyncio.wait_for(synthesize(window_chars=2000), timeout=5)
    assert sibling_cancelled.is_set() and calls == 2


async def test_reduce_hierarchy_checks_every_provider_await(world):
    world.corpus.chunks[:] = [cs.CorpusChunk(URI, i, "x" * 1900) for i in range(3)]
    provider_calls = 0

    async def complete(system, user, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        return SimpleNamespace(text="Long note " * 230 if provider_calls <= 3 else "Answer")

    world.model.complete = complete
    result = await synthesize(window_chars=2000)
    assert not result.abstain and result.reduce_rounds > 1
    assert provider_calls > 4 and world.plan.check.await_count == provider_calls * 2 + 1
