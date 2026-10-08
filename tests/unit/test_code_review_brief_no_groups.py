"""The review brief has no `groups` / `reviewers` sections.

These tests pin the payload, tiers and decisions without them, a stored
payload served as it is, and the 422 a request naming one gets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

import server.code.app.review_brief as h
from server.code.api.models_review import CodeReviewBriefInput
from server.code.app.index import review_brief as rb
from server.code.app.index import review_brief_build

# pinned canonical digest of the brief below
PAYLOAD_DIGEST = "be5a9ced5f1a839c827119e7994f84ca9ddfb18446900c31f96dcba01c2e0402"
ORG, NS = "clickhouse", "code-clickhouse"
SHA = "1e93c0a25f177d3a971668e058745a6e8bfe3e2c"
PARENT = "b41b780feecc85780bebca041acda03358719a79"
NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)

# Two activities in one PR: (A) MergeTreeData::loadDataParts calls a new
# helper in MergeTreeData.h, with a stateless test naming the helper; (B) an
# unrelated fix in Parsers/ParserSelectQuery.cpp with a setting declared in
# Settings.cpp and used by a second test. Plus a docs hunk nothing anchors.
DIFF = """diff --git a/src/Storages/MergeTree/MergeTreeData.cpp b/src/Storages/MergeTree/MergeTreeData.cpp
--- a/src/Storages/MergeTree/MergeTreeData.cpp
+++ b/src/Storages/MergeTree/MergeTreeData.cpp
@@ -500,4 +500,5 @@ void MergeTreeData::loadDataParts()
 a
-    old_call();
+    loadOutdatedPartsAsync(parts);
 c
diff --git a/src/Storages/MergeTree/MergeTreeData.h b/src/Storages/MergeTree/MergeTreeData.h
--- a/src/Storages/MergeTree/MergeTreeData.h
+++ b/src/Storages/MergeTree/MergeTreeData.h
@@ -90,3 +90,4 @@ class MergeTreeData
     void loadDataParts();
+    void loadOutdatedPartsAsync(const DataPartsVector & parts);
 };
diff --git a/src/Parsers/ParserSelectQuery.cpp b/src/Parsers/ParserSelectQuery.cpp
--- a/src/Parsers/ParserSelectQuery.cpp
+++ b/src/Parsers/ParserSelectQuery.cpp
@@ -40,3 +40,4 @@ bool ParserSelectQuery::parseImpl(Pos & pos, ASTPtr & node, Expected & expected)
     x
+    if (settings.allow_experimental_qualify) parseQualify(pos, node);
     y
diff --git a/src/Parsers/ParserSelectQuery.cpp b/src/Parsers/ParserSelectQuery.cpp
--- a/src/Parsers/ParserSelectQuery.cpp
+++ b/src/Parsers/ParserSelectQuery.cpp
@@ -200,3 +201,4 @@ bool ParserSelectQuery::parseQualify(Pos & pos, ASTPtr & node)
     p
+    return true;
     q
diff --git a/src/Core/Settings.cpp b/src/Core/Settings.cpp
--- a/src/Core/Settings.cpp
+++ b/src/Core/Settings.cpp
@@ -100,3 +100,4 @@ namespace DB
 x
+    DECLARE(Bool, allow_experimental_qualify, false, R"(qualify)", 0) \\
 y
diff --git a/tests/queries/0_stateless/03999_outdated_parts_async.sql b/tests/queries/0_stateless/03999_outdated_parts_async.sql
--- /dev/null
+++ b/tests/queries/0_stateless/03999_outdated_parts_async.sql
@@ -0,0 +1,2 @@
+-- loadOutdatedPartsAsync
+SELECT 1;
diff --git a/tests/queries/0_stateless/03998_qualify.sql b/tests/queries/0_stateless/03998_qualify.sql
--- /dev/null
+++ b/tests/queries/0_stateless/03998_qualify.sql
@@ -0,0 +1,2 @@
+SET allow_experimental_qualify = 1;
+SELECT 1 QUALIFY 1;
diff --git a/docs/en/whats-new/changelog.md b/docs/en/whats-new/changelog.md
--- a/docs/en/whats-new/changelog.md
+++ b/docs/en/whats-new/changelog.md
@@ -1,2 +1,3 @@
 # Changelog
