"""Every code.search answer says where its time went.

The stage wall is a per-request accumulator the existing code_span()
context managers report into: each span charges its exclusive wall time to
a stage name, spans in gathered leg tasks land on the same wall, and the
op reads it twice at the end — `retrieval_work.stage_ms_<stage>` integers
on the wire and the `stage_ms` Map on the trace row. The payload is
byte-identical to one without the wall apart from those keys; a memo replay
reports its own probe, not the memoised computation's wall; a fan-out takes
the slowest namespace per stage; a table without the `stage_ms` column drops
that column group alone.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from server.code.api.models_search import CodeSearchInput
from server.code.app import common as cc
from server.code.app import spans
from server.code.app.search import handler as h
from server.settings import settings

# ---- the accumulator -------------------------------------------------------


class _Clock:
    """perf_counter for the span timer: time moves only when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def perf_counter(self) -> float:
        return self.now

    def advance(self, ms: float) -> None:
        self.now += ms / 1000.0


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    from server.code.app import spans as code_spans

    fake = _Clock()
    monkeypatch.setattr(code_spans, "time", fake)
    return fake


def test_code_span_charges_exclusive_time_to_the_active_wall(clock: _Clock) -> None:
    wall = spans.start_stage_wall()
    with spans.code_span("search.vector_leg"):
        clock.advance(20)
        with spans.code_span("search.embed"):
            clock.advance(30)
    with spans.code_span("search.fuse"):
        pass
    with spans.code_span("search.bodies"):
        clock.advance(5)
    with spans.code_span("search.expand_top_to_file"):  # an alias of `bodies`: adds up
        clock.advance(5)
    ms = wall.ms
    assert set(ms) == {"vector", "embed", "fusion", "bodies"}, ms
    assert round(ms["embed"]) == 30
    # the parent is charged its OWN time, not the embed call inside it
    assert round(ms["vector"]) == 20, ms
    assert round(ms["bodies"]) == 10
    assert round(ms["fusion"]) == 0
    col = wall.column()
    assert all(isinstance(v, int) for v in col.values())
    assert list(col) == sorted(col), "the Map column has a fixed stage order"
    assert spans.current_stage_ms() == col
    work = spans.with_stage_ms({"lexical_queries": 1})
    assert work["lexical_queries"] == 1
    assert {k for k in work if k.startswith("stage_ms_")} == {
        "stage_ms_vector",
        "stage_ms_embed",
        "stage_ms_fusion",
        "stage_ms_bodies",
    }


def test_stage_of_maps_span_suffixes_to_the_stage_vocabulary() -> None:
    assert spans.stage_of("search.lexical_leg") == "lexical"
    assert spans.stage_of("search.vector_leg") == "vector"
    assert spans.stage_of("search.fuse") == "fusion"
    assert (
        spans.stage_of("search.history_leg") == spans.stage_of("search.history_route") == "history"
    )
    assert spans.stage_of("search.rerank") == "rerank"
    assert spans.stage_of("search.risk_annotate") == "risk_annotate"
    assert spans.stage_of("search.memo_probe") == "memo_probe"
    assert spans.stage_of("symbol.resolve") == "resolve"
    assert spans.stage_of("symbol.budget") == "budget"
    assert spans.stage_of("grep.chunks") == "chunks"


def test_without_a_wall_a_span_still_yields_and_records_nothing() -> None:
    spans._STAGE_WALL.set(None)
    with spans.code_span("search.lexical_leg") as sp:
        assert sp is None or not sp.is_recording()
    assert spans.current_stage_ms() == {}
    assert spans.with_stage_ms({"a": 1}) == {"a": 1}


def test_spans_in_gathered_leg_tasks_report_into_one_wall() -> None:
    async def _run() -> dict[str, float]:
        wall = spans.start_stage_wall()

        async def leg(name: str, s: float) -> None:
            with spans.code_span(name):
                await asyncio.sleep(s)

        await asyncio.gather(leg("search.lexical_leg", 0.02), leg("search.vector_leg", 0.03))
        return dict(wall.ms)

    ms = asyncio.run(_run())
    assert set(ms) == {"lexical", "vector"}
    assert ms["lexical"] >= 18 and ms["vector"] >= 28
    # concurrent stages: each is its own wall, they need not sum to the total


def test_an_exception_inside_a_span_is_still_charged_and_re_raised(clock: _Clock) -> None:
    wall = spans.start_stage_wall()
    with pytest.raises(RuntimeError), spans.code_span("search.lexical_leg"):
        clock.advance(5)
        raise RuntimeError("leg failed")
    assert round(wall.ms["lexical"]) == 5
    assert spans._STAGE_FRAMES.get() == (), "the frame stack unwinds on error"


# ---- the handler's payload ---------------------------------------------------

_UNIT = dict(
    scope="f",
    kind="function",
    start_line=1,
    end_line=5,
    content="x",
    symbol_ids=[],
    context_description="",
)


def _lex(cid: str, score: float) -> dict[str, Any]:
    return {"chunk_id": cid, "lex_score": score, "path": f"src/{cid}.cpp", **_UNIT}


def _vec(cid: str, distance: float) -> dict[str, Any]:
    return {"chunk_id": cid, "distance": distance, "path": f"src/{cid}.cpp", **_UNIT}


class _Repo:
    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        return [_lex("a", 40.0), _lex("b", 10.0)]

    async def vector_search_chunks(self, *a, **k):
        return [_vec("c", 0.1), _vec("a", 0.2)]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}

    async def all_state(self, org, ns):
        return {}


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


