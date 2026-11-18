"""Oversized summary inputs refuse the composed read instead of losing a leg."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.errors import VectorInputTooLargeError
from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput, _MemorySearchHit
from server.memory.app.ask import pool, retrieval_legs
from server.memory.app.loom_search import fanout, repo_decisions
from server.memory.app.search import consistency, fallbacks


def _input(**changes: Any) -> MemorySearchInput:
    return MemorySearchInput(org="org", namespace="home", query="question").model_copy(
        update=changes
    )


@pytest.fixture(params=[VectorInputTooLargeError, RuntimeError], ids=["refusal", "ordinary"])
def failure(request: pytest.FixtureRequest) -> Exception:
    return request.param("summary seed input exceeds 256 names")


async def _nothing(*_args: Any, **_kwargs: Any) -> list[Any]:
    return []


async def test_decomposition_preserves_typed_refusal(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def parts(*_args: Any, **_kwargs: Any) -> list[str]:
        return ["part one", "part two"]

    calls: list[str] = []

    async def search(inp: MemorySearchInput) -> MemorySearchOutput:
        calls.append(inp.query)
        raise failure

    monkeypatch.setattr(retrieval_legs, "decompose_question", parts)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", search)
    stats = {"failed": 0}
    if isinstance(failure, VectorInputTooLargeError):
        with pytest.raises(VectorInputTooLargeError) as raised:
            await retrieval_legs.decomposed_retrieval(
                _input(), "question", [], llm=None, mode="always", stats=stats
            )
        assert raised.value is failure
        assert calls == ["part one"] and stats == {"failed": 0}
    else:
        parts_out, ranked = await retrieval_legs.decomposed_retrieval(
            _input(), "question", [], llm=None, mode="always", stats=stats
        )
        assert parts_out == ["part one", "part two"] and ranked == []
        assert calls == parts_out and stats == {"failed": 2}


@pytest.mark.parametrize("route", ["compound", "bridge"])
async def test_question_widening_preserves_typed_refusal(
    route: str, failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[MemorySearchInput] = []

    async def search(inp: MemorySearchInput) -> MemorySearchOutput:
        calls.append(inp)
        raise failure

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", search)
    monkeypatch.setattr(
        pool,
        "split_compound_question",
        lambda _q: ["first", "second"] if route == "compound" else [],
    )
    monkeypatch.setattr(pool, "is_chain_question", lambda _q: route == "bridge")
    monkeypatch.setattr(pool, "bridge_entities", lambda *_a, **_k: ["bridge"])
    monkeypatch.setattr(pool, "bridge_rows_by_key", _nothing)
    run = SimpleNamespace(
        search_input=_input(),
        question="question",
        ranked=[],
        settings=SimpleNamespace(memory_ask_entity_follow_max=2, memory_ask_entity_follow_top_k=6),
    )
    if isinstance(failure, VectorInputTooLargeError):
        with pytest.raises(VectorInputTooLargeError) as raised:
            await pool._widen_by_question(run, llm=None, llm_cheap=None, decompose="off")
        assert raised.value is failure
    else:
        await pool._widen_by_question(run, llm=None, llm_cheap=None, decompose="off")
        assert run.ranked == []
    assert len(calls) == 1
    assert (calls[0].org, calls[0].namespace) == ("org", "home")


@pytest.mark.parametrize("route", ["fresh-hit", "empty"])
async def test_consistency_reread_preserves_typed_refusal(
    route: str, failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def search(inp: MemorySearchInput) -> MemorySearchOutput:
        assert (inp.org, inp.namespace, inp._pass_depth) == ("org", "home", 1)
        raise failure

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", search)
    monkeypatch.setattr(
        consistency, "_fresh_hit_reread_wanted", lambda *_a, **_k: route == "fresh-hit"
    )
    monkeypatch.setattr(consistency, "_sequential_retry_wanted", lambda **_k: route == "empty")
    monkeypatch.setattr("server.memory.store.read_your_writes_enabled", lambda: True)
    budget = SimpleNamespace(wall_left_s=lambda: 10.0, remaining_wall_s=lambda: 10.0)
    ctx = SimpleNamespace(input=_input(), search_budget=budget, namespaces=["home"])
    hits = [
        _MemorySearchHit(
            memory_key="existing",
            memory_type="semantic",
            content_excerpt="kept",
            distance=0.5,
            similarity=0.5,
        )
    ]
    if isinstance(failure, VectorInputTooLargeError):
        with pytest.raises(VectorInputTooLargeError) as raised:
            await consistency._consistency_reread(ctx, hits)
        assert raised.value is failure
    else:
        assert await consistency._consistency_reread(ctx, hits) is None
    assert hits[0].memory_key == "existing"


async def test_iterative_retry_preserves_typed_refusal(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def judge(*_args: Any) -> tuple[bool, str]:
        return False, "rephrased question"

    async def search(inp: MemorySearchInput) -> MemorySearchOutput:
        assert inp.query == "rephrased question" and inp._pass_depth == 1
        raise failure

    monkeypatch.setattr("server.memory.app.search_llm.judge_results_iteratively", judge)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", search)
    budget = SimpleNamespace(
        fallback_affordable=lambda **_k: True,
        try_spend_llm=lambda _n: True,
        note_llm_call=lambda _n: None,
        try_spend_fallback=lambda **_k: True,
        remaining_wall_s=lambda: 10.0,
    )
    hits = [
        _MemorySearchHit(
            memory_key="existing",
            memory_type="semantic",
            content_excerpt="kept",
            distance=0.5,
            similarity=0.5,
        )
    ]
    ctx = SimpleNamespace(
        input=_input(recipe="quality"),
        final_hits=hits,
        chronological_intent=False,
        llm_free=False,
        search_budget=budget,
        namespaces=["home"],
        actual_work={},
    )
    if isinstance(failure, VectorInputTooLargeError):
        with pytest.raises(VectorInputTooLargeError) as raised:
            await fallbacks._maybe_iterative_retry(ctx)
        assert raised.value is failure
    else:
        await fallbacks._maybe_iterative_retry(ctx)
    assert ctx.final_hits == hits


async def test_repository_search_preserves_typed_refusal(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def search(inp: MemorySearchInput) -> MemorySearchOutput:
        assert (inp.org, inp.namespace) == ("org", "repository")
        raise failure

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", search)
    if isinstance(failure, VectorInputTooLargeError):
        with pytest.raises(VectorInputTooLargeError) as raised:
            await repo_decisions.search_repo_decisions(_input(), "repository")
        assert raised.value is failure
    else:
        assert await repo_decisions.search_repo_decisions(_input(), "repository") is None


async def test_section_gather_preserves_typed_refusal(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def namespaces(_org: str, ns: str, _scope: str) -> tuple[list[str], list[str]]:
        return [ns], []

    async def memory(inp: MemorySearchInput, **_kwargs: Any) -> MemorySearchOutput:
        assert (inp.org, inp.namespace) == ("org", "home")
        raise failure

    async def prepare(*_args: Any) -> Any:
        return None, SimpleNamespace(marked=False)

    async def unavailable(*_args: Any) -> Any:
        raise RuntimeError("ordinary unavailable section")

    monkeypatch.setattr("server.auth.fanout.namespaces_to_search", namespaces)
    monkeypatch.setattr(fanout, "request_scope", lambda: None)
    monkeypatch.setattr(fanout, "memory_with_repo_decisions", memory)
    monkeypatch.setattr(fanout, "_prepare_knowledge", prepare)
    monkeypatch.setattr(fanout, "_run_knowledge", unavailable)
    monkeypatch.setattr(fanout, "search_profile", _nothing)
    monkeypatch.setattr(fanout, "_conversation_leg", unavailable)
    monkeypatch.setattr(fanout, "tracker_leg", _nothing)
    monkeypatch.setattr(fanout, "answer_first", lambda payload, *_a, **_k: payload)
    kwargs = dict(
        org="org",
        namespace="home",
        agent="a",
        query="question",
        top_k=3,
        tags=[],
        namespace_scope="this",
        recipe="",
        bodies="excerpt",
        page="",
        explain=False,
        repo_namespace="",
        tracker_plan=None,
    )
    if isinstance(failure, VectorInputTooLargeError):
        with pytest.raises(VectorInputTooLargeError) as raised:
            await fanout.search_sections(**kwargs)
        assert raised.value is failure
    else:
        result = await fanout.search_sections(**kwargs)
        assert result["memory"]["results"] == []
        assert result["memory"]["error"] == "RuntimeError"
        assert result["knowledge"]["results"] == []


async def test_repository_base_failure_joins_its_owned_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, finished = asyncio.Event(), asyncio.Event()
    refusal = VectorInputTooLargeError("summary input exceeds 256 names")
    child: asyncio.Task[Any] | None = None

    async def extra(*_args: Any) -> None:
        nonlocal child
        child = asyncio.current_task()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.set()

    async def search(_inp: MemorySearchInput) -> MemorySearchOutput:
        await started.wait()
        raise refusal

    monkeypatch.setattr(repo_decisions, "search_repo_decisions", extra)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", search)
    try:
        with pytest.raises(VectorInputTooLargeError) as raised:
            await repo_decisions.memory_with_repo_decisions(_input(), repo_namespace="repository")
        assert raised.value is refusal
        assert finished.is_set() and child is not None and child.done()
    finally:
        if child is not None:
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)
