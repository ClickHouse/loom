"""An external reranker's order is the order memory.search returns
(`server/memory/app/search/ranking.py::_run_external_rerank`)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search import ranking
from server.memory.domain.context_block import assemble_context_block


def _hit(key: str, score: float, session: str = "s1") -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=f"{key} in {session}",
        distance=1.0 - score,
        similarity=score,
        rank_score=score,
        source="fact",
        session_id=session,
        namespace="n",
    )


def _ctx(hits: list[_MemorySearchHit]) -> Any:
    return SimpleNamespace(
        input=SimpleNamespace(query="where does the user live now?", namespace="n"),
        endpoint="builtin:openai",
        prompt_template="",
        all_hits=hits,
        chronological_intent=None,
        actual_work={},
    )


def _reranker(order: list[str]) -> Any:
    """A reranker that returns the candidates whose excerpts are listed, in that order."""

    async def _rerank(*, candidates: list[Any], **_k: Any) -> list[Any]:
        by_text = {c.content_excerpt: c for c in candidates}
        return [by_text[t] for t in order]

    return _rerank


def _block(ctx: Any) -> list[str]:
    return [h.content_excerpt for h in assemble_context_block(ctx.all_hits, top_k=10)]


@pytest.mark.asyncio
async def test_the_block_keeps_the_rerankers_order(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx([_hit("user_name", 0.40), _hit("user_editor", 0.37), _hit("user_rust", 0.20)])
    order = ["user_editor in s1", "user_rust in s1", "user_name in s1"]
    monkeypatch.setattr("server.memory.app.rerank.rerank", _reranker(order))
    await ranking._run_external_rerank(ctx)
    assert _block(ctx) == order
    # The scores are the pool's own, handed out in the reranker's order.
    assert [h.rank_score for h in ctx.all_hits] == [0.40, 0.37, 0.20]
    assert ctx.all_hits[0].score_components["rank_score_pre_rerank"] == 0.37


@pytest.mark.asyncio
async def test_rows_of_one_key_from_two_sessions_both_survive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extraction writes a key once per session: the reranker ranks each row."""
    ctx = _ctx([_hit("user_location", 0.37, "s1"), _hit("user_location", 0.33, "s3")])
    order = ["user_location in s3", "user_location in s1"]
    monkeypatch.setattr("server.memory.app.rerank.rerank", _reranker(order))
    await ranking._run_external_rerank(ctx)
    assert _block(ctx) == order


@pytest.mark.asyncio
async def test_a_reranker_that_keeps_the_input_order_changes_no_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout or an unreadable reply keeps the input order: the scores stay as fused."""
    ctx = _ctx([_hit("user_name", 0.40), _hit("user_editor", 0.37)])

    async def _kept(*, candidates: list[Any], **_k: Any) -> list[Any]:
        return candidates

    monkeypatch.setattr("server.memory.app.rerank.rerank", _kept)
    await ranking._run_external_rerank(ctx)
    assert [(h.memory_key, h.rank_score) for h in ctx.all_hits] == [
        ("user_name", 0.40),
        ("user_editor", 0.37),
    ]


@pytest.mark.asyncio
async def test_the_confidence_gates_read_the_fused_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reranker's pick can answer in words the query does not share: its low cosine at
    rank 1 must not make the response abstain on the answer it serves."""
    from server.memory.app.search.verdict import _gate_top_sim, gate_order

    ctx = _ctx([_hit("user_name", 0.40), _hit("user_location", 0.25)])
    order = ["user_location in s1", "user_name in s1"]
    monkeypatch.setattr("server.memory.app.rerank.rerank", _reranker(order))
    await ranking._run_external_rerank(ctx)
    block = assemble_context_block(ctx.all_hits, top_k=10)
    assert [h.memory_key for h in block] == ["user_location", "user_name"]
    assert [h.memory_key for h in gate_order(block)] == ["user_name", "user_location"]
    assert _gate_top_sim(block) == pytest.approx(0.40)


def test_an_unreranked_block_is_gated_as_served() -> None:
    from server.memory.app.search.verdict import gate_order

    block = [_hit("b", 0.30), _hit("a", 0.50)]
    assert gate_order(block) is block


