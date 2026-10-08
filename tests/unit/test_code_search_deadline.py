"""`code.search` latency for real callers.

- `llm_legs="none"` skips the model-backed legs (HyDE) and keeps the plain ones;
- `deadline_ms` is the request's budget: the legs get what it leaves after the
  post-leg reserve, a leg still running then is dropped and named
  (`legs_timed_out`, `epistemic: partial`, `partial_reason`), the first content
  leg is waited for inside the budget, HyDE is not started when the budget
  cannot pay for it, and rerank is skipped or cut at what is left;
- the model-leg grace bounds HyDE past the plain legs;
- one provider embed per query across the vector leg, its prefix twin and the
  card leg, and a repeated query is a cache hit;
- the op prices symbol / hybrid / hybrid+HyDE requests by their own budgets.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import search as cs
from server.code.app import search_legs as legs
from server.code.app.search import gather as cs_gather
from server.code.app.search import rerank as cs_rerank
from server.code.app.search.rerank import _rerank_stage
from server.code.app.search.voting import _voting_leg_config
from server.settings import settings

_UNIT = dict(
    scope="f",
    kind="function",
    start_line=1,
    end_line=5,
    content="x",
    symbol_ids=[],
    context_description="",
)
_QUESTION = "where is the memory limit for a query enforced"


class _Repo:
    def __init__(self, *, lexical_s: float = 0.0, vector_s: float = 0.0) -> None:
        self.lexical_s, self.vector_s = lexical_s, vector_s
        self.vector_calls: list[list[float]] = []
        self.shortlist_calls: list[list[float]] = []

    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        await asyncio.sleep(self.lexical_s)
        return [{"chunk_id": "L1", "path": "src/a.cpp", "lex_score": 4.0, **_UNIT}]

    async def vector_search_chunks(self, org, ns, model, vec, **k):
        self.vector_calls.append(list(vec))
        if vec[0] == 9.0:  # the passage's vector
            return [
                {"chunk_id": "H1", "path": "src/Common/MemoryTracker.cpp", "distance": 0.2, **_UNIT}
            ]
        await asyncio.sleep(self.vector_s)
        return [{"chunk_id": "V1", "path": "src/b.cpp", "distance": 0.3, **_UNIT}]

    async def vector_shortlist_ids(self, org, ns, model, vec, **k):
        self.shortlist_calls.append(list(vec))
        return [{"chunk_id": "V1", "path": "src/b.cpp", "distance": 0.3}]

    async def exact_rerank_by_ids(self, org, ns, model, vec, keys, **k):
        return [{"chunk_id": "V1", "path": "src/b.cpp", "distance": 0.3, **_UNIT}]

    async def vector_search_cards(self, *a, **k):
        return [{"path": "src/b.cpp", "summary": "s", "pagerank": 0.1, "distance": 0.2}]

    async def best_chunks_for_paths(self, *a, **k):
        return []

    async def chunks_for_paths(self, *a, **k):
        return []

    async def symbol_names_for_paths(self, *a, **k):
        return []

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        if name == "MemoryTracker::allocImpl":
            return [
                {
                    "symbol_id": "S1",
                    "path": "src/Common/MemoryTracker.cpp",
                    "start_line": 10,
                    "end_line": 40,
                    "qualified_name": name,
                    "signature": "void allocImpl()",
                }
            ]
        return []

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text: str) -> list[float]:
        return [9.0, 0.0, 0.0, 0.0] if "MemoryTracker" in text else [0.5, 0.5, 0.5, 0.5]


class _LLM:
    delay_s = 0.0
    calls = 0

    async def complete(self, system, user, max_tokens=220):
        _LLM.calls += 1
        await asyncio.sleep(_LLM.delay_s)

        class R:
            text = (
                "DB::MemoryTracker::allocImpl in src/Common/MemoryTracker.cpp enforces the "
                "per-query memory limit."
            )

        return R()


def _run(
    repo: _Repo | None = None,
    *,
    llm_delay: float = 0.0,
    grace_ms: int = 0,
    deadline_setting: int = 0,
    **kw: Any,
) -> tuple[_Repo, Any, float]:
    repo = repo or _Repo()
    _LLM.calls, _LLM.delay_s = 0, llm_delay
    with (
        patch.multiple(
            settings,
            code_fusion_signals=False,
            code_search_card_leg=False,
            code_search_concept_expand_model="",
            code_search_hyde_model="gpt-4o",
            code_search_hyde_kinds="responsibility",
            code_search_hyde_cap=1.0,
            code_search_vector_model_default="",
            code_search_llm_leg_grace_ms=grace_ms,
            code_search_deadline_ms=deadline_setting,
        ),
        patch("server.llm.client.get_llm_client_for_model", lambda m: _LLM()),
    ):
        t0 = time.perf_counter()
        res = asyncio.run(
            cs.search(
                repo,
                "o",
                "n",
                query=kw.pop("query", _QUESTION),
                top_k=5,
                embedder=_Embedder(),
                **kw,
            )
        )
    return repo, res, time.perf_counter() - t0


# ---- llm_legs -------------------------------------------------------------------------------


def test_default_still_runs_hyde_and_waits_for_it() -> None:
    _, res, _ = _run(llm_delay=0.05)
    assert _LLM.calls == 1 and res.retrieval_work["hyde_queries"] == 1
    assert any(u.via == "hyde" for u in res.units)
    assert res.legs_timed_out == [] and res.epistemic == ""


def test_llm_legs_none_skips_hyde_and_keeps_the_plain_legs() -> None:
    repo, res, _ = _run(llm_legs="none")
    assert _LLM.calls == 0 and "hyde_queries" not in res.retrieval_work
    assert len(repo.vector_calls) == 1  # the question's vector only, no passage scan
    assert {u.unit_id for u in res.units} >= {"L1", "V1"}
    assert res.legs_timed_out == []


def test_llm_legs_none_by_deployment_default() -> None:
    with patch.object(settings, "code_search_llm_legs_default", "none"):
        _, res, _ = _run()
    assert _LLM.calls == 0 and "hyde_queries" not in res.retrieval_work


def test_llm_legs_none_drops_a_model_named_concept_source_but_keeps_cards() -> None:
    work: dict[str, Any] = {}
    with patch.multiple(
        settings,
        code_search_concept_expand_model="gpt-4o-mini",
        code_search_concept_expand_kinds="any",
        code_search_card_leg=False,
    ):
        _, _, model, _ = _voting_leg_config(
            _QUESTION, prose=True, run_lexical=True, work=work, llm_legs="none"
        )
        assert model == ""
        _, _, model, _ = _voting_leg_config(
            _QUESTION, prose=True, run_lexical=True, work=work, llm_legs="auto"
        )
        assert model == "gpt-4o-mini"
    with patch.multiple(
        settings,
        code_search_concept_expand_model="cards:5",
        code_search_concept_expand_kinds="any",
        code_search_card_leg=False,
    ):
        _, _, model, _ = _voting_leg_config(
            _QUESTION, prose=True, run_lexical=True, work=work, llm_legs="none"
        )
        assert model == "cards:5"  # no model call behind it


# ---- deadline / grace -----------------------------------------------------------------------


def test_deadline_drops_a_slow_hyde_and_reports_it() -> None:
    # HyDE started (its start bar lowered), then dropped at the legs' share
    with patch.object(cs_gather, "MODEL_LEG_MIN_MS", 0):
        _, res, wall = _run(llm_delay=2.0, deadline_ms=400)
    assert wall < 1.0
    assert res.legs_timed_out == ["hyde"] and res.epistemic == "partial"
    assert res.retrieval_work["legs_timed_out"] == 1
    assert {u.unit_id for u in res.units} >= {"L1", "V1"}
    assert all(u.via != "hyde" for u in res.units)
    assert "ranked without hyde (past the deadline)" in res.calibration_explanation
    assert res.partial and res.partial_reason == "deadline 400 ms: legs not finished: hyde"


def test_a_short_budget_does_not_start_hyde() -> None:
    _, res, wall = _run(llm_delay=2.0, deadline_ms=1000)
    assert wall < 1.0 and _LLM.calls == 0 and "hyde_queries" not in res.retrieval_work
    assert res.legs_timed_out == [] and {u.unit_id for u in res.units} >= {"L1", "V1"}
    assert res.partial and res.partial_reason == "deadline 1000 ms: hyde skipped"


def test_deadline_from_the_deployment_setting_and_zero_turns_it_off() -> None:
    _, res, _ = _run(llm_delay=2.0, deadline_setting=150)
    assert _LLM.calls == 0 and res.partial and "hyde skipped" in res.partial_reason
    _, res, _ = _run(llm_delay=0.3, deadline_setting=100, deadline_ms=0)
    assert res.legs_timed_out == [] and res.retrieval_work["hyde_queries"] == 1
    assert not res.partial and res.partial_reason == ""


def test_a_leg_inside_the_deadline_is_kept() -> None:
    _, res, _ = _run(llm_delay=0.01, deadline_ms=5000)
    assert res.legs_timed_out == [] and any(u.via == "hyde" for u in res.units)
    assert not res.partial


def test_no_deadline_is_served_as_before() -> None:
    _, res, _ = _run(llm_delay=0.05)
    assert res.budget is None and not res.partial and res.partial_reason == ""
    assert res.legs_timed_out == [] and "leg_phase_ms" not in res.retrieval_work
    assert any(u.via == "hyde" for u in res.units)


def test_grace_bounds_hyde_past_the_plain_legs() -> None:
    _, res, wall = _run(llm_delay=2.0, grace_ms=100)
    assert wall < 1.0 and res.legs_timed_out == ["hyde"] and res.epistemic == "partial"


def test_the_deadline_waits_for_the_first_content_leg() -> None:
    # both content legs are slower than the legs' share (250 of 400 ms): the first to
    # answer inside the budget is waited for, the other is dropped
    repo = _Repo(lexical_s=0.3, vector_s=1.5)
    _, res, wall = _run(repo, llm_legs="none", deadline_ms=400)
    assert 0.25 < wall < 0.8
    assert "vector" in res.legs_timed_out and "lexical" not in res.legs_timed_out
    assert [u.unit_id for u in res.units] == ["L1"]
    assert res.epistemic == "partial" and res.partial


def test_no_content_leg_inside_the_budget_is_an_empty_partial_page_not_an_error() -> None:
    repo = _Repo(lexical_s=2.0, vector_s=2.0)
    _, res, wall = _run(repo, llm_legs="none", deadline_ms=300)
    assert wall < 0.8
    assert res.units == [] and res.abstain
    assert {"lexical", "vector"} <= set(res.legs_timed_out)
    assert res.partial and res.partial_reason.startswith("deadline 300 ms: legs not finished")


def test_leg_window_leaves_the_post_leg_reserve() -> None:
    from server.code.domain import deadline as code_deadline

    assert legs.leg_window(None) == (None, None)
    b = code_deadline.Budget(ms=4000)
    leg_ms, floor_ms = legs.leg_window(b)
    assert leg_ms is not None and floor_ms is not None
    assert 2900 <= leg_ms <= 3000 and 990 <= floor_ms <= 1010  # a quarter held back
    small = code_deadline.Budget(ms=200)
    leg_ms, floor_ms = legs.leg_window(small)
    assert leg_ms is not None and floor_ms is not None
    assert 1 <= leg_ms <= 50 and 140 <= floor_ms <= 150


def test_rerank_is_skipped_on_a_short_budget_and_cut_at_what_is_left(monkeypatch) -> None:
    from server.code.domain import deadline as code_deadline
    from server.code.domain.units import CodeUnit

    calls: list[int] = []

    async def slow_rerank(endpoint, query, units, **kw):
        calls.append(1)
        await asyncio.sleep(5)
        return list(reversed(units)), True

    monkeypatch.setattr(cs_rerank, "_rerank_units", slow_rerank)
    units = [
        CodeUnit(unit_id=f"u{i}", path=f"src/{i}.cpp", kind="function", start_line=1, end_line=2)
        for i in range(3)
    ]

    def stage(ms: int | None) -> tuple[list[Any], Any]:
        res = cs.CodeSearchResult(budget=code_deadline.start(ms))
        out = asyncio.run(_rerank_stage("builtin:openai", "q", list(units), {}, res, prose=True))
        return out, res

    out, res = stage(300)  # 200 ms after the page reserve: not started
    assert calls == [] and out == units and res.partial_reason == "deadline 300 ms: rerank skipped"
    t0 = time.perf_counter()
    out, res = stage(800)  # started, cut at what is left, the fused order kept
    assert time.perf_counter() - t0 < 1.2
    assert calls == [1] and out == units
    assert res.partial_reason == "deadline 800 ms: rerank cut at the deadline"


def test_run_legs_without_bounds_is_a_plain_gather() -> None:
    class R:
        legs_timed_out: list[str] = []
        epistemic = ""

    async def v(x: int, s: float = 0.0) -> int:
        await asyncio.sleep(s)
        return x

    r, work = R(), {}
    out = asyncio.run(legs.run_legs({"a": (v(1), 0), "b": (v(2, 0.01), 0)}, result=r, work=work))
    assert out == [1, 2] and r.legs_timed_out == [] and "leg_phase_ms" not in work


def test_run_legs_cancels_the_dropped_leg() -> None:
    cancelled: list[str] = []

    async def slow() -> int:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append("slow")
            raise
        return 1

    async def fast() -> int:
        return 2

    class R:
        legs_timed_out: list[str] = []
        epistemic = ""

    r, work = R(), {}
    out = asyncio.run(
        legs.run_legs(
            {"lexical": (fast(), []), "history": (slow(), [])}, result=r, work=work, deadline_ms=30
        )
    )
    assert out == [2, []] and cancelled == ["slow"]
    assert r.legs_timed_out == ["history"] and work["legs_timed_out"] == 1


# ---- the embedding: once per query, cached across queries ------------------------------------


def test_one_provider_embed_across_vector_prefix_twin_and_cards_and_a_cache_hit(
    monkeypatch,
) -> None:
    from server.embedding import _cache
    from server.embedding.prefix import PrefixEmbedder

    fetches: list[str] = []

    async def fetch(text: str) -> list[float]:
        fetches.append(text)
        return [0.5, 0.5, 0.5, 0.5]

    class CachedEmbedder(_Embedder):
        async def embed(self, text: str) -> list[float]:
            vec, _ = await _cache.cached_embed(self.model_name, text, fetch, dim=4)
            return vec

    served = CachedEmbedder()
    twin = PrefixEmbedder(base=served, spec="prefix:2:text-embedding-3-large")
    monkeypatch.setattr(settings, "embedding_cache_shared", False)
    monkeypatch.setattr(_cache._cache, "_store", type(_cache._cache._store)())
    monkeypatch.setattr("server.embedding.embedder.get_secondary_embedder", lambda spec: twin)

    def one_search(repo: _Repo) -> Any:
        with patch.multiple(
            settings,
            code_fusion_signals=False,
            code_search_card_leg=True,
            code_search_card_leg_kinds="any",
            code_search_card_leg_mode="skeleton",
            code_search_concept_expand_model="",
            code_search_hyde_model="",
            code_search_vector_model_default="prefix:2:text-embedding-3-large",
            code_search_vector_shortlist=10,
        ):
            return asyncio.run(cs.search(repo, "o", "n", query=_QUESTION, top_k=5, embedder=served))

    repo = _Repo()
    res = one_search(repo)
    assert len(fetches) == 1 and res.retrieval_work["embedding_calls"] == 1
    # the twin's shortlist read the served vector's 2-d prefix, renormalised
    assert repo.shortlist_calls and len(repo.shortlist_calls[0]) == 2
    assert res.retrieval_work["vector_shortlist"] == 1 and res.retrieval_work.get("card_queries")
    one_search(_Repo())
    assert len(fetches) == 1  # the repeated query came from the process cache


# ---- the wire and the memo ------------------------------------------------------------------


def test_legs_timed_out_and_epistemic_are_on_the_wire_only_when_fired() -> None:
    from server.code.api.models_search import CodeSearchOutput

    quiet = CodeSearchOutput(results=[], route="hybrid").model_dump()
    assert "legs_timed_out" not in quiet and "epistemic" not in quiet
    loud = CodeSearchOutput(
        results=[], route="hybrid", legs_timed_out=["hyde"], epistemic="partial"
    ).model_dump()
    assert loud["legs_timed_out"] == ["hyde"] and loud["epistemic"] == "partial"


def test_llm_legs_none_is_its_own_memo_key_and_auto_keeps_the_old_one() -> None:
    from server.code.api.models_search import CodeSearchInput
    from server.code.app import search_memo as memo

    def key(**kw: Any) -> str:
        return memo.key_for(
            CodeSearchInput(query="q", **kw),
            embedding_model="m",
            rerank="",
            token_budget=3000,
            recent_dirs=[],
        )

    assert key() == key(llm_legs="auto") == key(deadline_ms=500)
    assert key(llm_legs="none") != key()


def test_input_bounds() -> None:
    from pydantic import ValidationError

    from server.code.api.models_search import CodeSearchInput

    with pytest.raises(ValidationError):
        CodeSearchInput(query="q", deadline_ms=-1)
    with pytest.raises(ValidationError):
        CodeSearchInput(query="q", deadline_ms=60001)
    with pytest.raises(ValidationError):
        CodeSearchInput(query="q", llm_legs="full")  # type: ignore[arg-type]


# ---- budgets per shape ----------------------------------------------------------------------


def test_code_search_prices_each_shape() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["code.search"]
    assert set(op.shape_budgets_p99_ms) == {"hybrid", "hybrid_hyde"}
    # the base prices the exact symbol route: the cheapest shape
    assert (
        op.budget_p99_ms
        < op.shape_budgets_p99_ms["hybrid"]
        < op.shape_budgets_p99_ms["hybrid_hyde"]
    )


def test_each_request_notes_the_shape_it_served() -> None:
    from server.observability.budget_shape import budget_shape_var, current_budget_shape

    async def go(**kw: Any) -> str:
        token = budget_shape_var.set("")
        try:
            with (
                patch.multiple(
                    settings,
                    code_fusion_signals=False,
                    code_search_card_leg=False,
                    code_search_concept_expand_model="",
                    code_search_hyde_model="gpt-4o",
                    code_search_hyde_kinds="responsibility",
                    code_search_vector_model_default="",
                ),
                patch("server.llm.client.get_llm_client_for_model", lambda m: _LLM()),
            ):
                await cs.search(
                    _Repo(),
                    "o",
                    "n",
                    query=kw.pop("query", _QUESTION),
                    top_k=5,
                    embedder=_Embedder(),
                    **kw,
                )
            return current_budget_shape()
        finally:
            budget_shape_var.reset(token)

    _LLM.delay_s = 0.0
    assert asyncio.run(go()) == "hybrid_hyde"
    assert asyncio.run(go(llm_legs="none")) == "hybrid"
    _LLM.delay_s = 2.0
    assert asyncio.run(go(deadline_ms=100)) == "hybrid"
    _LLM.delay_s = 0.0
    # the exact symbol route never reaches the legs: the base budget prices it
    assert asyncio.run(go(query="MemoryTracker::allocImpl")) == ""


# ---- the handler: the budget from arrival, partial on the wire -------------------------------


class _HandlerRepo:
    def __init__(self, vector_s: float) -> None:
        self.vector_s = vector_s

    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        return [{"chunk_id": "L1", "path": "src/a.cpp", "lex_score": 4.0, **_UNIT}]

    async def vector_search_chunks(self, *a, **k):
        await asyncio.sleep(self.vector_s)
        return [{"chunk_id": "V1", "path": "src/b.cpp", "distance": 0.3, **_UNIT}]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}


def _handle(monkeypatch, repo: _HandlerRepo, **kw: Any) -> tuple[dict[str, Any], float]:
    import server.operations  # noqa: F401 — handlers import wire models lazily
    from server.code.api.models_search import CodeSearchInput
    from server.code.app.search import handler as h
    from server.embedding import embedder as emb

    for name, value in {
        "code_intel_enabled": True,
        "code_org": "o",
        "code_namespace": "n",
        "code_trace_log_enabled": False,
        "code_risk_annotations": False,
        "code_search_cursor_enabled": False,
        "code_fusion_signals": False,
        "code_search_hyde_model": "",
        "code_search_card_leg": False,
        "code_search_concept_expand_model": "",
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "code_search_memo_enabled", False, raising=False)
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)

    async def _as_of(repo_, org, ns):
        return {"commit": "c" * 40}

    async def _thresholds(org, ns):
        return 0.30, 0.22

    monkeypatch.setattr(h, "index_as_of", _as_of)
    monkeypatch.setattr(h, "resolve_code_thresholds", _thresholds)
    monkeypatch.setattr(emb, "get_embedder", lambda: _Embedder())
    t0 = time.perf_counter()
    out = asyncio.run(h.handle_code_search(CodeSearchInput(query=_QUESTION, top_k=5, **kw)))
    return out.model_dump(mode="json"), time.perf_counter() - t0


def test_the_handler_serves_the_finished_legs_as_a_partial_page(monkeypatch) -> None:
    data, wall = _handle(monkeypatch, _HandlerRepo(vector_s=3.0), deadline_ms=500)
    assert wall < 1.0
    assert [u["unit_id"] for u in data["results"]] == ["L1"]
    assert data["partial"] is True and data["legs_timed_out"] == ["vector"]
    assert data["partial_reason"] == "deadline 500 ms: legs not finished: vector"


def test_the_handler_without_a_deadline_waits_and_carries_no_partial_field(monkeypatch) -> None:
    data, _ = _handle(monkeypatch, _HandlerRepo(vector_s=0.05))
    assert {u["unit_id"] for u in data["results"]} == {"L1", "V1"}
    assert not {"partial", "partial_reason", "legs_timed_out", "epistemic"} & set(data)


class _FailingVectorRepo(_HandlerRepo):
    async def vector_search_chunks(self, *a, **k):
        raise RuntimeError("vector index unavailable")


def test_a_failed_leg_is_partial_on_the_wire_and_never_memoized(monkeypatch) -> None:
    """A leg that raised answered nothing: the page says so and is not replayed as a
    complete answer for the memo's TTL."""
    from server.code.app import search_memo as memo

    puts: list[Any] = []

    async def _no_hit(*a: Any, **k: Any) -> None:
        return None

    async def _put(*a: Any, **k: Any) -> None:
        puts.append(a)

    monkeypatch.setattr(memo, "enabled", lambda _input: True)
    monkeypatch.setattr(memo, "get", _no_hit)
    monkeypatch.setattr(memo, "put", _put)
    data, _ = _handle(monkeypatch, _FailingVectorRepo(vector_s=0.0))
    assert [u["unit_id"] for u in data["results"]] == ["L1"]
    assert data["epistemic"] == "partial"
    assert data["retrieval_work"]["vector_leg_failed"] == 1
    assert puts == []
