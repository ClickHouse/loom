"""History intent inside code.search: a narrow classifier for
questions ABOUT a change, a `history` section answered from the PR plane
inside the token budget, the decision-0060 lift shape for the PRs' files,
route names history_number / history_phrase, plain-WHERE statements, and a
default that adds no reads to an ordinary search."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import history as ch
from server.code.app import search as cs
from server.code.app.history import (
    HistoryIntent,
    anchor_paths,
    apply_file_anchors,
    classify_history_intent,
    section_tokens,
)
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.settings import settings

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

# ---- classifier: table-driven --------------------------------------------------------

_FIRES: list[tuple[str, str, list[int]]] = [
    ("what fixed issue 117585", "number", [117585]),
    ("What do we know about GitHub issue 68601 from ringerc?", "number", [68601]),
    ("What is the fix status for PR 117428 and its revert PR 117651", "number", [117428, 117651]),
    ("PR #114783 isInjectiveFunction plus/minus type gate", "number", [114783]),
    ("pull request 42", "number", [42]),
    ("#104969", "number", [104969]),
    ("status of #104969", "number", [104969]),
    # a bare #N next to a history phrase is the subject
    ("why was #104969 merged before the drain fix", "number", [104969]),
    ("why was the Buffer drain changed to stop re-flushing", "phrase", []),
    ("when was join_use_nulls conversion fixed", "phrase", []),
    ("who changed the MergeTree reader to skip empty parts", "phrase", []),
    ("which PR fixed the trailing empty chunk merge bug", "phrase", []),
    ("What is the fix status for the 01599 flaky test on arm_darwin?", "phrase", []),
    ("has this been fixed: LOGICAL_ERROR in EXPLAIN ANALYZE", "phrase", []),
]

_SILENT: list[str] = [
    "how does quota enforcement work",
    "parseQuotaKey",
    "DB::IColumn::insert",
    # mined issue titles cite a PR as a symptom, not as the subject
    "Same-database Buffer->Buffer chain silently loses acked rows on graceful shutdown after "
    "#104969 (drain no longer re-flushes)",
    # backport titles name the PR they port; localization, not history
    "Backport #95164 to 25.10: Fix performance issue in reading of iceberg avro manifest files",
    "Cherry pick #85371 to 25.8: Check access rights during typo correction hints generation",
    # bare "introduced" / "regressed" are symptom words in issue titles
    "ATTEMPT_TO_READ_AFTER_EOF in MergeTextIndexesTask when a merge includes a 0-row part "
    "(regression since 26.6)",
    "the analyzer change introduced a wrong result for IN subqueries",
    "wrong count() after ALTER ADD COLUMN with join_algorithm='direct'",
    # versions and small numbers are not mentions
    "reading parquet in 26.6 and 26.7 is slower",
    "issue 5",
    "",
]


@pytest.mark.parametrize(("query", "kind", "numbers"), _FIRES)
def test_history_intent_fires_on_number_mentions_and_phrasings(
    query: str, kind: str, numbers: list[int]
) -> None:
    got = classify_history_intent(query)
    assert got is not None, query
    assert got.kind == kind and got.numbers == numbers


@pytest.mark.parametrize("query", _SILENT)
def test_history_intent_is_silent_on_ordinary_and_issue_shaped_queries(query: str) -> None:
    assert classify_history_intent(query) is None, query


def test_history_intent_caps_numbers_at_three_in_order_without_duplicates() -> None:
    got = classify_history_intent("issue 11 issue 22 PR 33 issue 11 PR 44")
    assert got == HistoryIntent("number", [11, 22, 33], "")


def test_guard_fixtures_never_fire_the_number_intent() -> None:
    """The history-intent gate: zero fires on the authored conceptual lane
    and on the two mined lanes (issue titles, PR titles)."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "bench" / "fixtures"
    fired: dict[str, int] = {}
    for name in (
        "code_intel_qa.jsonl",
        "code_intel_mined_issue.jsonl",
        "code_intel_mined_pr_title.jsonl",
    ):
        n = 0
        for line in (root / name).read_text().splitlines():
            if not line.strip() or line.startswith("//"):
                continue
            it = classify_history_intent(str(json.loads(line)["query"]))
            if it is not None and it.kind == "number":
                n += 1
        fired[name] = n
    assert fired == {k: 0 for k in fired}, fired


