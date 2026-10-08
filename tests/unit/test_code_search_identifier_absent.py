"""A symbol-shaped query that misses the symbol table is
probed for the literal identifier; absent from every chunk, the search
abstains before any leg runs; present, the hybrid legs run."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
from server.code.app.search.symbol import _case_folded_symbol_spelling, _identifier_probe
from server.settings import settings


def _lex(cid: str, score: float) -> dict[str, Any]:
    return {
        "chunk_id": cid,
        "score": score,
        "path": f"src/{cid}.go",
        "start_line": 1,
        "end_line": 5,
        "content": "x",
        "kind": "function",
        "scope": "",
        "symbol_ids": [],
        "lang": "go",
        "context_description": "",
        "embedding_model": "fake-embed",
    }


class _Repo:
    def __init__(self, *, files_mentioning: int, symbols: dict[str, Any] | None = None) -> None:
        self.files_mentioning = files_mentioning
        self.calls: list[str] = []
        self.probe_scope: Any = None
        self.symbols = symbols or {}
        self.symbol_names: list[str] = []

    async def symbols_by_name(self, org, ns, name, limit=10):
        self.calls.append("symbols")
        self.symbol_names.append(name)
        if name in self.symbols:
            return [
                {
                    "symbol_id": f"sym:{name}",
                    "path": "src/Common/ErrorCodes.cpp",
                    "qualified_name": f"DB::ErrorCodes::{name}",
                    "name": name,
                    "kind": "constant",
                    "start_line": 57,
                    "end_line": 57,
                    "signature": "= 49",
                    "tier": "fast",
                }
            ]
        return []

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def scope_file_count(self, org, ns, model, scope, *, path_prefix=""):
        self.calls.append("probe")
        self.probe_scope = scope
        return self.files_mentioning

    async def code_corpus_stats(self, org, ns, tokens):
        self.calls.append("stats")
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        self.calls.append("lexical")
        self.lexical_scope = k.get("scope")
        return [_lex("a", 40.0)]

    async def vector_search_chunks(self, *a, **k):
        self.calls.append("vector")
        return []

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _search(repo: _Repo, query: str, **kw: Any) -> cs.CodeSearchResult:
    with patch.object(settings, "code_fusion_signals", False):
        return asyncio.run(
            cs.search(repo, "o", "n", query=query, top_k=3, embedder=_Embedder(), **kw)
        )


def test_probe_literal_is_the_bare_member_without_call_parens() -> None:
    assert _identifier_probe("_FooService_Bar_Handler") == "_FooService_Bar_Handler"
    assert _identifier_probe("DB::IColumn::insert()") == "insert"
    assert _identifier_probe("pkg.Recv.Name") == "Name"
    assert _identifier_probe("a.b.util::Recv.Name") == "Name"
    assert _identifier_probe("two words") == ""


def test_symbol_miss_with_no_mentioning_file_abstains_before_any_leg() -> None:
    repo = _Repo(files_mentioning=0)
    res = _search(repo, "_CITrendsService_GetFailingTests_Handler")
    assert res.abstain is True and res.abstain_code == "identifier_absent"
    assert res.route == "identifier_absent" and res.retrieval_risk == "abstain"
    assert res.retrieval_work["identifier_probe_files"] == 0
    assert "lexical" not in repo.calls and "vector" not in repo.calls
    assert repo.probe_scope.pattern == "_CITrendsService_GetFailingTests_Handler"
    assert repo.probe_scope.case_sensitive is True
    assert "generated" in res.abstain_reason


def test_symbol_miss_with_a_mentioning_file_runs_the_hybrid_legs() -> None:
    repo = _Repo(files_mentioning=3)
    res = _search(repo, "_CITrendsService_GetFailingTests_Handler")
    assert res.abstain_code != "identifier_absent"
    assert res.route in (
        "symbol_miss_hybrid",
        "symbol_miss_scoped+pattern",
    ) or res.route.startswith("hybrid")
    assert "lexical" in repo.calls
    assert res.retrieval_work["identifier_probe_files"] == 3


# ------------------------------------------------------------------ scoped legs


def test_a_mentioned_identifier_scopes_the_hybrid_legs_to_its_chunks() -> None:
    """Present in 3 files (under the cap) → the legs run with a
    pattern scope on the literal, case-sensitive because the query has
    capitals; the route says so."""
    repo = _Repo(files_mentioning=3)
    res = _search(repo, "_CITrendsService_GetFailingTests_Handler")
    # the hybrid path suffixes "+pattern" for a constrained pool, as it does for a caller's pattern
    assert res.route == "symbol_miss_scoped+pattern" and res.constrained is True and not res.abstain
    assert repo.lexical_scope is not None
    assert repo.lexical_scope.pattern == "_CITrendsService_GetFailingTests_Handler"
    assert repo.lexical_scope.case_sensitive is True
    assert res.matched_files == 3 and "matched_files=3" in res.score_explanation


def test_a_lower_case_identifier_scopes_case_insensitively_after_a_recount() -> None:
    """`logical_error` means `LOGICAL_ERROR`: the scope is case-insensitive,
    and the pool is recounted under that scope before the cap is judged."""
    repo = _Repo(files_mentioning=2)
    res = _search(repo, "logical_error")
    assert res.route == "symbol_miss_scoped+pattern"
    assert repo.lexical_scope.case_sensitive is False
    assert repo.calls.count("probe") == 2  # the absent probe, then the case-insensitive recount
    assert res.retrieval_work["identifier_scope_files"] == 2


def test_an_identifier_in_more_files_than_the_cap_abstains_with_the_count() -> None:
    """Over the pattern cap the scoped pool would be a namespace-wide content
    scan per leg and the unscoped ranking its sub-words: the answer is the
    count and where to go instead. No leg runs."""
    repo = _Repo(files_mentioning=1518)
    res = _search(repo, "LOGICAL_ERROR", pattern_max_files=1500)
    assert res.abstain is True and res.abstain_code == "pattern_too_broad"
    assert res.route == "symbol_miss_hybrid" and res.retrieval_risk == "abstain"
    assert "1518 files" in res.abstain_reason and "code.grep" in res.abstain_reason
    assert "lexical" not in repo.calls and "vector" not in repo.calls


# ---- a lower-case query retries the exact read in upper case ----------------


def test_a_lower_case_query_retries_the_symbol_read_in_upper_case() -> None:
    repo = _Repo(files_mentioning=1532, symbols={"LOGICAL_ERROR": True})
    out = _search(repo, "logical_error")
    assert repo.symbol_names == ["logical_error", "LOGICAL_ERROR"]
    assert out.route == "symbol_exact" and not out.abstain
    assert [u.symbol for u in out.units] == ["DB::ErrorCodes::LOGICAL_ERROR"]
    assert "'LOGICAL_ERROR'" in out.score_explanation
    assert "probe" not in repo.calls and "lexical" not in repo.calls  # no scoping, no legs


def test_mixed_case_and_qualified_queries_are_not_folded() -> None:
    assert _case_folded_symbol_spelling("logical_error") == "LOGICAL_ERROR"
    assert _case_folded_symbol_spelling(" abc_def ") == "ABC_DEF"
    assert _case_folded_symbol_spelling("LOGICAL_ERROR") == ""
    assert _case_folded_symbol_spelling("fooBar") == ""
    assert _case_folded_symbol_spelling("DB::ErrorCodes::logical_error") == ""
    assert _case_folded_symbol_spelling("src/a.cpp:12") == ""
    assert _case_folded_symbol_spelling("two words") == ""
    repo = _Repo(files_mentioning=3, symbols={"LOGICAL_ERROR": True})
    out = _search(repo, "fooBar")
    assert repo.symbol_names == ["fooBar"] and out.route != "symbol_exact"
