"""code.search history leg: merged-PR text scored lexically
votes for the files it touched; the votes join the fusion as a third RRF
list, capped so history corroborates and never outranks; gated by the
issue shape or forced per request; the statements use plain WHERE."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import search as cs
from server.code.app.search import classify_query_shape
from server.code.app.search.fusion import _RRF_K, _rrf_fuse
from server.code.app.search.history import file_votes
from server.code.app.search.shape import is_issue_shaped
from server.code.store.index import CodeRepo
from server.settings import settings

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

# ---- classifier ---------------------------------------------------------------


def test_issue_shape_fires_on_length_or_vocabulary() -> None:
    assert classify_query_shape("how does quota enforcement work") == "hybrid"
    assert classify_query_shape("parseQuotaKey") == "symbol"
    assert classify_query_shape("DB::IColumn::insert") == "symbol"
    long_q = "why does the merge tree reader return the same row twice when a mutation is in flight"
    assert len(long_q.split()) >= 12 and classify_query_shape(long_q) == "issue"
    assert classify_query_shape("Logical error in EXPLAIN ANALYZE") == "issue"
    assert classify_query_shape("SIGSEGV with join_algorithm='direct'") == "issue"
    assert classify_query_shape("exception thrown by parser") == "issue"
    assert classify_query_shape("stack trace on startup") == "issue"
    assert classify_query_shape("wrong count() after ALTER") == "issue"
    assert not is_issue_shaped("where is backpressure handled")
    # a one-word identifier never becomes an issue, even with the vocabulary
    assert classify_query_shape("ErrorCodes") == "symbol"
    assert classify_query_shape("errors") == "hybrid"


# ---- vote math ------------------------------------------------------------------


def _pr(n: int, score: float, files: list[str]) -> dict[str, Any]:
    return {"pr_number": n, "pr_score": score, "files": files, "title": f"pr {n}"}


def test_file_votes_damp_by_pr_size_and_filter_prefixes() -> None:
    prs = [
        _pr(1, 10.0, ["src/A.cpp", "src/A.h"]),
        _pr(2, 10.0, ["src/B.cpp"] + [f"src/X{i}.cpp" for i in range(47)]),
        _pr(3, 4.0, ["tests/t.sql", "src/A.cpp"]),
        _pr(4, 0.0, ["src/Z.cpp"]),  # no score -> no vote
    ]
    votes = dict(file_votes(prs, ["src/"]))
    assert "tests/t.sql" not in votes and "src/Z.cpp" not in votes
    # A.cpp: 10/log(4) from the 2-file fix + 4/log(4) from the 2-file PR 3
    assert votes["src/A.cpp"] == pytest.approx(10 / math.log(4) + 4 / math.log(4))
    # B.cpp: the 48-file sweep says little about any one file
    assert votes["src/B.cpp"] == pytest.approx(10 / math.log(50))
    assert votes["src/A.cpp"] > votes["src/A.h"] > votes["src/B.cpp"]
    ranked = [f for f, _ in file_votes(prs, ["src/"])]
    assert ranked[:2] == ["src/A.cpp", "src/A.h"]
    # no prefixes = every file votes; ties break on path for determinism
    assert "tests/t.sql" in dict(file_votes(prs, []))


# ---- fusion -------------------------------------------------------------------------


def _row(cid: str, **extra: Any) -> dict[str, Any]:
    return {"chunk_id": cid, "path": f"src/{cid}.cpp", **extra}


def test_rrf_two_leg_result_is_unchanged_without_history() -> None:
    lex = [_row("a"), _row("b")]
    vec = [_row("c"), _row("a")]
    before = _rrf_fuse(lex, vec)
    assert _rrf_fuse(lex, vec, None) == before
    assert _rrf_fuse(lex, vec, [], history_cap=0.25) == before
    assert [r["chunk_id"] for r, _ in before] == ["a", "c", "b"]


def test_history_list_is_a_third_rrf_list_capped_at_a_fraction_of_the_top() -> None:
    lex = [_row("a"), _row("b")]
    vec = [_row("c"), _row("a")]
    hist = [_row("b"), _row("h")]  # b corroborated; h history-only
    fused = dict((r["chunk_id"], s) for r, s in _rrf_fuse(lex, vec, hist, history_cap=0.25))
    top = 1 / 61 + 1 / 62  # a: lexical #1 + vector #2
    assert fused["a"] == pytest.approx(top)
    # b: lexical #2 + history #1 capped at 0.25 x top (1/61 would exceed it)
    assert fused["b"] == pytest.approx(1 / 62 + min(1 / 61, 0.25 * top))
    # a history-only unit can never reach the top on its own
    assert fused["h"] == pytest.approx(min(1 / 62, 0.25 * top)) and fused["h"] < fused["c"]
    order = [r["chunk_id"] for r, _ in _rrf_fuse(lex, vec, hist, history_cap=0.25)]
    assert order == ["a", "b", "c", "h"]
    # uncapped, the history row contributes the plain reciprocal rank
    loose = dict((r["chunk_id"], s) for r, s in _rrf_fuse(lex, vec, hist, history_cap=None))
    assert loose["b"] == pytest.approx(1 / 62 + 1 / 61)
    # k stays 60: the fusion operator is not changed by the third list
    assert _RRF_K == 60.0


# ---- the leg inside search(): gating, list count, disclosure --------------------


_UNIT = dict(
    scope="f",
    kind="function",
    start_line=1,
    end_line=5,
    content="x",
    symbol_ids=[],
    context_description="",
)


class _Repo:
    def __init__(self, *, n_prs: int = 5000) -> None:
        self.n_prs = n_prs
        self.calls: list[str] = []
        self.kw: dict[str, Any] = {}

    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [
            {"chunk_id": "a", "lex_score": 4.0, "path": "src/a.cpp", **_UNIT},
            {"chunk_id": "b", "lex_score": 1.0, "path": "src/b.cpp", **_UNIT},
        ]

    async def vector_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [
            {"chunk_id": "c", "distance": 0.1, "path": "src/c.cpp", **_UNIT},
            {"chunk_id": "a", "distance": 0.2, "path": "src/a.cpp", **_UNIT},
        ]

    async def pr_corpus_stats(self, org: str, ns: str, tokens: list[str], **k: Any) -> Any:
        self.calls.append("pr_corpus_stats")
        self.kw["stats"] = k
        return [2] * len(tokens), self.n_prs

    async def pr_history_search(
        self, org: str, ns: str, tokens: list[str], idfs: list[float], **k: Any
    ) -> list[dict[str, Any]]:
        self.calls.append("pr_history_search")
        self.kw["search"] = k
        assert len(idfs) == len(tokens) and all(i > 0 for i in idfs)
        return [_pr(1, 9.0, ["src/hist.cpp", "src/b.cpp"]), _pr(2, 1.0, ["tests/x.sql"])]

    async def chunks_for_paths(
        self, org: str, ns: str, model: str, paths: list[str], **k: Any
    ) -> list[dict[str, Any]]:
        self.calls.append("chunks_for_paths")
        self.kw["paths"] = paths
        return [
            {"chunk_id": "b", "path": "src/b.cpp", **_UNIT},
            {"chunk_id": "h", "path": "src/hist.cpp", **_UNIT},
        ]

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _search(repo: _Repo, query: str, *, pr_leg: str = "off", **kw: Any) -> Any:
    with (
        patch.object(settings, "code_fusion_signals", False),
        patch.object(settings, "code_search_pr_leg", pr_leg),
        patch.object(settings, "code_search_pr_leg_cap", 0.5),
    ):
        return asyncio.run(
            cs.search(repo, "o", "ns", query=query, top_k=5, embedder=_Embedder(), **kw)
        )


def test_default_is_the_issue_gate_since_revision_2() -> None:
    # on populated lexemes the shape-gated leg clears the gate on both mined
    # lanes and leaves the conceptual guard's MRR untouched; "always" does not.
    assert type(settings).model_fields["code_search_pr_leg"].default == "issue"


def test_leg_off_adds_no_reads() -> None:
    repo = _Repo()
    res = _search(repo, "wrong count after ALTER ADD COLUMN")
    assert repo.calls == [] and "history" not in res.score_explanation
    assert res.route == "hybrid" and "history_queries" not in res.retrieval_work


def test_issue_mode_runs_only_for_issue_shaped_queries() -> None:
    repo = _Repo()
    res = _search(repo, "how does quota enforcement work", pr_leg="issue")
    assert repo.calls == [] and "history" not in res.score_explanation
    repo = _Repo()
    res = _search(repo, "wrong count after ALTER ADD COLUMN", pr_leg="issue")
    assert repo.calls == ["pr_corpus_stats", "pr_history_search", "chunks_for_paths"]
    assert res.route == "hybrid+history"


def test_always_mode_fuses_three_lists_and_discloses_the_votes() -> None:
    repo = _Repo()
    before = datetime(2026, 9, 1, tzinfo=UTC)
    res = _search(
        repo,
        "how does quota enforcement work",
        pr_leg="always",
        pr_leg_before=before,
    )
    assert repo.calls == ["pr_corpus_stats", "pr_history_search", "chunks_for_paths"]
    # the time-travel pin reaches both history reads
    assert repo.kw["stats"]["before"] == before and repo.kw["search"]["before"] == before
    # only src/ files vote; hist.cpp (9/log 4) outvotes b.cpp? no — both files
    # of PR 1 share its weight, ties break on path
    assert repo.kw["paths"] == ["src/b.cpp", "src/hist.cpp"]
    assert "history:2 PRs voted(2 files, 2 units, cap=0.5)" in res.score_explanation
    assert res.retrieval_work["history_queries"] == 1
    ids = [u.unit_id for u in res.units]
    # b: lexical #2 + history vote climbs above vector-only c; history-only h trails
    assert ids[:2] == ["a", "b"] and "h" in ids and ids.index("h") > ids.index("c")
    # provenance: a unit only the history leg proposed says so; a content-leg
    # unit the history also voted for keeps its content-leg label
    via = {u.unit_id: u.via for u in res.units}
    assert via["h"] == "history" and via["b"] == "lexical" and via["c"] == "vector"


def test_sparse_history_skips_the_leg_and_says_so() -> None:
    repo = _Repo(n_prs=120)
    res = _search(repo, "anything at all", pr_leg="always")
    assert repo.calls == ["pr_corpus_stats"]
    assert "history:skipped(sparse, prs=120)" in res.score_explanation
    assert res.route == "hybrid"


def test_history_leg_failure_degrades_to_two_legs() -> None:
    class _Broken(_Repo):
        async def pr_history_search(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("column lexemes does not exist")

    repo = _Broken()
    res = _search(repo, "anything at all", pr_leg="always")
    assert [u.unit_id for u in res.units] == ["a", "c", "b"]
    assert "history:0 PRs voted(0 files, 0 units" in res.score_explanation


class _Client:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.params: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.sql.append(sql)
        self.params.append(params or {})
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.sql.append(sql)
        self.params.append(params or {})
        return {"n": 7, "dfs": [3, 0]}


def _repo_with_client() -> tuple[CodeRepo, _Client]:
    client = _Client()
    repo = CodeRepo.__new__(CodeRepo)
    repo._ch = client  # type: ignore[attr-defined]
    return repo, client


def test_pr_history_search_statement_scores_lexemes_with_plain_where() -> None:
    repo, client = _repo_with_client()
    before = datetime(2026, 9, 1, tzinfo=UTC)
    asyncio.run(
        repo.pr_history_search("o", "ns", ["crash", "merge"], [2.0, 1.0], pool=30, before=before)
    )
    (sql,), (params,) = client.sql, client.params
    assert "PREWHERE" not in sql.upper() and "FROM loom.code_pr_history FINAL" in sql
    assert "concat(' ', lexemes, ' ')" in sql and "body_excerpt" not in sql.split("FROM")[0]
    assert "multiSearchAny(" in sql and "countSubstrings(" in sql
    assert "notEmpty(files)" in sql and "NOT endsWith(author, '[bot]')" in sql
    # the rendered SQL carries \\s / \\b so ClickHouse's literal unescape yields \s / \b
    assert "'(?i)^\\\\s*revert\\\\b'" in sql
    assert "merged_at < {before:DateTime}" in sql and params["before"] == before
    assert params["tokens"] == ["crash", "merge"] and params["idfs"] == [2.0, 1.0]
    assert params["pool"] == 30 and "LIMIT {pool:UInt32}" in sql
    assert "linked_issues" in sql and "files" in sql


def test_pr_corpus_stats_is_positional_over_the_voting_prs() -> None:
    repo, client = _repo_with_client()
    dfs, n = asyncio.run(repo.pr_corpus_stats("o", "ns", ["crash", "merge", "zzz"]))
    sql = client.sql[0]
    assert "sumForEach(" in sql and "position(" in sql and "PREWHERE" not in sql.upper()
    assert "notEmpty(files)" in sql and "FROM loom.code_pr_history FINAL" in sql
    assert n == 7 and dfs == [3, 0, 0]  # padded to the token count, positional
    assert asyncio.run(repo.pr_corpus_stats("o", "ns", [])) == ([], 0)


def test_chunks_for_paths_reads_no_vector_and_limits_per_path() -> None:
    repo, client = _repo_with_client()
    asyncio.run(repo.chunks_for_paths("o", "ns", "m", ["src/a.cpp", "src/b.cpp"], per_path=2))
    sql, params = client.sql[0], client.params[0]
    assert "embedding" not in sql.replace("embedding_model", "")
    assert "LIMIT {per_path:UInt32} BY path" in sql and params["per_path"] == 2
    assert "kind = 'class_skeleton' DESC" in sql and "PREWHERE" not in sql.upper()
    assert asyncio.run(repo.chunks_for_paths("o", "ns", "m", [])) == []


def test_gh_item_titles_dedupes_and_maps_numbers() -> None:
    repo, client = _repo_with_client()
    assert asyncio.run(repo.gh_item_titles("o", "ns", [])) == {}
    asyncio.run(repo.gh_item_titles("o", "ns", [5, 5, 3, 0]))
    assert client.params[0]["nums"] == [3, 5]