# ---- section shape and budget ---------------------------------------------------------


class _Repo:
    """Stub carrying the three PR-plane reads and the anchor read."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.kw: dict[str, Any] = {}

    async def items_by_number(self, org: str, ns: str, nums: list[int]) -> dict[int, Any]:
        self.calls.append("items_by_number")
        return {
            117585: {
                "is_pr": False,
                "state": "closed",
                "state_reason": "completed",
                "title": "base58 limb accumulator is slow",
                "author": "r",
                "opened_at": "o",
                "closed_at": "c",
                "merged_at": None,
            },
            114783: {
                "is_pr": True,
                "state": "closed",
                "state_reason": "",
                "title": "Fix gate",
                "author": "a",
                "opened_at": "o",
                "closed_at": "c",
                "merged_at": "m",
            },
        }

    async def prs_by_number(self, org: str, ns: str, nums: list[int], **k: Any) -> dict[int, Any]:
        self.calls.append("prs_by_number")
        return {
            114783: {
                "pr_number": 114783,
                "title": "Fix gate",
                "author": "a",
                "merged_at": "2026-01-01 00:00:00",
                "files": ["src/Functions/gate.cpp", "tests/q.sql"],
                "linked_issues": [],
            }
        }

    async def prs_linking_issue(self, org: str, ns: str, nums: list[int], **k: Any) -> list[Any]:
        self.calls.append("prs_linking_issue")
        self.kw["linking"] = k
        return [
            {
                "pr_number": 117600,
                "title": "Speed up base58",
                "author": "b",
                "merged_at": "2026-02-01 00:00:00",
                "files": ["src/Functions/base58.cpp"],
                "linked_issues": [117585],
                "is_revert": 0,
            },
            {
                "pr_number": 117590,
                "title": "Revert speed up",
                "author": "b",
                "merged_at": "2026-01-20 00:00:00",
                "files": ["src/Functions/base58.cpp"],
                "linked_issues": [117585],
                "is_revert": 1,
            },
        ]

    async def chunks_for_paths(
        self, org: str, ns: str, model: str, paths: list[str], **k: Any
    ) -> list[dict[str, Any]]:
        self.calls.append("chunks_for_paths")
        self.kw["paths"] = paths
        return [
            {
                "chunk_id": f"u:{p}",
                "path": p,
                "scope": "f",
                "kind": "class_skeleton",
                "start_line": 1,
                "end_line": 3,
                "content": "x",
                "symbol_ids": [],
                "context_description": "",
            }
            for p in paths
        ]

    async def pr_corpus_stats(self, org: str, ns: str, tokens: list[str], **k: Any) -> Any:
        self.calls.append("pr_corpus_stats")
        return [2] * len(tokens), 5000

    async def pr_history_search(
        self, org: str, ns: str, tokens: list[str], idfs: list[float], **k: Any
    ) -> list[dict[str, Any]]:
        self.calls.append("pr_history_search")
        self.kw["search"] = k
        return [
            {
                "pr_number": 9,
                "title": "Fix buffer drain",
                "author": "z",
                "merged_at": "2026-03-01",
                "files": ["src/Storages/Buffer.cpp"],
                "pr_score": 3.5,
            }
        ]


def test_number_section_lists_items_then_own_pr_then_closing_prs_newest_first() -> None:
    repo = _Repo()
    section = asyncio.run(ch.history_for_numbers(repo, "o", "n", [117585, 114783], max_prs=3))
    assert sorted(repo.calls) == ["items_by_number", "prs_by_number", "prs_linking_issue"]
    assert section["intent"] == "number" and section["numbers"] == [117585, 114783]
    assert [i["kind"] for i in section["items"]] == ["issue", "pr"]
    assert section["items"][0]["state"] == "closed"
    prs = section["prs"]
    # the PR named directly comes first; the issue's closing PRs follow, newest first
    assert [p["pr_number"] for p in prs] == [114783, 117600, 117590]
    assert prs[0]["role"] == "pr" and prs[1]["role"] == "fixes #117585"
    assert prs[2]["is_revert"] is True and "body_excerpt" not in prs[0]
    assert prs[0]["files_total"] == 2


def test_number_section_respects_max_prs_and_unknown_numbers() -> None:
    class _Empty(_Repo):
        async def items_by_number(self, *a: Any) -> dict[int, Any]:
            return {}

        async def prs_by_number(self, *a: Any, **k: Any) -> dict[int, Any]:
            return {}

        async def prs_linking_issue(self, *a: Any, **k: Any) -> list[Any]:
            return []

    section = asyncio.run(ch.history_for_numbers(_Empty(), "o", "n", [5], max_prs=1))
    assert section["items"] == [{"number": 5, "kind": "unknown"}] and section["prs"] == []
    section = asyncio.run(ch.history_for_numbers(_Repo(), "o", "n", [117585, 114783], max_prs=1))
    assert [p["pr_number"] for p in section["prs"]] == [114783]


def test_phrase_section_scores_with_the_history_leg_statement_and_pool_n() -> None:
    repo = _Repo()
    section = asyncio.run(
        ch.history_for_phrase(
            repo,
            "o",
            "n",
            ["buffer", "drain"],
            max_prs=3,
            before="2026-04-01",
        )
    )
    assert repo.calls == ["pr_corpus_stats", "pr_history_search"]
    assert repo.kw["search"]["pool"] == 3 and repo.kw["search"]["before"] == "2026-04-01"
    assert section["prs"][0]["pr_number"] == 9 and section["prs"][0]["score"] == 3.5
    assert section["prs"][0]["role"] == "matched"


def test_section_tokens_and_anchor_paths_are_bounded() -> None:
    section = {
        "intent": "number",
        "prs": [
            {"pr_number": 1, "files": ["src/a.cpp", "tests/t.sql", "src/b.cpp"]},
            {"pr_number": 2, "files": ["src/b.cpp", "src/c.cpp", "src/d.cpp"]},
        ],
    }
    assert section_tokens({}) == 0 and section_tokens(section) >= 20
    assert anchor_paths(section, ["src/"], max_files=3) == ["src/a.cpp", "src/b.cpp", "src/c.cpp"]
    assert anchor_paths(section, [], max_files=10)[1] == "tests/t.sql"
    assert anchor_paths({"prs": []}, ["src/"], max_files=3) == []


def _u(uid: str, path: str, score: float) -> CodeUnit:
    return CodeUnit(unit_id=uid, path=path, start_line=1, end_line=2, score=score)


def test_apply_file_anchors_keeps_present_files_and_lifts_absent_ones_first() -> None:
    ranked = [_u("a", "src/a.cpp", 0.9), _u("b", "src/b.cpp", 0.8), _u("c", "src/c.cpp", 0.7)]
    anchors = [_u("hb", "src/b.cpp", 0.0), _u("hx", "src/x.cpp", 0.0), _u("hy", "src/y.cpp", 0.0)]
    out, placed = apply_file_anchors(ranked, anchors, top_k=3)
    assert placed == [
        {"path": "src/b.cpp", "placed": "present"},
        {"path": "src/x.cpp", "placed": "lifted"},
        {"path": "src/y.cpp", "placed": "lifted"},
    ]
    # lifted units enter at rank 1 in order; nothing is dropped; b stays where fusion put it
    assert [u.unit_id for u in out] == ["hx", "hy", "a", "b", "c"]
    assert all(u.via == "history" and u.score > 0.9 for u in out[:2])
    same, none = apply_file_anchors(ranked, [], top_k=3)
    assert same == ranked and none == []


# ---- inside search(): gating, reads, route, budget --------------------------------------


_UNIT = dict(
    scope="f",
    kind="function",
    start_line=1,
    end_line=5,
    content="x" * 400,
    symbol_ids=[],
    context_description="",
)


class _SearchRepo(_Repo):
    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [
            {"chunk_id": "a", "lex_score": 4.0, "path": "src/a.cpp", **_UNIT},
            {"chunk_id": "b", "lex_score": 1.0, "path": "src/Functions/gate.cpp", **_UNIT},
        ]

    async def vector_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [
            {"chunk_id": "c", "distance": 0.1, "path": "src/c.cpp", **_UNIT},
            {"chunk_id": "a", "distance": 0.2, "path": "src/a.cpp", **_UNIT},
        ]

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _search(
    repo: _Repo, query: str, *, route_setting: str = "off", token_budget: int = 3000, **kw: Any
) -> Any:
    with (
        patch.object(settings, "code_fusion_signals", False),
        patch.object(settings, "code_search_pr_leg", "off"),
        patch.object(settings, "code_search_history_route", route_setting),
        patch.object(settings, "code_search_history_max_prs", 3),
        patch.object(settings, "code_search_history_anchor_max_files", 5),
    ):
        return asyncio.run(
            cs.search(
                repo,
                "o",
                "ns",
                query=query,
                top_k=3,
                token_budget=token_budget,
                embedder=_Embedder(),
                **kw,
            )
        )


def test_route_off_adds_no_reads_and_no_section() -> None:
    repo = _SearchRepo()
    res = _search(repo, "what fixed issue 117585", route_setting="off")
    assert repo.calls == [] and res.history == {} and res.route == "hybrid"
    assert "history_route" not in res.score_explanation


def test_number_route_serves_the_section_lifts_absent_files_and_pays_for_it() -> None:
    repo = _SearchRepo()
    res = _search(repo, "what fixed issue 117585 and PR 114783", route_setting="number")
    assert sorted(repo.calls) == [
        "chunks_for_paths",
        "items_by_number",
        "prs_by_number",
        "prs_linking_issue",
    ]
    # only src/ files anchor, PR order then file order, one unit per file
    assert repo.kw["paths"] == ["src/Functions/gate.cpp", "src/Functions/base58.cpp"]
    assert res.route == "history_number"
    assert res.history["intent"] == "number"
    assert [p["pr_number"] for p in res.history["prs"]] == [114783, 117600, 117590]
    assert res.history["anchored"] == [
        {"path": "src/Functions/gate.cpp", "placed": "present"},
        {"path": "src/Functions/base58.cpp", "placed": "lifted"},
    ]
    ids = [u.unit_id for u in res.units]
    # the absent file entered at rank 1; the fused order behind it is intact
    assert ids == ["u:src/Functions/base58.cpp", "a", "c"]
    assert res.units[0].via == "history"
    # the section is paid for inside the budget and disclosed
    assert res.history["tokens"] > 0
    assert res.tokens_returned >= res.history["tokens"]
    assert "history_route:number(prs=3, items=2, lifted=1," in res.score_explanation
    assert res.retrieval_work["history_route_queries"] == 1


def test_number_setting_does_not_run_the_phrase_route() -> None:
    repo = _SearchRepo()
    res = _search(repo, "why was the buffer drain changed", route_setting="number")
    assert repo.calls == [] and res.history == {}


def test_phrase_route_serves_three_pr_rows_without_anchoring_by_default() -> None:
    repo = _SearchRepo()
    res = _search(repo, "why was the buffer drain changed", route_setting="on")
    assert repo.calls == ["pr_corpus_stats", "pr_history_search"]
    assert res.route == "history_phrase"
    assert [p["pr_number"] for p in res.history["prs"]] == [9]
    assert "anchored" not in res.history
    assert [u.unit_id for u in res.units] == ["a", "c", "b"]


def test_section_budget_is_reserved_before_bodies_and_the_ceiling_holds() -> None:
    repo = _SearchRepo()
    res = _search(repo, "what fixed issue 117585", route_setting="number")
    assert res.tokens_returned <= 3000
    # a tight budget still serves the section and elides bodies instead
    res_small = _search(
        _SearchRepo(), "what fixed issue 117585", route_setting="number", token_budget=260
    )
    assert res_small.history["prs"] and res_small.tokens_returned >= res_small.history["tokens"]


def test_route_failure_degrades_to_the_two_legs_and_says_so() -> None:
    class _Broken(_SearchRepo):
        async def prs_linking_issue(self, *a: Any, **k: Any) -> list[Any]:
            raise RuntimeError("column linked_issues does not exist")

    res = _search(_Broken(), "what fixed issue 117585", route_setting="number")
    assert [u.unit_id for u in res.units] == ["a", "c", "b"]
    assert res.route == "hybrid" and res.history.get("failed") is True
    assert "failed" in res.score_explanation


def test_no_content_hit_still_serves_the_named_change_files() -> None:
    class _Silent(_SearchRepo):
        async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return []

        async def vector_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return []

    res = _search(_Silent(), "#114783", route_setting="number")
    assert res.route == "history_number" and not res.abstain
    # PR 114783's own src/ file; the stub's issue-closing PRs link 117585, not it
    assert [u.path for u in res.units] == ["src/Functions/gate.cpp"]
    assert [p["pr_number"] for p in res.history["prs"]] == [114783]
    assert res.units[0].via == "history" and res.retrieval_risk == "medium"


def test_constrained_search_serves_the_section_but_never_anchors() -> None:
    from server.code.domain.search_scope import build_scope

    class _Scoped(_SearchRepo):
        async def scope_file_count(self, *a: Any, **k: Any) -> int:
            return 2

    repo = _Scoped()
    scope = build_scope(
        pattern="drain",
        regex=False,
        case_sensitive=False,
        path_globs=[],
        exclude_globs=[],
        langs=[],
    )
    res = _search(repo, "what fixed issue 117585", route_setting="number", scope=scope)
    assert "chunks_for_paths" not in repo.calls and res.history["prs"]


# ---- statements (stub client) ---------------------------------------------------------------


class _Client:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.params: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.sql.append(sql)
        self.params.append(params or {})
        return []


def _repo() -> tuple[CodeRepo, _Client]:
    client = _Client()
    repo = CodeRepo.__new__(CodeRepo)
    repo._ch = client  # type: ignore[attr-defined]
    return repo, client


def test_prs_linking_issue_reads_the_column_under_plain_where_with_bots_excluded() -> None:
    repo, client = _repo()
    asyncio.run(repo.prs_linking_issue("o", "n", [117585, 5, 117585], limit=6))
    sql, params = client.sql[0], client.params[0]
    assert "PREWHERE" not in sql.upper()
    assert "hasAny(linked_issues, {nums:Array(UInt32)})" in sql
    assert "FROM loom.code_pr_history FINAL" in sql and "ORDER BY merged_at DESC" in sql
    assert "NOT endsWith(author, '[bot]')" in sql and "LIMIT {limit:UInt32}" in sql
    assert params["nums"] == [5, 117585] and params["limit"] == 6
    assert params["revert_re"] == r"(?i)^\s*revert\b"
    # the revert marker is a parameter: nothing for the SQL lexer to unescape
    assert "match(title, {revert_re:String})" in sql


def test_prs_by_number_now_carries_files_and_linked_issues() -> None:
    repo, client = _repo()
    asyncio.run(repo.prs_by_number("o", "n", [3]))
    assert "any(files) AS files, any(linked_issues) AS linked_issues" in client.sql[0]
    assert "PREWHERE" not in client.sql[0].upper()


def test_empty_numbers_read_nothing() -> None:
    repo, client = _repo()
    assert asyncio.run(repo.prs_linking_issue("o", "n", [0, -1])) == []
    assert client.sql == []
