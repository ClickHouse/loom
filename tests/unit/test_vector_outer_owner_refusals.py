"""A complete-read refusal survives optional outer owners and owned cleanup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from server.core.errors import (
    VectorAdmissionError,
    VectorCapacityError,
    VectorInputTooLargeError,
    VectorOverloadedError,
)
from server.knowledge.app.source_access import ReadPlan
from server.memory.app.ask.source_context import PreparedKnowledge
from server.settings import settings
from tests.unit.test_vector_capacity_owner_escapes import native as native

BODY = "The entire ordinary answer remains available.\nThe second line remains too."


def failure(status):
    if status == 429:
        return VectorAdmissionError("whole read refused", reason="rows", retry_after_s=7)
    if status == 422:
        return VectorInputTooLargeError("whole input refused")
    assert status == 503
    return VectorOverloadedError("whole process refused", reason="pool", retry_after_s=7)


def assert_refusal(actual, expected, status):
    assert actual is expected
    assert actual.status_code == status
    detail = {
        "code": {429: "VECTOR_CAPACITY_REFUSED", 422: "VECTOR_INPUT_TOO_LARGE", 503: "OVERLOADED"}[
            status
        ],
        "message": str(expected),
    }
    if status != 422:
        detail.update(reason="rows" if status == 429 else "pool", retry_after_s=7)
    assert actual.to_detail() == detail
    assert actual.headers() == (None if status == 422 else {"Retry-After": "7"})


async def join_owned(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)


@pytest.mark.parametrize(
    "site,status",
    [
        ("memories", 429),
        ("procedures", 422),
        ("knowledge", 503),
        ("memory_stack", 429),
        ("conversations", 503),
    ],
)
@pytest.mark.parametrize("capacity", [True, False], ids=["refusal", "ordinary-fallback"])
async def test_public_pack_preserves_requested_section_refusal(monkeypatch, site, status, capacity):
    from server.knowledge.app.knowledge_search import SearchRequest
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app import context_pack
    from tests.unit.checked_pack_fakes import install_ordinary_pack

    install_ordinary_pack(monkeypatch)
    expected = failure(status) if capacity else RuntimeError("ordinary section unavailable")
    calls = []

    async def profile(inp):
        assert (inp.org, inp.namespace) == ("acme", "docs")
        return NS(rendered=BODY, labels=["ordinary"], section_count=1)

    async def refused(inp, *rest, **kwargs):
        actual = inp.input if isinstance(inp, SearchRequest) else inp
        assert (actual.org, actual.namespace) == ("acme", "docs")
        assert (
            getattr(actual, "query", None)
            or getattr(actual, "situation", None)
            or getattr(actual, "seed_query", None)
        ) == "What is known about 109741?"
        calls.append(actual)
        raise expected

    monkeypatch.setattr("server.memory.app.profile.handle_profile_render", profile)
    targets = {
        "memories": "server.memory.app.search.handle_memory_search",
        "procedures": "server.memory.app.procedure.handle_procedure_match",
        "knowledge": "server.knowledge.app.source_passages.collect",
        "memory_stack": "server.memory.app.stack.run_checked_stack",
        "conversations": "server.knowledge.app.conversations.handle_conversation_search",
    }
    monkeypatch.setattr(targets[site], refused)
    inp = ContextPackInput(
        agent="reader",
        org="acme",
        namespace="docs",
        query="What is known about 109741?",
        only_sections=["profile", site],
        include_knowledge=site in {"knowledge", "memory_stack"},
        include_conversations=site == "conversations",
    )
    try:
        if capacity:
            with pytest.raises(VectorCapacityError) as caught:
                await context_pack.handle_context_pack(inp)
            assert_refusal(caught.value, expected, status)
        else:
            out = await context_pack.handle_context_pack(inp)
            assert out.sections == {"profile": BODY}
            assert [(b.section, b.rendered, b.citations, b.source_count) for b in out.blocks] == [
                ("profile", BODY, ["profile:ordinary"], 1)
            ]
            assert out.section_count == 1 and out.truncated is False
            assert BODY in out.rendered
    finally:
        assert len(calls) == 1


async def test_public_pack_joins_other_requested_section_before_refusal(monkeypatch):
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    started, settled = asyncio.Event(), asyncio.Event()
    tasks = set()
    expected = failure(429)

    async def profile(inp):
        tasks.add(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    async def search(inp):
        assert (inp.org, inp.namespace, inp.query) == ("acme", "docs", "109741")
        await started.wait()
        raise expected

    monkeypatch.setattr("server.memory.app.profile.handle_profile_render", profile)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", search)
    caller = asyncio.create_task(
        handle_context_pack(
            ContextPackInput(
                agent="reader",
                org="acme",
                namespace="docs",
                query="109741",
                only_sections=["profile", "memories"],
                include_knowledge=False,
            )
        )
    )
    tasks.add(caller)
    try:
        await asyncio.wait({caller}, timeout=1)
        assert settled.is_set(), (
            "a refused pack did not cancel and join its other requested section"
        )
        with pytest.raises(VectorCapacityError) as caught:
            await asyncio.wait_for(asyncio.shield(caller), 1)
        assert_refusal(caught.value, expected, 429)
        assert all(t.done() for t in tasks)
    finally:
        await join_owned(tasks)


@pytest.mark.parametrize("capacity", [True, False], ids=["refusal", "ordinary-fallback"])
async def test_code_answer_cannot_synthesize_after_bound_memory_refusal(monkeypatch, capacity):
    from server.code.app import answers

    expected = failure(503) if capacity else RuntimeError("ordinary memory failure")
    reads, syntheses = [], []
    monkeypatch.setattr(settings, "code_ask_memory_namespace", "docs")
    monkeypatch.setattr(settings, "code_ask_memory_org", "acme")

    async def allow(*args, **kwargs):
        return True

    async def search(inp):
        assert (inp.org, inp.namespace, inp.query, inp.namespace_scope, inp.include_tags) == (
            "acme",
            "docs",
            "Why FooBar?",
            "this",
            True,
        )
        reads.append(inp)
        raise expected

    async def synthesize(llm, question, units, *, memories, history):
        syntheses.append((question, units, memories, history))
        return BODY

    monkeypatch.setattr("server.auth.authz.row_in_scope", allow)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", search)
    monkeypatch.setattr(answers, "_synthesize", synthesize)
    monkeypatch.setattr(answers, "_memory_context_cacheable", allow)
    ctx = NS(repo=None, org="acme", ns="code", question="Why FooBar?", model="fake")
    retrieval = NS(units=[])
    try:
        if capacity:
            with pytest.raises(VectorCapacityError) as caught:
                await answers._grounded_answer(ctx, None, retrieval, ["Prior full turn"])
            assert_refusal(caught.value, expected, 503)
            assert syntheses == []
        else:
            out = await answers._grounded_answer(ctx, None, retrieval, ["Prior full turn"])
            assert out.answer == BODY and out.source == "synthesized"
            assert out.citations == [] and out.memory_keys == [] and out.memory_namespace == ""
            assert syntheses == [("Why FooBar?", [], [], ["Prior full turn"])]
    finally:
        assert len(reads) == 1


@pytest.mark.parametrize("capacity", [True, False], ids=["refusal", "ordinary-fallback"])
async def test_memory_grounding_preserves_refusal_and_full_ordinary_pool(monkeypatch, capacity):
    from server.memory.app import code_grounding as grounding

    expected = failure(422) if capacity else RuntimeError("ordinary code search failure")
    calls = []
    pool = [NS(memory_key="known", content_excerpt=BODY)]

    async def target(*args):
        return "acme", "code"

    async def search(inp):
        assert (inp.org, inp.namespace, inp.query, inp.consumer, inp.top_k) == (
            "acme",
            "code",
            "where is helper_fn",
            "memory.ask",
            3,
        )
        calls.append(inp)
        raise expected

    monkeypatch.setattr(grounding, "_authorized_code_target", target)
    monkeypatch.setattr("server.code.domain.idents.embedded_idents", lambda query: {"helper_fn"})
    monkeypatch.setattr("server.code.app.search.handler.handle_code_search", search)
    cfg = settings.model_copy(
        update={
            "memory_ask_code_hop_symbols": 0,
            "memory_ask_pr_temporal_gate": False,
            "memory_ask_code_enrichment": True,
            "memory_ask_pr_grounding": False,
            "memory_ask_code_staleness": False,
        }
    )
    try:
        if capacity:
            with pytest.raises(VectorCapacityError) as caught:
                await grounding.apply_code_grounding(
                    "where is helper_fn", NS(org="acme", namespace="docs"), pool, cfg
                )
            assert_refusal(caught.value, expected, 422)
        else:
            out = await grounding.apply_code_grounding(
                "where is helper_fn", NS(org="acme", namespace="docs"), pool, cfg
            )
            assert out is pool and out[0].content_excerpt == BODY
    finally:
        assert len(calls) == 1


async def test_code_grounding_joins_inner_symbol_and_outer_gate_on_refusal(monkeypatch):
    from server.memory.app import code_grounding as grounding

    inner_started, outer_started = asyncio.Event(), asyncio.Event()
    inner_settled, outer_settled = asyncio.Event(), asyncio.Event()
    tasks = set()
    expected = failure(429)

    async def target(*args):
        return "acme", "code"

    async def block(started, settled):
        tasks.add(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    async def symbol(inp):
        assert inp.name == "FooBar" and inp.consumer == "memory.ask"
        return await block(inner_started, inner_settled)

    async def gate(*args, **kwargs):
        return await block(outer_started, outer_settled)

    async def search(inp):
        assert (inp.org, inp.namespace, inp.consumer) == ("acme", "code", "memory.ask")
        await inner_started.wait()
        await outer_started.wait()
        raise expected

    monkeypatch.setattr(grounding, "_authorized_code_target", target)
    monkeypatch.setattr(grounding, "_gate_future_prs", gate)
    monkeypatch.setattr("server.code.domain.idents.embedded_idents", lambda query: {"FooBar"})
    monkeypatch.setattr("server.code.app.symbol.handle_code_symbol", symbol)
    monkeypatch.setattr("server.code.app.search.handler.handle_code_search", search)
    cfg = settings.model_copy(
        update={
            "memory_ask_code_hop_symbols": 0,
            "memory_ask_pr_temporal_gate": True,
            "memory_ask_code_enrichment": True,
            "memory_ask_pr_grounding": False,
            "memory_ask_code_staleness": False,
        }
    )
    caller = asyncio.create_task(
        grounding.apply_code_grounding("FooBar", NS(org="acme", namespace="docs"), [], cfg)
    )
    tasks.add(caller)
    try:
        await asyncio.wait({caller}, timeout=1)
        assert inner_settled.is_set() and outer_settled.is_set(), (
            "refused grounding did not cancel and join both owned fanout levels"
        )
        with pytest.raises(VectorCapacityError) as caught:
            await asyncio.wait_for(asyncio.shield(caller), 1)
        assert_refusal(caught.value, expected, 429)
        assert all(t.done() for t in tasks)
    finally:
        await join_owned(tasks)


@pytest.mark.parametrize("wide", [False, True], ids=["initial", "lazy-wide"])
@pytest.mark.parametrize("capacity", [True, False], ids=["refusal", "ordinary-fallback"])
async def test_corpus_route_preserves_knowledge_refusal(monkeypatch, wide, capacity):
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app.knowledge_search import SearchRequest
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.app.ask import handler
    from tests.unit.ask_knowledge_fake import checked_reply

    expected = failure(429) if capacity else RuntimeError("ordinary corpus failure")
    widths = []

    async def search(request, plan):
        assert (request.input.org, request.input.namespace, request.input.query) == (
            "acme",
            "docs",
            "Summarize these reports",
        )
        assert plan.org == "acme"
        widths.append(request.input.top_k)
        if wide and len(widths) == 1:
            # A full first page consists entirely of another agent's rules, so real
            # served_hits must widen before selecting the corpus documents.
            return checked_reply(
                NS(
                    results=[
                        NS(
                            source_uri="doc:excluded",
                            tags=["source-kind:assistant_turn", "agent:other"],
                        )
                        for _ in range(request.input.top_k)
                    ]
                )
            )
        raise expected

    monkeypatch.setattr("server.knowledge.app.knowledge_search.run_checked_search", search)
    inp = MemoryAskInput(
        org="acme",
        namespace="docs",
        agent="reader",
        query="Summarize these reports",
        knowledge=False,
    )
    prepared = PreparedKnowledge(
        SearchRequest(
            KnowledgeSearchInput(org="acme", namespace="docs", query=inp.query), ("docs",), ()
        ),
        ReadPlan("acme"),
    )
    try:
        if capacity:
            with pytest.raises(VectorCapacityError) as caught:
                await handler._maybe_route_to_synthesize(inp, prepared)
            assert_refusal(caught.value, expected, 429)
        else:
            assert await handler._maybe_route_to_synthesize(inp, prepared) is None
    finally:
        assert widths[0] == 20
        assert len(widths) == (2 if wide else 1)
        if wide:
            assert widths[1] > widths[0]


@pytest.mark.parametrize("mode", ["refusal", "cancel-only", "success"])
async def test_ask_pipeline_preserves_first_refusal_during_repeated_cleanup_cancel(
    native, monkeypatch, mode
):
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.ask import identifiers, pipeline
    from server.memory.app.ask.result import MemoryAskResult

    native.site, native.failure = "fact", failure(503)
    started, cleaning, release, settled = (asyncio.Event() for _ in range(4))
    tasks = set()
    ordinary = MemoryAskResult(
        answer=BODY, source="retrieval_only", memory_keys=["complete"], results=[]
    )

    async def knowledge(*args, **kwargs):
        tasks.add(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            settled.set()

    async def fill(run, **kwargs):
        await started.wait()
        if mode == "cancel-only":
            await asyncio.Event().wait()
        if mode == "refusal":
            # Actual corrected identifier owner and real store SQL dispatch; the
            # existing RecordingCH fixture asserts native attribution before raise.
            await identifiers._inject_identifier_memories(run.search_input, ["109741"], [])
        return False, False, False

    async def early(*args, **kwargs):
        return ordinary, False

    monkeypatch.setattr(pipeline, "knowledge_passages", knowledge)
    monkeypatch.setattr(
        pipeline, "_ask_pool_size", lambda inp, *args, **kwargs: NS(search_input=inp)
    )
    monkeypatch.setattr(pipeline, "_fill_pool", fill)
    monkeypatch.setattr(pipeline, "_early_answer", early)
    request = MemorySearchInput(org="acme", namespace="docs", query="What is known about 109741?")
    caller = asyncio.create_task(
        pipeline.ask(
            request, llm=None, prepared_knowledge=PreparedKnowledge(None, ReadPlan("acme"))
        )
    )
    tasks.add(caller)
    waiter = asyncio.create_task(cleaning.wait())
    tasks.add(waiter)
    try:
        await asyncio.wait_for(started.wait(), 1)
        if mode == "cancel-only":
            caller.cancel()
        done, _ = await asyncio.wait(
            {caller, waiter}, timeout=1, return_when=asyncio.FIRST_COMPLETED
        )
        assert done and cleaning.is_set(), "ask returned without starting owned knowledge cleanup"
        assert not caller.done() and not settled.is_set()
        if mode != "success":
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done() and not settled.is_set()
        release.set()
        if mode == "refusal":
            with pytest.raises(VectorCapacityError) as caught:
                await asyncio.wait_for(asyncio.shield(caller), 1)
            assert_refusal(caught.value, native.failure, 503)
        elif mode == "cancel-only":
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(caller), 1)
        else:
            assert await asyncio.wait_for(asyncio.shield(caller), 1) is ordinary
            assert ordinary.answer == BODY and ordinary.memory_keys == ["complete"]
        assert settled.is_set()
        assert all(t.done() for t in tasks)
    finally:
        release.set()
        await join_owned(tasks)
        assert len(native.refused) == (1 if mode == "refusal" else 0)
