"""Every served unit says which leg found it.

`legs` (per-request A/B) runs one leg or both and skips the embedding call
when the vector leg is off; each served unit carries `via` and its per-leg
ranks (dropped from the wire when empty); calibration is legs-aware, so a
single-leg run is not scored as "the legs disagreed"; `track_path` reports
where one file's chunks stood in this call's pools; the trace row carries
the aligned provenance arrays and survives a table that lacks those columns.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from server.code.api.models_search import CodeSearchInput
from server.code.app import common as cc
from server.code.app import search as cs
from server.code.app.search.calibrate import calibration_routes
from server.code.app.search.fusion import leg_provenance
from server.code.app.search.state import _POOL
from server.code.app.search.track import track_verdict
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
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


def _lex(cid: str, score: float) -> dict[str, Any]:
    return {"chunk_id": cid, "lex_score": score, "path": f"src/{cid}.cpp", **_UNIT}


def _vec(cid: str, distance: float) -> dict[str, Any]:
    return {"chunk_id": cid, "distance": distance, "path": f"src/{cid}.cpp", **_UNIT}


class _Repo:
    """Lexical pool [a, b]; vector pool [c, a]; fuzzy hit configurable; the
    track read answers from `track_rows`."""

    def __init__(self, *, fuzzy: bool = False, track_rows: list[dict[str, Any]] | None = None):
        self.fuzzy = fuzzy
        self.track_rows = track_rows or []
        self.calls: list[str] = []
        self.track_args: dict[str, Any] = {}

    async def code_corpus_stats(self, org, ns, tokens):
        self.calls.append("stats")
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        self.calls.append("lexical")
        return [_lex("a", 40.0), _lex("b", 10.0)]

    async def vector_search_chunks(self, *a, **k):
        self.calls.append("vector")
        return [_vec("c", 0.1), _vec("a", 0.2)]

    async def symbols_fuzzy(self, *a, **k):
        self.calls.append("fuzzy")
        return [{"symbol_id": "s"}] if self.fuzzy else []

    async def symbols_by_name(self, org, ns, name, limit=10):
        self.calls.append("symbols")
        return [
            {
                "symbol_id": "sym1",
                "path": "src/q.cpp",
                "qualified_name": name,
                "start_line": 1,
                "end_line": 3,
                "signature": "void f()",
                "tier": "scip",
            }
        ]

    async def unit_signals(self, *a, **k):
        return {}

    async def paths_under_prefix(self, org, ns, model, prefix, limit=9):
        self.calls.append("paths")
        if prefix == "src/dir":
            return [f"src/dir/{i}.cpp" for i in range(limit)]
        if prefix == "src/pre":
            return ["src/pre.h", "src/pre/impl.cpp", "src/prefix_other.cpp"]
        return [prefix] if self.track_rows else []

    async def track_path_chunks(self, org, ns, model, paths, tokens, idfs, qvec, **k):
        self.calls.append("track")
        self.track_args = {"paths": paths, "tokens": tokens, "idfs": idfs, "qvec": qvec}
        return self.track_rows


class _Embedder:
    model_name = "fake-embed"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [0.1, 0.2, 0.3]


def _search(repo: _Repo, embedder: _Embedder | None, **kw: Any) -> cs.CodeSearchResult:
    kw.setdefault("query", "where do we retry uploads")
    kw.setdefault("top_k", 3)
    with patch.object(settings, "code_fusion_signals", False):
        return asyncio.run(cs.search(repo, "o", "n", embedder=embedder, **kw))


# ---- the legs switch -------------------------------------------------------


def test_legs_lexical_skips_the_vector_leg_and_the_embedding_call() -> None:
    repo, emb = _Repo(), _Embedder()
    res = _search(repo, emb, legs="lexical")
    assert emb.calls == 0 and "vector" not in repo.calls
    assert res.route == "lexical_only" and res.legs == "lexical"
    assert res.retrieval_work["embedding_calls"] == 0
    assert [u.unit_id for u in res.units] == ["a", "b"]
    assert all(u.via == "lexical" for u in res.units)
    assert [u.lexical_rank for u in res.units] == [1, 2]
    assert all(u.vector_rank == 0 for u in res.units)
    assert res.top_similarity == 0.0, "no cosine was measured, none is reported"


def test_legs_lexical_with_expansion_terms_reads_lexical_expand() -> None:
    res = _search(_Repo(), _Embedder(), legs="lexical", extra_terms=["retryUpload"])
    assert res.route == "lexical_expand"


def test_legs_vector_skips_the_corpus_stats_and_the_lexical_scan() -> None:
    repo, emb = _Repo(), _Embedder()
    res = _search(repo, emb, legs="vector")
    assert emb.calls == 1
    assert "stats" not in repo.calls and "lexical" not in repo.calls
    assert res.route == "vector_only" and res.legs == "vector"
    assert [u.unit_id for u in res.units] == ["c", "a"]
    assert all(u.via == "vector" for u in res.units)
    assert [u.vector_rank for u in res.units] == [1, 2]
    assert all(u.lexical_rank == 0 for u in res.units)


def test_hybrid_is_the_default_and_an_unknown_legs_value_falls_back_to_it() -> None:
    a = _search(_Repo(), _Embedder())
    b = _search(_Repo(), _Embedder(), legs="both-please")
    assert a.legs == b.legs == "hybrid" and a.route == b.route == "hybrid"
    assert [u.unit_id for u in a.units] == [u.unit_id for u in b.units]
    assert "legs=hybrid" in a.score_explanation


def test_legs_is_accepted_on_the_wire_only_as_the_three_names() -> None:
    assert CodeSearchInput(query="q", legs="lexical").legs == "lexical"
    assert CodeSearchInput(query="q").legs is None
    with pytest.raises(ValidationError):
        CodeSearchInput(query="q", legs="grep")


# ---- provenance ------------------------------------------------------------


def test_hybrid_units_carry_via_and_both_leg_ranks_from_before_fusion() -> None:
    res = _search(_Repo(), _Embedder())
    by_id = {u.unit_id: u for u in res.units}
    assert (by_id["a"].via, by_id["a"].lexical_rank, by_id["a"].vector_rank) == ("both", 1, 2)
    assert (by_id["b"].via, by_id["b"].lexical_rank, by_id["b"].vector_rank) == ("lexical", 2, 0)
    assert (by_id["c"].via, by_id["c"].lexical_rank, by_id["c"].vector_rank) == ("vector", 0, 1)


def test_leg_provenance_keeps_the_first_occurrence_rank() -> None:
    prov = leg_provenance(
        [_lex("a", 3.0), _lex("a", 2.0), _lex("b", 1.0)], [_vec("b", 0.1), _vec("z", 0.3)]
    )
    assert prov == {"a": ("lexical", 1, 0), "b": ("both", 3, 1), "z": ("vector", 0, 2)}
    assert leg_provenance([], []) == {}


def test_symbol_route_units_say_symbol() -> None:
    res = _search(_Repo(), _Embedder(), query="parseQuotaKey")
    assert res.route == "symbol_exact"
    assert res.units and all(u.via == "symbol" for u in res.units)


def test_wire_drops_empty_provenance_and_keeps_a_set_one() -> None:
    bare = CodeUnit(path="src/a.cpp").model_dump()
    assert not {"via", "lexical_rank", "vector_rank"} & bare.keys()
    full = CodeUnit(path="src/a.cpp", via="both", lexical_rank=3, vector_rank=1).model_dump()
    assert (full["via"], full["lexical_rank"], full["vector_rank"]) == ("both", 3, 1)
    # a vector-only unit has no lexical rank: only the rank it has is serialized
    vec_only = CodeUnit(path="src/a.cpp", via="vector", vector_rank=7).model_dump()
    assert "lexical_rank" not in vec_only and vec_only["vector_rank"] == 7


# ---- legs-aware calibration --------------------------------------------------


def test_calibration_routes_count_leg_agreement_only_when_both_legs_ran() -> None:
    hybrid_agree = calibration_routes(legs_agree=True, both_legs=True, fuzzy_hit=False)
    assert hybrid_agree == {"extra_route_votes": 1, "extra_routes_possible": 2}
    hybrid_disagree = calibration_routes(legs_agree=False, both_legs=True, fuzzy_hit=True)
    assert hybrid_disagree == {"extra_route_votes": 1, "extra_routes_possible": 2}
    single = calibration_routes(legs_agree=False, both_legs=False, fuzzy_hit=True)
    assert single == {"extra_route_votes": 1, "extra_routes_possible": 1}
    # a stale True cannot leak in when one leg did not run
    assert calibration_routes(legs_agree=True, both_legs=False, fuzzy_hit=False) == {
        "extra_route_votes": 0,
        "extra_routes_possible": 1,
    }


def test_single_leg_route_agreement_is_not_penalised_for_the_missing_leg() -> None:
    # fuzzy hit on every arm: the hybrid legs agree on `a`, so hybrid = 3/3;
    # a single leg has one extra route (fuzzy) and reaches 2/2 — the same 1.0.
    hybrid = _search(_Repo(fuzzy=True), _Embedder())
    lexical = _search(_Repo(fuzzy=True), _Embedder(), legs="lexical")
    vector = _search(_Repo(fuzzy=True), _Embedder(), legs="vector")
    assert hybrid.route_agreement == lexical.route_agreement == vector.route_agreement == 1.0
    # without the fuzzy vote a single leg is 1/2 and so is a disagreeing hybrid 2/3 > 1/2:
    # the single leg is scored on what ran, never below a hybrid whose legs disagreed
    lexical_no_fuzzy = _search(_Repo(), _Embedder(), legs="lexical")
    assert lexical_no_fuzzy.route_agreement == 0.5
    assert not lexical_no_fuzzy.abstain


def test_a_requested_lexical_only_run_does_not_abstain_on_the_similarity_stand_in() -> None:
    # per-namespace calibration can put the abstain threshold above
    # noise_floor + 0.1; a vector leg the request skipped must not abstain
    # the result on a cosine nobody measured
    res = _search(
        _Repo(fuzzy=True), _Embedder(), legs="lexical", abstain_threshold=0.45, noise_floor=0.22
    )
    assert not res.abstain and res.retrieval_risk != "abstain"
    # the degrade path (no embedder at all) is unchanged: the stand-in stays
    # at noise_floor + 0.1 and the old threshold rule applies
    degraded = _search(_Repo(fuzzy=True), None, abstain_threshold=0.45, noise_floor=0.22)
    assert degraded.abstain and degraded.abstain_code == "below_threshold"


# ---- track_path --------------------------------------------------------------


def _chunk(cid: str, lex: float, dist: float | None, start: int = 1) -> dict[str, Any]:
    return {
        "chunk_id": cid,
        "scope": f"S::{cid}",
        "kind": "function",
        "start_line": start,
        "end_line": start + 4,
        "embedding_dim": 3 if dist is not None else 0,
        "lex_score": lex,
        "distance": -1.0 if dist is None else dist,
    }


def test_track_path_uses_this_calls_tokens_idfs_and_query_vector() -> None:
    repo = _Repo(track_rows=[_chunk("a", 40.0, 0.2)])
    res = _search(repo, _Embedder(), track_path="src/a.cpp")
    assert repo.calls[-2:] == ["paths", "track"]
    assert repo.track_args["paths"] == ["src/a.cpp"]
    assert repo.track_args["tokens"] and len(repo.track_args["idfs"]) == len(
        repo.track_args["tokens"]
    )
    assert repo.track_args["qvec"] == [0.1, 0.2, 0.3]
    assert res.tracked["verdict"] == "served at rank 1"
    assert res.tracked["best"]["served_rank"] == 1
    assert (res.tracked["best"]["lexical_rank"], res.tracked["best"]["vector_rank"]) == (1, 2)


def test_track_path_in_pool_but_cut_by_top_k() -> None:
    repo = _Repo(track_rows=[_chunk("b", 10.0, 0.6)])
    res = _search(repo, _Embedder(), top_k=1, track_path="src/b.cpp")
    assert [u.unit_id for u in res.units] == ["a"]
    assert res.tracked["verdict"] == "rank 2 in lexical pool"
    assert res.tracked["best"]["served_rank"] == 0


def test_track_path_not_indexed_and_no_lexeme_match() -> None:
    repo = _Repo(track_rows=[])
    res = _search(repo, _Embedder(), track_path="src/missing.cpp")
    assert res.tracked == {
        "path": "src/missing.cpp",
        "verdict": "not indexed",
        "chunks": 0,
        "pool": _POOL,
        "files": [],
    }
    assert "track" not in repo.calls, "no file resolved, no chunk scan"
    res = _search(_Repo(track_rows=[_chunk("z", 0.0, 0.55)]), _Embedder(), track_path="src/z.cpp")
    assert res.tracked["verdict"] == "no lexeme match; distance 0.550 but not returned by the leg"
    assert res.tracked["best"]["distance"] == 0.55 and res.tracked["chunks"] == 1


def test_track_path_prefix_resolves_to_its_files_and_a_directory_is_refused() -> None:
    repo = _Repo(track_rows=[_chunk("p1", 0.0, 0.5)])
    res = _search(repo, _Embedder(), track_path="src/pre")
    # boundary rule: pre.h and pre/impl.cpp belong to the prefix, prefix_other.cpp does not
    assert repo.track_args["paths"] == ["src/pre.h", "src/pre/impl.cpp"]
    assert res.tracked["files"] == ["src/pre.h", "src/pre/impl.cpp"]
    assert res.tracked["path"] == "src/pre" and res.tracked["chunks"] == 1
    repo = _Repo(track_rows=[_chunk("p1", 0.0, 0.5)])
    res = _search(repo, _Embedder(), track_path="src/dir")
    assert "track" not in repo.calls
    assert res.tracked["verdict"].startswith("prefix matches more than 8 files")
    assert len(res.tracked["files"]) == 8


def test_track_path_vector_only_sends_no_tokens_and_reports_no_lexical_side() -> None:
    # legs="vector": no idfs were computed, so the lexical zip would be
    # unequal arrays (a DatabaseError) — the
    # track read gets no tokens and the verdict has no lexical clause
    repo = _Repo(track_rows=[_chunk("z", 0.0, 0.55)])
    res = _search(repo, _Embedder(), legs="vector", track_path="src/z.cpp")
    assert repo.track_args["tokens"] == [] and repo.track_args["idfs"] == []
    assert repo.track_args["qvec"] == [0.1, 0.2, 0.3]
    assert res.tracked["verdict"] == "distance 0.550 but not returned by the leg"


def test_track_path_lexical_only_reports_no_vector_side() -> None:
    repo = _Repo(track_rows=[_chunk("z", 0.0, None)])
    res = _search(repo, _Embedder(), legs="lexical", track_path="src/z.cpp")
    assert repo.track_args["qvec"] is None
    assert res.tracked["verdict"] == "no lexeme match"
    assert res.tracked["best"]["distance"] is None


def test_track_verdict_beyond_full_pools_reports_floor_and_ceiling() -> None:
    lexical = [_lex("a", 40.0), _lex("b", 10.0)]
    vector = [_vec("c", 0.1), _vec("a", 0.2)]
    out = track_verdict(
        "src/z.cpp",
        [_chunk("z1", 1.2, 0.5), _chunk("z2", 3.4, 0.7, start=20)],
        lexical,
        vector,
        served_ids=["a"],
        pool=2,
        vector_ran=True,
        lexical_ran=True,
    )
    assert out["verdict"] == (
        "beyond pool: lexical 3.40 under floor 10.00; beyond pool at distance 0.700 (ceiling 0.200)"
    )
    assert out["best"]["chunk_id"] == "z2", "the chunk that came closest on the lexical axis"
    assert out["lexical_floor"] == 10.0 and out["vector_ceiling"] == 0.2
    # a chunk without a vector under a vector run says so
    out = track_verdict(
        "src/z.cpp",
        [_chunk("z1", 0.0, None)],
        lexical,
        vector,
        served_ids=[],
        pool=2,
        vector_ran=True,
        lexical_ran=True,
    )
    assert out["verdict"] == "no lexeme match; not embedded"


def test_track_read_failure_reports_itself_and_keeps_the_results() -> None:
    class _Broken(_Repo):
        async def paths_under_prefix(self, *a, **k):
            raise RuntimeError("boom")

    res = _search(_Broken(), _Embedder(), track_path="src/a.cpp")
    assert res.units and res.tracked["verdict"] == "track read failed: RuntimeError"


def test_track_path_bypasses_the_served_response_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app import search_memo as memo

    monkeypatch.setattr(settings, "code_search_memo_enabled", True)
    assert memo.enabled(CodeSearchInput(query="q"))
    assert not memo.enabled(CodeSearchInput(query="q", track_path="src/a.cpp"))


def test_legs_shapes_the_memo_key() -> None:
    from server.code.app import search_memo as memo

    def key(**kw: Any) -> str:
        return memo.key_for(
            CodeSearchInput(query="q", **kw),
            embedding_model="m",
            rerank="",
            token_budget=3000,
            recent_dirs=[],
        )

    assert key() != key(legs="lexical") != key(legs="vector")
    assert key() == key(legs="hybrid") or key(legs=None) == key()


# ---- trace row -----------------------------------------------------------------


class _Insert:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def client_for(self) -> Any:
        outer = self

        class _Client:
            class client:
                @staticmethod
                async def insert(table, rows, *, column_names, settings):
                    outer.rows.append(dict(zip(column_names, rows[0], strict=True)))

        return _Client()


def _trace(ins: _Insert, **extra: Any) -> None:
    async def go() -> None:
        with patch("server.core.ch.get_client", return_value=ins.client_for()):
            cc.schedule_code_trace(
                org="o",
                ns="n",
                op="code.search",
                query="q",
                route="hybrid",
                returned_ids=["a", "b"],
                returned_paths=["p", "q"],
                returned_scores=[0.5, 0.4],
                top_k=2,
                tokens_returned=10,
                abstain=False,
                retrieval_risk="low",
                embedding_model="m",
                started_monotonic=0.0,
                trace_run_id="r",
                **extra,
            )
            await asyncio.sleep(0)
            await asyncio.gather(*cc._TRACE_TASKS)

    asyncio.run(go())


def test_trace_row_carries_the_aligned_provenance_arrays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_trace_log_enabled", True)
    ins = _Insert()
    units = [
        CodeUnit(unit_id="a", path="p", via="both", lexical_rank=1, vector_rank=2),
        CodeUnit(unit_id="b", path="q", via="lexical", lexical_rank=2),
    ]
    _trace(ins, **cc.unit_provenance(units))
    row = ins.rows[0]
    assert row["returned_via"] == ["both", "lexical"]
    assert row["lexical_ranks"] == [1, 2] and row["vector_ranks"] == [2, 0]
    # omitted = empty arrays, never missing keys
    ins2 = _Insert()
    _trace(ins2)
    assert ins2.rows[0]["returned_via"] == [] and ins2.rows[0]["vector_ranks"] == []


def test_trace_row_carries_the_grep_scope_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_trace_log_enabled", True)
    ins = _Insert()
    _trace(ins, chunks_matched=7, path_prefix="src/Storages/StorageFactory.cpp")
    assert ins.rows[0]["chunks_matched"] == 7
    assert ins.rows[0]["path_prefix"] == "src/Storages/StorageFactory.cpp"
    _trace(ins)
    assert ins.rows[1]["chunks_matched"] == 0 and ins.rows[1]["path_prefix"] == "", (
        "other ops write the defaults, never a missing key"
    )
    assert list(ins.rows[0]) == cc.TRACE_COLUMNS


# ---- retrieval pairs ---------------------------------------------------------------


def test_exported_pairs_carry_via_when_the_trace_row_has_it(tmp_path) -> None:
    from server.code.app.index.retrieval_pairs import export_pairs

    class _CH:
        async def query_all(self, sql, params=None):
            assert "t.returned_via AS via" in sql
            return [
                {
                    "query": "how does the merge selector pick parts",
                    "route": "hybrid",
                    "ts": "2026-09-14 10:00:00",
                    "ids": ["u1", "u2"],
                    "paths": ["src/A.cpp", "src/B.cpp"],
                    "via": ["vector", "both"],
                    "action": "opened_file",
                    "targets": ["src/B.cpp"],
                },
                {
                    "query": "where is the part log written to disk",
                    "route": "hybrid",
                    "ts": "2026-09-14 10:01:00",
                    "ids": ["u3", "u4"],
                    "paths": ["src/C.cpp", "src/D.cpp"],
                    "via": [],
                    "action": "opened_file",
                    "targets": ["src/C.cpp"],
                },
            ]

    out = tmp_path / "pairs.jsonl"
    asyncio.run(export_pairs(CodeRepo(_CH()), "o", "n", out_path=str(out)))
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert rows[0]["positive"] == {"unit_id": "u2", "path": "src/B.cpp", "via": "both"}
    assert rows[0]["negatives"][0]["via"] == "vector"
    # a row with an empty provenance array exports '' rather than guessing
    assert rows[1]["positive"]["via"] == "" and rows[1]["negatives"][0]["via"] == ""