@pytest.mark.asyncio
async def test_a_score_tie_still_gates_on_the_fused_rank_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under rrf two rows often share a fused score; the fused order then breaks the tie
    on distance, and the gates read that row, not the reranker's pick."""
    from server.memory.app.search.verdict import _gate_top_sim, gate_order

    near, far = _hit("near", 0.5), _hit("far", 0.5)
    near.distance, far.distance = 0.3, 0.6
    ctx = _ctx([near, far])
    monkeypatch.setattr("server.memory.app.rerank.rerank", _reranker(["far in s1", "near in s1"]))
    await ranking._run_external_rerank(ctx)
    block = assemble_context_block(ctx.all_hits, top_k=10)
    assert [h.memory_key for h in block] == ["far", "near"]
    assert _gate_top_sim(block) == pytest.approx(0.7)
    gated = gate_order(block)
    assert 1.0 - gated[1].distance == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_the_retry_gate_reads_the_fused_rank_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The quality recipe's judge runs for a mid-range fused top cosine (0.35-0.65); the
    reranker's low-cosine pick at rank 1 must not skip it."""
    from server.memory.app import search_llm
    from server.memory.app.search import fallbacks

    fused_top, pick = _hit("user_location", 0.5), _hit("user_name", 0.3)
    # As the rerank trail stamps them: the pick moved up from 1 to 0, the fused top down.
    fused_top.score_components.update({"rerank_from": 0.0, "rerank_delta": -1.0})
    pick.score_components.update({"rerank_from": 1.0, "rerank_delta": 1.0})
    judged: list[str] = []

    async def _judge(query: str, excerpts: list[str]) -> tuple[bool, str]:
        judged.append(query)
        return True, ""

    budget = SimpleNamespace(
        fallback_affordable=lambda **_k: True,
        try_spend_llm=lambda _n: True,
        note_llm_call=lambda _n: None,
    )
    ctx = SimpleNamespace(
        input=SimpleNamespace(query="q", recipe="quality", _pass_depth=0),
        final_hits=[pick, fused_top],
        chronological_intent=None,
        llm_free=False,
        search_budget=budget,
        namespaces=["n"],
        actual_work={},
    )
    monkeypatch.setattr(search_llm, "judge_results_iteratively", _judge)
    await fallbacks._maybe_iterative_retry(ctx)
    assert judged == ["q"]


def test_a_reranked_chunks_lift_is_over_its_own_score() -> None:
    """The bounded lift records how far the parent boost lifted the row; a reranked row
    holds another rank's score, so the lift is read against its own fused score."""
    from server.memory.domain.query_planner import bound_component_lift

    chunk, fact = _hit("chunk", 0.9), _hit("fact", 0.5)
    chunk.score_components.update({"chunk_parent_boost": 0.3, "rank_score_pre_rerank": 0.7})
    assert bound_component_lift([chunk, fact]) == 1
    assert chunk.rank_score == pytest.approx(0.5 + 1e-6)
    # Own un-boosted score 0.7 - 0.3 = 0.4: lifted to just above the 0.5 ceiling.
    assert chunk.score_components["chunk_parent_lift"] == pytest.approx(0.1 + 1e-6)