@pytest.fixture
def handler_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")
    monkeypatch.setattr(settings, "code_trace_log_enabled", False)
    monkeypatch.setattr(settings, "code_risk_annotations", False)
    monkeypatch.setattr(settings, "code_search_cursor_enabled", False)
    monkeypatch.setattr(settings, "code_search_memo_enabled", False, raising=False)
    monkeypatch.setattr(settings, "code_fusion_signals", False)
    monkeypatch.setattr(settings, "code_search_pr_leg", "off", raising=False)
    monkeypatch.setattr(h, "CodeRepo", lambda: _Repo())

    async def _as_of(repo_, org, ns):
        return {"commit": "c" * 40}

    async def _thresholds(org, ns):
        return 0.30, 0.22

    monkeypatch.setattr(h, "index_as_of", _as_of)
    monkeypatch.setattr(h, "resolve_code_thresholds", _thresholds)
    from server.embedding import embedder as emb

    monkeypatch.setattr(emb, "get_embedder", lambda: _Embedder())
    yield


def _run(inp: CodeSearchInput) -> dict[str, Any]:
    out = asyncio.run(h.handle_code_search(inp))
    d = out.model_dump(mode="json")
    d.pop("trace_run_id", None)
    return d


def test_search_payload_is_unchanged_except_the_stage_keys(
    handler_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the corpus-statistics cache would serve the second search's statistics
    # from the process; this pin compares two computed payloads, so bypass it
    monkeypatch.setattr(settings, "code_corpus_stats_ttl_s", 0)
    inp = CodeSearchInput(query="where do we retry uploads", top_k=3)
    with_wall = _run(inp)
    stage_keys = {k for k in with_wall["retrieval_work"] if k.startswith("stage_ms_")}
    assert {
        "stage_ms_lexical",
        "stage_ms_vector",
        "stage_ms_embed",
        "stage_ms_fusion",
    } <= stage_keys
    assert all(isinstance(with_wall["retrieval_work"][k], int) for k in stage_keys)
    assert "stage_ms" not in with_wall["score_explanation"]

    # the same request with the wall never started: the payload without the wall
    monkeypatch.setattr(h, "start_stage_wall", lambda: spans._STAGE_WALL.set(None))
    without = _run(inp)
    assert not any(k.startswith("stage_ms_") for k in without["retrieval_work"])
    stripped = dict(with_wall)
    stripped["retrieval_work"] = {
        k: v for k, v in with_wall["retrieval_work"].items() if k not in stage_keys
    }
    assert stripped == without


# ---- the trace row -------------------------------------------------------------


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch):
    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    class _Inner:
        async def insert(self, table, data, column_names=None, settings=None):
            if errors:
                raise RuntimeError(errors.pop(0))
            rows.append(dict(zip(column_names, data[0], strict=True)))

    class _Client:
        client = _Inner()

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(settings, "code_trace_log_enabled", True, raising=False)
    yield rows, errors


def _schedule(stage_ms: dict[str, int] | None) -> None:
    cc.schedule_code_trace(
        org="o",
        ns="n",
        op="code.search",
        query="q",
        route="hybrid",
        returned_ids=["u1"],
        returned_paths=["src/a.cpp"],
        returned_scores=[0.9],
        top_k=5,
        tokens_returned=100,
        abstain=False,
        retrieval_risk="low",
        embedding_model="m",
        started_monotonic=time.monotonic(),
        trace_run_id="run-1",
        stage_ms=stage_ms,
    )


@pytest.mark.asyncio
async def test_trace_row_carries_the_stage_map_as_whole_milliseconds(capture) -> None:
    rows, _ = capture
    _schedule({"lexical": 120, "vector": 340, "embed": 31, "fusion": 0})
    await asyncio.gather(*list(cc._TRACE_TASKS))
    (row,) = rows
    assert row["stage_ms"] == {"lexical": 120, "vector": 340, "embed": 31, "fusion": 0}
    rows.clear()
    _schedule(None)
    await asyncio.gather(*list(cc._TRACE_TASKS))
    assert rows[0]["stage_ms"] == {}


# ---- replay and fan-out ----------------------------------------------------------


def test_memo_replay_drops_the_memoised_computations_wall() -> None:
    from server.code.api.models_search import CodeSearchOutput
    from server.code.app import search_memo as memo

    stored = CodeSearchOutput(
        results=[],
        route="hybrid",
        retrieval_work={"lexical_rows": 200, "stage_ms_vector": 410},
    )
    got = memo.replay(
        {"payload": stored.model_dump_json(), "indexed_commit": b"", "created_at": None},
        CodeSearchOutput,
        as_of={},
        trace_run_id="r",
    )
    assert got.retrieval_work == {"lexical_rows": 200, "memo_hit": 1}


def test_fanout_merge_takes_the_slowest_namespace_per_stage_and_sums_counters() -> None:
    from types import SimpleNamespace

    from server.code.app.search_fanout import _merge_work

    a = SimpleNamespace(retrieval_work={"lexical_queries": 1, "stage_ms_vector": 400})
    b = SimpleNamespace(retrieval_work={"lexical_queries": 1, "stage_ms_vector": 250})
    work = _merge_work({"n1": (a, 1), "n2": (b, 2)}, ["n1", "n2"], [])
    assert work["lexical_queries"] == 2 and work["stage_ms_vector"] == 400