+Filters window results.
"""
PATHS = [
    "src/Storages/MergeTree/MergeTreeData.cpp",
    "src/Storages/MergeTree/MergeTreeData.h",
    "src/Parsers/ParserSelectQuery.cpp",
    "src/Core/Settings.cpp",
    "tests/queries/0_stateless/03999_outdated_parts_async.sql",
    "tests/queries/0_stateless/03998_qualify.sql",
    "docs/en/whats-new/changelog.md",
]
CLASSES = {
    p: ("test" if p.startswith("tests/") else "doc" if p.startswith("docs/") else "src")
    for p in PATHS
}
FN_ROWS = [
    {
        "symbol_id": "s-load",
        "path": PATHS[0],
        "qualified_name": "DB::MergeTreeData::loadDataParts",
        "start_line": 480,
        "end_line": 560,
        "tier": "treesitter",
        "kind": "method",
    },
    {
        "symbol_id": "s-load-scip",
        "path": PATHS[0],
        "qualified_name": "DB::MergeTreeData::loadDataParts",
        "start_line": 480,
        "end_line": 560,
        "tier": "scip",
        "kind": "method",
    },
    {
        "symbol_id": "s-parse",
        "path": PATHS[2],
        "qualified_name": "DB::ParserSelectQuery::parseImpl",
        "start_line": 30,
        "end_line": 120,
        "tier": "treesitter",
        "kind": "method",
    },
    {
        "symbol_id": "s-qualify",
        "path": PATHS[2],
        "qualified_name": "DB::ParserSelectQuery::parseQualify",
        "start_line": 190,
        "end_line": 230,
        "tier": "treesitter",
        "kind": "method",
    },
    # a function of the file the hunks do not touch: never a member of a group
    {
        "symbol_id": "s-far",
        "path": PATHS[2],
        "qualified_name": "DB::ParserSelectQuery::parseFar",
        "start_line": 900,
        "end_line": 950,
        "tier": "treesitter",
        "kind": "method",
    },
]


class _Repo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def functions_for_paths(
        self, org: str, ns: str, paths: list[str], *, limit: int = 400, **_kw
    ) -> list[dict[str, Any]]:
        self.calls.append(("functions_for_paths", paths))
        return FN_ROWS

    async def caller_counts(self, org: str, ns: str, ids: list[str]) -> dict[str, dict[str, int]]:
        return {"s-load-scip": {"call_sites": 3, "registration": 0}}

    async def edges_for_symbols(
        self, org: str, ns: str, ids: list[str], **kw: Any
    ) -> list[dict[str, Any]]:
        self.calls.append(("edges_for_symbols", (sorted(ids), kw)))
        return [
            {"from_symbol_id": "s-parse", "to_symbol_id": "s-qualify", "edge_kind": "calls"},
            {"from_symbol_id": "s-parse", "to_symbol_id": "s-outside", "edge_kind": "calls"},
        ]

    async def unit_risk_rows(self, org: str, ns: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        self.calls.append(("unit_risk_rows", sorted(ids)))
        return {}

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return SHA

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.append((table, rows))


async def _call(op: str, body: dict[str, Any]) -> dict[str, Any]:
    if op == "code.impact":
        return {
            "touched_symbols": [
                {
                    "qualified_name": "DB::MergeTreeData::loadDataParts",
                    "path": PATHS[0],
                    "start_line": 480,
                    "tier": "scip",
                },
                {
                    "qualified_name": "DB::ParserSelectQuery::parseImpl",
                    "path": PATHS[2],
                    "start_line": 30,
                    "tier": "treesitter",
                },
            ],
            "by_hop_counts": {1: 3},
            "verdict": "touched",
            "epistemic": "lower_bound",
            "causes": [],
            "paths_seen": [{"path": p, "class": c} for p, c in CLASSES.items()],
        }
    if op == "code.coverage_claim":
        covered = body.get("symbol") == "DB::MergeTreeData::loadDataParts"
        return {
            "verdict": "covered_by" if covered else "uncovered",
            "tests": [{"test_path": "tests/queries/0_stateless/02994_parts.sql"}]
            if covered
            else [],
            "tests_total": 2 if covered else 0,
            "epistemic": "lower_bound",
        }
    if op == "code.test_home":
        return {
            "verdict": "add_case_to",
            "home": "",
            "candidates": [],
            "parts": [],
            "next_free_stateless_number": 4000,
        }
    if op == "code.setting_facts":
        return {
            "verdict": "known",
            "tier": "production",
            "randomized_in_ci": {"randomized": False},
            "history": [],
            "tests_total": 0,
        }
    if op == "code.symbol_history":
        return {
            "verdict": "history",
            "summary": {
                "prs_total": 1,
                "fix_prs": 0,
                "named_fix_prs": 0,
                "reverts": 0,
                "caused_issues": [],
                "open_issues_naming": 0,
                "last_touched": "",
            },
        }
    if op == "code.blame_range":
        return {
            "verdict": "no_candidates",
            "epistemic": "exact",
            "latest_pr": 0,
            "candidates": [],
            "causes": [],
        }
    raise AssertionError(op)


async def _tracker(
    org: str, ns: str, pr: int, syms: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    return rb.NOT_BOUND, []


def _inputs(**kw: Any) -> rb.BriefInputs:
    base: dict[str, Any] = dict(
        org=ORG,
        ns=NS,
        pr_number=120000,
        merge_sha=SHA,
        title="Two things at once",
        changed_paths=PATHS,
        diff=DIFF,
        parent_sha=PARENT,
        indexed_commit=SHA,
        path_prefixes=("src/", "tests/", "docs/"),
        author="authorlogin",
    )
    base.update(kw)
    return rb.BriefInputs(**base)


class _HandlerRepo:
    def __init__(self, stored: dict[str, Any] | None) -> None:
        self.stored = stored
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def review_brief(
        self, org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
    ) -> dict[str, Any] | None:
        return self.stored

    async def pr_merge_facts(
        self, org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
    ) -> dict[str, Any] | None:
        return {
            "pr_number": 120000,
            "title": "Two things at once",
            "files": PATHS,
            "merge_sha": SHA.encode(),
            "merged_at": "2026-09-16 09:30:00",
            "author": "authorlogin",
        }

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.append((table, rows))


def _digest(payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k != "build_ms"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


@pytest.mark.asyncio
async def test_the_brief_is_built_without_groups_reviewers_or_the_tangled_bit() -> None:
    repo = _Repo()
    payload = await review_brief_build.build_brief(
        _inputs(), call=_call, repo=repo, tracker_rows=_tracker, now=lambda: NOW
    )
    assert list(payload["sections"]) == list(rb.SECTION_ORDER)
    assert "groups" not in payload["sections"] and "reviewers" not in payload["sections"]
    assert set(payload["decisions"]) == {
        "test_only",
        "symbol_with_regressions",
        "uncovered_symbol",
        "randomized_setting",
        "deleted_lines_blamed",
    }
    assert payload["sections"]["symbols"]["status"] == rb.SERVED
    # neither the hunk-group edge read nor the unit-risk read runs
    assert "unit_risk_rows" not in [c[0] for c in repo.calls]
    # byte for byte the pinned payload
    assert _digest(payload) == PAYLOAD_DIGEST


def test_no_tier_carries_them_and_a_stored_payload_trims_as_before() -> None:
    assert rb.tier_sections("trivial", []) == ["paths", "symbols", "do_not_flag"]
    assert "groups" not in rb.tier_sections("standard", []) and "reviewers" not in rb.tier_sections(
        "full", []
    )
    old = {
        "sections": {
            "paths": rb.section(rb.SERVED, verdict="src", rows=[{"path": "a", "class": "src"}])
        }
    }
    served, _, _ = rb.serve(old, tier="standard", requested=["paths"])
    assert served["paths"]["verdict"] == "src" and "groups" not in served
    assert "tangled_change" not in rb.decisions_of(old["sections"])


@pytest.mark.asyncio
async def test_a_stored_payload_serves_as_it_is_and_is_never_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, Any] = {"built": []}
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(h, "set_code_span_attributes", lambda **kw: None)

    async def _build_for_merge(repo: Any, org: str, ns: str, **kw: Any) -> dict[str, Any]:
        state["built"].append(kw)
        return {}

    monkeypatch.setattr(review_brief_build, "build_for_merge", _build_for_merge)
    v1 = {
        "v": 1,
        "pr_number": 120000,
        "merge_sha": SHA,
        "built_at": "2026-09-15T00:00:00Z",
        "decisions": {},
        "sections": {"paths": rb.section(rb.SERVED, verdict="src", rows=[])},
    }
    repo = _HandlerRepo(stored={"pr_number": 120000, "merge_sha": SHA, "payload": json.dumps(v1)})
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=120000, tier="full"))
    assert out.source == "precomputed" and repo.inserted == [] and state["built"] == []
    assert "groups" not in out.sections and "tangled_change" not in out.decisions


def test_a_section_outside_the_served_set_is_a_validation_error() -> None:
    for name in ("groups", "reviewers"):
        with pytest.raises(ValueError):
            CodeReviewBriefInput(pr_number=1, sections=["paths", name])
    with pytest.raises(ValueError):
        CodeReviewBriefInput(pr_number=1, sections=["owners"])
    assert CodeReviewBriefInput(pr_number=1, sections=["paths", "risk"]).sections == [
        "paths",
        "risk",
    ]