@pytest.mark.asyncio
async def test_a_retrys_rows_carry_no_rerank_position_into_the_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry reranks its own pool: its positions are not on this pool's scale, so the
    rows it merges in are gated where they land, like any row that joins after the rerank."""
    import server.memory.app.search as search_pkg
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app import search_llm
    from server.memory.app.search import fallbacks

    parent = _hit("user_location", 0.5)
    parent.score_components["rerank_from"] = 0.0
    retry_row = _hit("user_employer", 0.45)
    retry_row.score_components.update(
        {"rerank_from": 0.0, "rerank_pos": 2.0, "rerank_delta": -2.0, "rerank_external": 1.0}
    )

    async def _judge(query: str, excerpts: list[str]) -> tuple[bool, str]:
        return False, "where does the user work?"

    async def _search(_input: Any) -> Any:
        return SimpleNamespace(results=[retry_row])

    budget = SimpleNamespace(
        fallback_affordable=lambda **_k: True,
        try_spend_llm=lambda _n: True,
        note_llm_call=lambda _n: None,
        try_spend_fallback=lambda **_k: True,
        remaining_wall_s=lambda: 5.0,
    )
    ctx = SimpleNamespace(
        input=MemorySearchInput(org="o", namespace="n", query="q", recipe="quality", top_k=5),
        final_hits=[parent],
        chronological_intent=None,
        llm_free=False,
        search_budget=budget,
        namespaces=["n"],
        actual_work={},
    )
    monkeypatch.setattr(search_llm, "judge_results_iteratively", _judge)
    monkeypatch.setattr(search_pkg, "handle_memory_search", _search)
    await fallbacks._maybe_iterative_retry(ctx)
    merged = {h.memory_key: h for h in ctx.final_hits}
    assert set(merged) == {"user_location", "user_employer"}
    assert not {k for k in merged["user_employer"].score_components if k.startswith("rerank_")}
    assert merged["user_location"].score_components["rerank_from"] == 0.0


@pytest.mark.asyncio
async def test_a_reranker_that_moved_nothing_leaves_the_floors_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout keeps the fused order; the floor then puts a weak lexical rank 1 last. The
    gates must read what an unreranked search would: the floor's order."""
    from server.memory.app.search.verdict import _gate_top_sim, gate_order

    weak, strong = _hit("lexical", 0.9), _hit("cosine", 0.5)
    weak.distance, strong.distance = 0.85, 0.4  # cosines 0.15 and 0.6

    async def _kept(*, candidates: list[Any], **_k: Any) -> list[Any]:
        return candidates

    ctx = _ctx([weak, strong])
    monkeypatch.setattr("server.memory.app.rerank.rerank", _kept)
    await ranking._run_external_rerank(ctx)
    weak.score_components["below_floor"] = 1.0
    floored = [strong, weak]  # the floor's readmit order: kept rows, then fillers
    assert gate_order(floored) is floored
    assert _gate_top_sim(floored) == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_a_row_under_the_floor_stays_last_after_a_real_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.search.verdict import _gate_top_sim, gate_order

    weak, a, b = _hit("lexical", 0.9), _hit("a", 0.5), _hit("b", 0.4)
    weak.distance, a.distance, b.distance = 0.85, 0.4, 0.5
    ctx = _ctx([weak, a, b])
    order = ["b in s1", "lexical in s1", "a in s1"]
    monkeypatch.setattr("server.memory.app.rerank.rerank", _reranker(order))
    await ranking._run_external_rerank(ctx)
    weak.score_components["below_floor"] = 1.0
    floored = [b, a, weak]
    assert [h.memory_key for h in gate_order(floored)] == ["a", "b", "lexical"]
    assert _gate_top_sim(floored) == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_the_calibrator_reads_the_fused_top_and_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict phase feeds the calibrator the fused rank 1 and rank 2 cosines, and does
    not abstain on a reranked answer whose own cosine is low."""
    from server.memory.app.search import verdict
    from server.memory.domain import retrieval_calibration

    top, second, pick = _hit("top", 0.6), _hit("second", 0.5), _hit("pick", 0.25)
    ctx = _ctx([top, second, pick])
    order = ["pick in s1", "top in s1", "second in s1"]
    monkeypatch.setattr("server.memory.app.rerank.rerank", _reranker(order))
    await ranking._run_external_rerank(ctx)
    block = assemble_context_block(ctx.all_hits, top_k=10)
    assert [h.memory_key for h in block] == ["pick", "top", "second"]

    seen: dict[str, Any] = {}
    real = retrieval_calibration.calibrate_retrieval

    def _spy(**kw: Any) -> Any:
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(retrieval_calibration, "calibrate_retrieval", _spy)
    ctx.actual_work, ctx.query_triples = {}, []
    run = SimpleNamespace(ns_calibration=None, graph_seeds=0, graph_neighbors=0, triples_matched=0)
    _cal, (abstain, _code, _reason) = verdict._calibrate_final_hits(ctx, run, block, 0)
    assert seen["top_similarity"] == pytest.approx(0.6)
    assert seen["second_similarity"] == pytest.approx(0.5)
    assert abstain is False
