"""code.review_brief: the brief assembles every section from
the ops through one seam, each section carries a verdict, tiers select
sections and a token budget trimmed by whole rows, two builds of the same
inputs serialise byte-identical, and a miss builds once then reads as a
point."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

import server.code.app.review_brief as h
from server.code.api.models_review import CodeReviewBriefInput
from server.code.app.index import review_brief as rb
from server.code.app.index import review_brief_build, review_brief_sources

pytestmark = pytest.mark.usefixtures("mirror_here")

ORG, NS = "clickhouse", "code-clickhouse"
SHA = "1e93c0a25f177d3a971668e058745a6e8bfe3e2c"
PARENT = "b41b780feecc85780bebca041acda03358719a79"

DIFF = """diff --git a/src/Core/Settings.cpp b/src/Core/Settings.cpp
--- a/src/Core/Settings.cpp
+++ b/src/Core/Settings.cpp
@@ -100,3 +100,3 @@ namespace DB
 x
-    DECLARE(Bool, old_flag, false, R"(old)", 0) \\
+    DECLARE(Bool, new_flag, true, R"(new)", 0) \\
 y
diff --git a/src/Core/SettingsChangesHistory.cpp b/src/Core/SettingsChangesHistory.cpp
--- a/src/Core/SettingsChangesHistory.cpp
+++ b/src/Core/SettingsChangesHistory.cpp
@@ -40,2 +40,3 @@
 {
+    {"new_flag", false, true, "New flag."},
 }
diff --git a/src/Storages/MergeTree/MergeTreeData.cpp b/src/Storages/MergeTree/MergeTreeData.cpp
--- a/src/Storages/MergeTree/MergeTreeData.cpp
+++ b/src/Storages/MergeTree/MergeTreeData.cpp
@@ -500,6 +500,5 @@ void MergeTreeData::loadDataParts()
 a
 b
-    old_call();
-    other();
+    new_call();
 c
 d
diff --git a/tests/queries/0_stateless/03999_new_flag.sql b/tests/queries/0_stateless/03999_new_flag.sql
--- /dev/null
+++ b/tests/queries/0_stateless/03999_new_flag.sql
@@ -0,0 +1,2 @@
+SET max_threads = 1, new_flag = 1;
+SELECT 1;
"""
PATHS = [
    "src/Core/Settings.cpp",
    "src/Core/SettingsChangesHistory.cpp",
    "src/Storages/MergeTree/MergeTreeData.cpp",
    "tests/queries/0_stateless/03999_new_flag.sql",
]


# ---- pure ------------------------------------------------------------------


def test_parse_diff_reads_settings_deleted_runs_and_paths() -> None:
    facts = rb.parse_diff(DIFF)
    assert facts.paths == PATHS
    assert dict(facts.settings) == {
        "old_flag": "declared",
        "new_flag": "declared",
        "max_threads": "test_uses",
    }
    # `-` lines by OLD-side number, folded into runs: Settings.cpp line 101;
    # MergeTreeData.cpp 502-503. The new test file deleted nothing.
    assert facts.deleted == {
        "src/Core/Settings.cpp": [(101, 101)],
        "src/Storages/MergeTree/MergeTreeData.cpp": [(502, 503)],
    }
    ranges = rb.deleted_ranges(facts, src_only=lambda p: p.endswith(".cpp"))
    assert ranges == [
        ("src/Storages/MergeTree/MergeTreeData.cpp", 502, 503),
        ("src/Core/Settings.cpp", 101, 101),
    ]
    assert rb.source_stems(PATHS) == ["Settings", "SettingsChangesHistory", "MergeTreeData"]


def test_section_verdicts_are_decisions_not_lists() -> None:
    assert rb.paths_verdict([{"path": "tests/a.sql", "class": "test"}]) == "test_only"
    assert rb.paths_verdict([{"path": "docs/a.md", "class": "doc"}]) == "no_src"
    assert (
        rb.paths_verdict([{"path": "src/a.cpp", "class": "src"}, {"path": "t", "class": "test"}])
        == "src"
    )
    assert rb.tests_verdict([]) == "no_subjects"
    assert (
        rb.tests_verdict(
            [
                {"kind": "symbol", "verdict": "covered_by"},
                {"kind": "setting", "verdict": "uncovered"},
            ]
        )
        == "uncovered"
    )
    assert (
        rb.tests_verdict([{"kind": "symbol", "verdict": "covered_by"}, {"kind": "home"}])
        == "covered"
    )
    assert (
        rb.tests_verdict(
            [{"kind": "symbol", "verdict": "covered_by"}, {"kind": "symbol", "verdict": "unknown"}]
        )
        == "partly_covered"
    )
    assert rb.settings_verdict([{"verdict": "known", "randomized_in_ci": True}]) == "randomized"
    assert rb.settings_verdict([{"verdict": "known", "randomized_in_ci": False}]) == "known"
    assert rb.settings_verdict([]) == "none"
    # function-level evidence decides; a fix that only touched the file is a floor
    assert (
        rb.history_verdict([{"verdict": "history", "fix_prs": 2, "named_fix_prs": 1}])
        == "regressions"
    )
    assert rb.history_verdict([{"verdict": "history", "fix_prs": 0, "reverts": 1}]) == "regressions"
    assert (
        rb.history_verdict([{"verdict": "history", "fix_prs": 2, "named_fix_prs": 0}])
        == "file_fixes"
    )
    assert (
        rb.history_verdict(
            [{"verdict": "no_history", "fix_prs": 0, "reverts": 0, "caused_issues": 0}]
        )
        == "clean"
    )
    assert rb.history_verdict([{"verdict": "unknown"}]) == "unknown"
    assert rb.blame_verdict([], ranges=0) == "no_deleted_lines"
    assert rb.blame_verdict([{"latest_pr": 7, "candidates": [{}]}], ranges=1) == "candidates"


def test_tiers_select_sections_and_budget_trims_whole_rows_largest_first() -> None:
    assert rb.tier_sections("trivial", []) == ["paths", "symbols", "do_not_flag"]
    # `risk` and `rationale` ride the standard tier when the payload has them
    assert rb.tier_sections("standard", []) == [
        "paths",
        "symbols",
        "tests",
        "settings",
        "history",
        "risk",
        "do_not_flag",
        "rationale",
    ]
    assert rb.tier_sections("standard", [], present={"paths", "symbols"}) == [
        "paths",
        "symbols",
        "tests",
        "settings",
        "history",
        "do_not_flag",
    ]
    assert rb.tier_sections("full", []) == list(rb.SERVED_ORDER)
    assert rb.tier_sections("standard", ["blame", "tests", "nonsense"]) == ["tests"]
    assert rb.tier_sections("bogus", []) == rb.tier_sections("standard", [])

    big = [
        {"qualified_name": f"DB::f{i}", "path": "src/a.cpp", "callers_hop1": 10 - i}
        for i in range(10)
    ]
    small = [{"path": "src/a.cpp", "class": "src"}]
    sections = {
        "paths": rb.section(rb.SERVED, verdict="src", epistemic="exact", rows=small),
        "symbols": rb.section(rb.SERVED, verdict="touched", epistemic="lower_bound", rows=big),
        "do_not_flag": rb.section(rb.NOT_BUILT),
    }
    full_tokens = sum(s["tokens"] for s in sections.values())
    out, used = rb.apply_budget(sections, ["paths", "symbols", "do_not_flag"], full_tokens)
    assert used == full_tokens and not any(s["truncated"] for s in out.values())
    out, used = rb.apply_budget(sections, ["paths", "symbols", "do_not_flag"], 60)
    assert used <= 60
    assert out["symbols"]["truncated"] and out["symbols"]["verdict"] == "touched"
    assert out["symbols"]["rows_total"] == 10 and len(out["symbols"]["rows"]) < 10
    # rows come off the END of the largest section: the top callers survive
    assert out["symbols"]["rows"][0]["qualified_name"] == "DB::f0"
    assert out["paths"]["rows"] == small and not out["paths"]["truncated"]
    # the original payload is untouched
    assert len(sections["symbols"]["rows"]) == 10
    # a budget too small for even one row keeps every verdict
    out, used = rb.apply_budget(sections, ["paths", "symbols"], 1)
    assert out["symbols"]["rows"] == [] and out["symbols"]["verdict"] == "touched"
    assert out["paths"]["rows"] == [] and out["paths"]["verdict"] == "src"


# ---- assembly through the op seam -----------------------------------------


class _Repo:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []
        self.state = {"git_head": SHA}

    async def functions_for_paths(
        self, org: str, ns: str, paths: list[str], *, limit: int = 400, **_kw
    ) -> list[dict[str, Any]]:
        return [
            {
                "symbol_id": "s-load",
                "path": "src/Storages/MergeTree/MergeTreeData.cpp",
                "qualified_name": "DB::MergeTreeData::loadDataParts",
                "tier": "treesitter",
            },
            {
                "symbol_id": "s-load-scip",
                "path": "src/Storages/MergeTree/MergeTreeData.cpp",
                "qualified_name": "DB::MergeTreeData::loadDataParts",
                "tier": "scip",
            },
            {
                "symbol_id": "s-other",
                "path": "src/Core/Settings.cpp",
                "qualified_name": "DB::Settings::dump",
                "tier": "treesitter",
            },
        ]

    async def caller_counts(self, org: str, ns: str, ids: list[str]) -> dict[str, dict[str, int]]:
        assert set(ids) == {"s-load-scip", "s-other"}  # scip id preferred per (path, name)
        # The count comes with how many sites are registration functions
        return {"s-load-scip": {"call_sites": 7, "registration": 2}}

    async def symbol_stats(self, org: str, ns: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        # the row shape CodeRepo.symbol_stats serves (fan_in_calls /
        # fan_in_nontest); there is no `fan_in` key
        return {
            "s-load-scip": {"symbol_id": "s-load-scip", "fan_in_calls": 40, "fan_in_nontest": 31}
        }

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return self.state.get(key, "")

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.append((table, rows))


def _impact_out() -> dict[str, Any]:
    return {
        "touched_symbols": [
            {
                "qualified_name": "DB::Settings::dump",
                "path": "src/Core/Settings.cpp",
                "start_line": 90,
                "tier": "treesitter",
                "via": "span",
            },
            {
                "qualified_name": "DB::MergeTreeData::loadDataParts",
                "path": "src/Storages/MergeTree/MergeTreeData.cpp",
                "start_line": 480,
                "tier": "scip",
                "via": "header",
            },
        ],
        "by_hop_counts": {1: 7},
        "verdict": "",
        "epistemic": "lower_bound",
        "causes": ["treesitter_only"],
        "paths_seen": [
            {"path": p, "class": "test" if p.startswith("tests/") else "src"} for p in PATHS
        ],
    }


class _Calls:
    """Canned op outputs keyed by op; records every call body."""

    def __init__(self, *, fail: set[str] = frozenset(), history_fixes: int = 1) -> None:
        # one touched function has a fix PR that names it; the other only file-level fixes
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = set(fail)
        self.history_fixes = history_fixes

    async def __call__(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, body))
        if op in self.fail:
            raise RuntimeError(f"{op} down")
        assert body["org"] == ORG and body["namespace"] == NS
        if op == "code.impact":
            return _impact_out()
        if op == "code.coverage_claim":
            subject = body.get("symbol") or body.get("setting")
            covered = subject == "DB::MergeTreeData::loadDataParts"
            return {
                "verdict": "covered_by" if covered else "uncovered",
                "tests": [{"test_path": "tests/queries/0_stateless/02994_parts.sql"}]
                if covered
                else [],
                "tests_total": 3 if covered else 0,
                "epistemic": "lower_bound",
            }
        if op == "code.test_home":
            return {
                "verdict": "add_case_to",
                "home": "tests/queries/0_stateless/02994_parts.sql",
                "candidates": [{"path": "tests/queries/0_stateless/02994_parts.sql"}],
                "parts": ["stem:MergeTreeData", "setting:new_flag"],
                "next_free_stateless_number": 4000,
            }
        if op == "code.setting_facts":
            name = body["name"]
            return {
                "verdict": "known",
                "tier": "production",
                "randomized_in_ci": {"randomized": name == "max_threads", "sampler": "lambda: 1"}
                if name == "max_threads"
                else {"randomized": False},
                "history": [{"version": "26.9", "old": "false", "new": "true"}]
                if name == "new_flag"
                else [],
                "tests_total": 12,
            }
        if op == "code.symbol_history":
            named = 1 if body["name"] == "DB::MergeTreeData::loadDataParts" else 0
            return {
                "verdict": "history",
                "summary": {
                    "prs_total": 9,
                    "fix_prs": self.history_fixes,
                    "named_fix_prs": named,
                    "reverts": 0,
                    "caused_issues": [],
                    "open_issues_naming": 0,
                    "last_touched": "2026-09-01",
                },
                "tracker_namespace": "",
                "tracker_unavailable": False,
            }
        if op == "code.blame_range":
            assert body["to_sha"] == PARENT
            return {
                "verdict": "candidates",
                "epistemic": "exact",
                "latest_pr": 118000,
                "candidates": [
                    {
                        "pr_number": 118000,
                        "title": "Refactor loading",
                        "merged_at": "2026-08-30T10:00:00Z",
                        "lines_blamed": 2,
                    }
                ],
                "causes": [],
            }
        raise AssertionError(op)


async def _tracker_served(
    org: str, ns: str, pr: int, syms: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    return rb.SERVED, [{"kind": "pr", "number": pr, "line": f"PR #{pr} fixes #1", "fixes": [1]}]


async def _tracker_not_bound(
    org: str, ns: str, pr: int, syms: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    return rb.NOT_BOUND, []


def _inputs() -> rb.BriefInputs:
    return rb.BriefInputs(
        org=ORG,
        ns=NS,
        pr_number=117354,
        merge_sha=SHA,
        title="Fix wrong ILIKE results (#117354)",
        changed_paths=PATHS,
        diff=DIFF,
        parent_sha=PARENT,
        indexed_commit=SHA,
        path_prefixes=("src/", "tests/"),
    )


def _fixed_now() -> datetime:
    return datetime(2026, 9, 15, 20, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_build_assembles_every_section_with_verdicts_and_decisions() -> None:
    calls = _Calls()
    payload = await review_brief_build.build_brief(
        _inputs(), call=calls, repo=_Repo(), tracker_rows=_tracker_served, now=_fixed_now
    )
    secs = payload["sections"]
    assert list(secs) == list(rb.SECTION_ORDER)
    for name in rb.SECTION_ORDER:
        assert {
            "status",
            "verdict",
            "epistemic",
            "causes",
            "rows",
            "rows_total",
            "tokens",
            "truncated",
        } <= set(secs[name])
    # paths: the classes from impact's paths_seen; not test-only
    assert secs["paths"]["verdict"] == "src" and secs["paths"]["epistemic"] == "exact"
    # symbols: most-called first, decorated with hop-1 call sites
    rows = secs["symbols"]["rows"]
    assert [r["qualified_name"] for r in rows] == [
        "DB::MergeTreeData::loadDataParts",
        "DB::Settings::dump",
    ]
    assert rows[0]["callers_hop1"] == 7 and rows[1]["callers_hop1"] == 0
    # The registration share rides only where there is one
    assert rows[0]["callers_registration"] == 2 and "callers_registration" not in rows[1]
    assert rows[0]["fan_in"] == 40 and rows[0]["fan_in_nontest"] == 31 and rows[1]["fan_in"] == 0
    assert secs["symbols"]["verdict"] == "touched" and secs["symbols"]["causes"] == [
        "treesitter_only"
    ]
    # tests: a claim per symbol and per setting, then the home; Settings::dump is uncovered
    kinds = [(r["kind"], r["subject"]) for r in secs["tests"]["rows"]]
    assert ("symbol", "DB::MergeTreeData::loadDataParts") in kinds and (
        "setting",
        "new_flag",
    ) in kinds
    assert kinds[-1][0] == "home" and secs["tests"]["rows"][-1]["home"].endswith("02994_parts.sql")
    assert secs["tests"]["verdict"] == "uncovered"
    # settings: the three names the diff touched; max_threads randomized in CI decides
    names = {r["name"]: r for r in secs["settings"]["rows"]}
    assert set(names) == {"old_flag", "new_flag", "max_threads"}
    assert names["new_flag"]["last_change"] == "26.9" and names["new_flag"]["history_entries"] == 1
    assert (
        names["max_threads"]["randomized_in_ci"] is True
        and names["max_threads"]["source"] == "test_uses"
    )
    assert secs["settings"]["verdict"] == "randomized"
    # history: one row per touched function, the one with function-level
    # evidence first; a fix PR naming a touched function = regressions
    assert secs["history"]["verdict"] == "regressions"
    assert secs["history"]["rows"][0]["qualified_name"] == "DB::MergeTreeData::loadDataParts"
    assert secs["history"]["rows"][0]["named_fix_prs"] == 1
    assert {r["qualified_name"] for r in secs["history"]["rows"]} == {
        "DB::MergeTreeData::loadDataParts",
        "DB::Settings::dump",
    }
    # blame: the deleted runs at the parent, largest first; exact when blame ran
    assert [(r["path"], r["start"], r["end"]) for r in secs["blame"]["rows"]] == [
        ("src/Storages/MergeTree/MergeTreeData.cpp", 502, 503),
        ("src/Core/Settings.cpp", 101, 101),
    ]
    assert secs["blame"]["verdict"] == "candidates" and secs["blame"]["epistemic"] == "exact"
    assert secs["blame"]["rows"][0]["candidates"][0]["merged_at"] == "2026-08-30"
    # qa served through the pack's shared reader; do_not_flag reserved
    assert secs["tracker"]["status"] == rb.SERVED and secs["tracker"]["verdict"] == "record"
    assert secs["do_not_flag"]["status"] == rb.NOT_BUILT and secs["do_not_flag"]["rows"] == []
    assert payload["decisions"] == {
        "test_only": False,
        "symbol_with_regressions": True,
        "uncovered_symbol": True,
        "randomized_setting": True,
        "deleted_lines_blamed": True,
    }
    assert payload["built_at"] == "2026-09-15T20:00:00Z" and payload["indexed_commit"] == SHA
    # the fan-outs are bounded and go through the ops, never a second impact
    ops = [op for op, _ in calls.calls]
    assert ops.count("code.impact") == 1 and ops.count("code.test_home") == 1
    assert ops.count("code.coverage_claim") == 2 + 3 and ops.count("code.setting_facts") == 3
    assert ops.count("code.symbol_history") == 2 and ops.count("code.blame_range") == 2
    impact_body = calls.calls[0][1]
    assert (
        impact_body["depth"] == 1 and impact_body["files"] == PATHS and impact_body["diff"] == DIFF
    )


@pytest.mark.asyncio
async def test_two_builds_serialise_byte_identical_and_a_row_carries_every_column() -> None:
    a = await review_brief_build.build_brief(
        _inputs(), call=_Calls(), repo=_Repo(), tracker_rows=_tracker_not_bound, now=_fixed_now
    )
    b = await review_brief_build.build_brief(
        _inputs(), call=_Calls(), repo=_Repo(), tracker_rows=_tracker_not_bound, now=_fixed_now
    )
    a["build_ms"] = b["build_ms"] = 0  # wall time is the one non-deterministic field
    assert rb.canonical(a) == rb.canonical(b)
    assert rb.canonical(a) == json.dumps(
        a, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert (
        a["sections"]["tracker"]["status"] == rb.NOT_BOUND
        and a["sections"]["tracker"]["verdict"] == "tracker_unavailable"
    )
    row = review_brief_build.brief_row(ORG, NS, a)
    from server.code.store.index import CODE_REVIEW_BRIEF_COLS

    assert set(row) == set(CODE_REVIEW_BRIEF_COLS)
    assert row["pr_number"] == 117354 and row["merge_sha"] == SHA and row["indexed_commit"] == SHA
    assert json.loads(row["payload"]) == a
    # built (served or empty) sections are listed; not_bound qa and not_built do_not_flag are not
    # (an empty `risk` — no stored fit — is built, and listed)
    assert row["sections"] == ["paths", "symbols", "tests", "settings", "history", "blame", "risk"]
    assert row["tokens"] == sum(s["tokens"] for s in a["sections"].values()) and row["tokens"] > 0


@pytest.mark.asyncio
async def test_a_failing_op_fails_its_section_open_and_the_rest_stand() -> None:
    calls = _Calls(fail={"code.symbol_history", "code.blame_range"})
    payload = await review_brief_build.build_brief(
        _inputs(), call=calls, repo=_Repo(), tracker_rows=_tracker_not_bound, now=_fixed_now
    )
    secs = payload["sections"]
    assert secs["history"]["rows"] == [] and secs["history"]["verdict"] == "no_symbols"
    assert secs["history"]["causes"] == ["symbol_history:RuntimeError"] * 2
    assert secs["blame"]["verdict"] == "unknown" and secs["blame"]["epistemic"] == "lower_bound"
    assert secs["symbols"]["verdict"] == "touched" and secs["tests"]["verdict"] == "uncovered"
    assert payload["decisions"]["symbol_with_regressions"] is False
    # impact itself down: paths classified locally, symbols failed, the rest empty but present
    payload = await review_brief_build.build_brief(
        _inputs(),
        call=_Calls(fail={"code.impact"}),
        repo=_Repo(),
        tracker_rows=_tracker_not_bound,
        now=_fixed_now,
    )
    secs = payload["sections"]
    assert secs["symbols"]["status"] == rb.FAILED and secs["symbols"]["causes"] == [
        "code.impact:RuntimeError"
    ]
    assert secs["paths"]["status"] == rb.SERVED and {r["class"] for r in secs["paths"]["rows"]} == {
        "src",
        "test",
    }
    assert secs["history"]["verdict"] == "no_symbols"
    assert secs["settings"]["verdict"] == "randomized"  # settings come off the diff, not off impact


@pytest.mark.asyncio
async def test_serve_applies_tier_sections_and_budget_to_a_stored_payload() -> None:
    payload = await review_brief_build.build_brief(
        _inputs(), call=_Calls(), repo=_Repo(), tracker_rows=_tracker_served, now=_fixed_now
    )
    secs, used, budget = rb.serve(payload, tier="trivial", requested=[])
    assert list(secs) == ["paths", "symbols", "do_not_flag"] and budget == 700 and used <= 700
    secs, used, budget = rb.serve(payload, tier="full", requested=[])
    assert list(secs) == list(rb.SECTION_ORDER) and budget == 4000
    secs, used, budget = rb.serve(payload, tier="full", requested=["blame"], budget=1)
    assert list(secs) == ["blame"] and secs["blame"]["rows"] == [] and secs["blame"]["truncated"]
    assert secs["blame"]["verdict"] == "candidates" and secs["blame"]["rows_total"] == 2
    # standard never carries blame or qa, whatever is requested
    secs, _, _ = rb.serve(payload, tier="standard", requested=["blame", "qa", "paths"])
    assert list(secs) == ["paths"]


# ---- handler: point read, miss-then-build --------------------------------


class _HandlerRepo:
    def __init__(self, stored: dict[str, Any] | None) -> None:
        self.stored = stored
        self.reads: list[tuple[int, str]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def review_brief(
        self, org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
    ) -> dict[str, Any] | None:
        self.reads.append((pr_number, merge_sha))
        return self.stored

    async def pr_merge_facts(
        self, org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
    ) -> dict[str, Any] | None:
        if pr_number != 117354:
            return None
        return {
            "pr_number": 117354,
            "title": "Fix wrong ILIKE results",
            "files": PATHS,
            "merge_sha": SHA.encode("ascii"),
            "merged_at": "2026-09-15 17:05:43",
        }

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.append((table, rows))


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"repo": None, "built": []}
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "resolve_code_identity", lambda o, n: (ORG, NS))
    monkeypatch.setattr(
        h, "schedule_code_trace", lambda **kw: state.setdefault("traces", []).append(kw)
    )
    monkeypatch.setattr(h, "set_code_span_attributes", lambda **kw: None)
    monkeypatch.setattr(h, "CodeRepo", lambda: state["repo"])
    import server.code.domain.index.repos as repos_mod

    class _Cfg:
        git_dir = "/nonexistent/mirror.git"
        git_branch = "master"
        path_prefixes = ("src/", "tests/")

    monkeypatch.setattr(repos_mod, "repo_for_namespace", lambda o, n: _Cfg())

    async def _no_mirror(*a: Any, **kw: Any) -> None:
        return None  # the mirror fallback is its own test; here the ledger decides

    monkeypatch.setattr(review_brief_sources, "mirror_merge_facts", _no_mirror)
    monkeypatch.setattr(review_brief_sources, "mirror_commit_facts", _no_mirror)

    async def _build_for_merge(repo: Any, org: str, ns: str, **kw: Any) -> dict[str, Any]:
        state["built"].append(kw)
        payload = await review_brief_build.build_brief(
            rb.BriefInputs(
                org=org,
                ns=ns,
                pr_number=kw["pr_number"],
                merge_sha=kw["merge_sha"],
                title=kw["title"],
                changed_paths=kw["changed_paths"],
                diff=DIFF,
                parent_sha=PARENT,
                indexed_commit=SHA,
                path_prefixes=kw["path_prefixes"],
            ),
            call=_Calls(),
            repo=_Repo(),
            tracker_rows=_tracker_not_bound,
            now=_fixed_now,
        )
        await repo.insert_rows(
            "code_review_briefs", [review_brief_build.brief_row(org, ns, payload)]
        )
        return payload

    monkeypatch.setattr(review_brief_build, "build_for_merge", _build_for_merge)
    return state


@pytest.mark.asyncio
async def test_miss_builds_once_and_stores_then_the_next_read_is_a_point(
    wired: dict[str, Any],
) -> None:
    repo = _HandlerRepo(stored=None)
    wired["repo"] = repo
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354, tier="full"))
    assert out.source == "built" and out.pr_number == 117354 and out.merge_sha == SHA
    assert wired["built"][0]["merge_sha"] == SHA and wired["built"][0]["path_prefixes"] == (
        "src/",
        "tests/",
    )
    assert wired["built"][0]["changed_paths"] == PATHS
    assert [t for t, _ in repo.inserted] == ["code_review_briefs"]
    assert out.decisions["randomized_setting"] is True and out.token_budget == 4000
    assert set(out.sections) == set(rb.SERVED_ORDER) and out.tokens_used <= 4000
    assert wired["traces"][-1]["route"] == "brief_built" and wired["traces"][-1]["abstain"] is False
    # the row it stored is what the next read serves, without a build
    stored_row = repo.inserted[0][1][0]
    repo2 = _HandlerRepo(stored={**stored_row})
    wired["repo"] = repo2
    wired["built"].clear()
    out2 = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354, tier="trivial"))
    assert out2.source == "precomputed" and wired["built"] == [] and repo2.inserted == []
    assert list(out2.sections) == ["paths", "symbols", "do_not_flag"] and out2.token_budget == 700
    assert out2.built_at == "2026-09-15T20:00:00Z" and out2.indexed_commit == SHA
    assert repo2.reads == [(117354, "")]
    # a merge_sha-only read passes the sha through, lower-cased
    await h.handle_code_review_brief(CodeReviewBriefInput(merge_sha=SHA.upper()))
    assert repo2.reads[-1] == (0, SHA)


@pytest.mark.asyncio
async def test_rationale_is_read_on_every_call_and_never_stored(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import code_plane_reads

    reads: list[tuple[str, list[str]]] = []
    diffs: list[int] = []
    rule = "Keep `new_flag` off in the history file: CI randomizes it. Applies to `DB::Settings`."

    async def _decisions(org: str, trunk: str, paths: Any, *, limit: int, **kw: Any) -> Any:
        reads.append((trunk, list(paths)))
        row = {
            "memory_key": "design/new-flag",
            "tags": ["decision", "constraint"],
            "pinned": False,
            "code_state": "fresh",
            "line": "- design/new-flag [constraint, decision]: Keep `new_flag` off …",
            "text": rule,
        }
        return code_plane_reads.DecisionRows([row], [])

    async def _diff(org: str, ns: str, payload: Any, **kw: Any) -> tuple[str, bool]:
        diffs.append(kw["pr_number"])
        return DIFF, False

    from server.code.app import memory_links

    monkeypatch.setattr(memory_links, "trunk_namespace", lambda org, ns: ns)
    monkeypatch.setattr(code_plane_reads, "decisions_for_paths", _decisions)
    monkeypatch.setattr(h, "_pr_diff", _diff)
    repo = _HandlerRepo(stored=None)
    wired["repo"] = repo
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354))
    sec = out.sections["rationale"]
    assert sec["verdict"] == "check" and sec["rows"][0]["names"] == ["new_flag"]
    # the stored payload carries no rationale
    assert reads == [(NS, PATHS)] and diffs == [117354]
    stored_row = repo.inserted[0][1][0]
    assert "rationale" not in json.loads(stored_row["payload"])["sections"]
    assert "rationale" not in stored_row["sections"]
    # the next read is a point read that reads the decisions again
    wired["repo"] = _HandlerRepo(stored={**stored_row})
    out2 = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354))
    assert out2.source == "precomputed" and out2.sections["rationale"]["verdict"] == "check"
    assert len(reads) == 2
    # a tier without the section reads nothing for it
    await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354, tier="trivial"))
    await h.handle_code_review_brief(
        CodeReviewBriefInput(pr_number=117354, sections=["paths"], tier="full")
    )
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_rationale_reads_no_diff_when_no_row_names_anything_to_check(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app import memory_links
    from server.memory.app import code_plane_reads

    async def _decisions(org: str, trunk: str, paths: Any, *, limit: int, **kw: Any) -> Any:
        row = {"memory_key": "design/plain", "tags": ["decision"], "pinned": False}
        plain = row | {"code_state": "", "line": "- design/plain: Decided Z.", "text": "Decided Z."}
        return code_plane_reads.DecisionRows([plain], [])

    async def _diff(*a: Any, **kw: Any) -> tuple[str, bool]:
        raise AssertionError("read a diff with nothing to check")

    monkeypatch.setattr(memory_links, "trunk_namespace", lambda org, ns: ns)
    monkeypatch.setattr(code_plane_reads, "decisions_for_paths", _decisions)
    monkeypatch.setattr(h, "_pr_diff", _diff)
    wired["repo"] = _HandlerRepo(stored=None)
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354))
    sec = out.sections["rationale"]
    assert sec["verdict"] == "none" and sec["rows"][0]["memory_key"] == "design/plain"


@pytest.mark.asyncio
async def test_rationale_reads_source_paths_first_and_the_rows_its_settings_allow(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app import memory_links
    from server.memory.app import code_plane_reads
    from server.settings import settings

    calls: list[tuple[list[str], int]] = []

    async def _decisions(org: str, trunk: str, paths: Any, *, limit: int, **kw: Any) -> Any:
        calls.append((list(paths), limit))
        return code_plane_reads.DecisionRows([], [])

    monkeypatch.setattr(memory_links, "trunk_namespace", lambda org, ns: ns)
    monkeypatch.setattr(code_plane_reads, "decisions_for_paths", _decisions)
    monkeypatch.setattr(settings, "code_review_brief_rationale_paths", 1)
    monkeypatch.setattr(settings, "code_review_brief_rationale_rows", 3)
    # a stored brief whose changed paths list the test file first
    test_path, src_path = "tests/queries/0_stateless/03999_x.sql", "src/Core/Settings.cpp"
    paths = [{"path": test_path, "class": "test"}, {"path": src_path, "class": "src"}]
    payload = {
        "pr_number": 117354,
        "merge_sha": SHA,
        "sections": {"paths": rb.section(rb.SERVED, verdict="src", rows=paths)},
    }
    wired["repo"] = _HandlerRepo(stored={"payload": json.dumps(payload)})
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354))
    assert out.source == "precomputed"
    # the source path is read; the test path is the one left out
    assert calls == [([src_path], 3)]
    sec = out.sections["rationale"]
    # a changed path left unread keeps the section from `none` or `clear`
    assert sec["verdict"] == "unknown" and sec["causes"] == ["paths_not_read:1"]


@pytest.mark.asyncio
async def test_rationale_serves_whole_rows_up_to_its_token_setting(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app import memory_links
    from server.memory.app import code_plane_reads
    from server.settings import settings

    rows = [
        {
            "memory_key": f"design/d{i}",
            "tags": ["decision"],
            "pinned": False,
            "code_state": "",
            "line": "- " + "x" * 300,
            "text": "Decided Z.",
        }
        for i in range(3)
    ]

    async def _decisions(org: str, trunk: str, paths: Any, *, limit: int, **kw: Any) -> Any:
        return code_plane_reads.DecisionRows(rows, [])

    monkeypatch.setattr(memory_links, "trunk_namespace", lambda org, ns: ns)
    monkeypatch.setattr(code_plane_reads, "decisions_for_paths", _decisions)
    monkeypatch.setattr(settings, "code_review_brief_rationale_tokens", 50)
    wired["repo"] = _HandlerRepo(stored=None)
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354))
    sec = out.sections["rationale"]
    # the first row always; the second would pass 50 tokens
    assert len(sec["rows"]) == 1 and sec["rows_total"] == 3 and sec["truncated"]


@pytest.mark.asyncio
async def test_rationale_is_unknown_when_the_trunk_is_refused(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app import memory_links
    from server.memory.app import code_plane_reads

    async def _refused(org: str, trunk: str, paths: Any, *, limit: int, **kw: Any) -> Any:
        return code_plane_reads.DecisionRows([], [], refused=True)

    monkeypatch.setattr(memory_links, "trunk_namespace", lambda org, ns: ns)
    monkeypatch.setattr(code_plane_reads, "decisions_for_paths", _refused)
    wired["repo"] = _HandlerRepo(stored=None)
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354))
    sec = out.sections["rationale"]
    assert sec["verdict"] == "unknown" and sec["causes"] == ["trunk_not_readable"]
    assert sec["rows"] == []


@pytest.mark.asyncio
async def test_rationale_fails_open_to_a_failed_section(
    wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import code_plane_reads

    async def _boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("down")

    from server.code.app import memory_links

    monkeypatch.setattr(memory_links, "trunk_namespace", lambda org, ns: ns)
    monkeypatch.setattr(code_plane_reads, "decisions_for_paths", _boom)
    wired["repo"] = _HandlerRepo(stored=None)
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=117354))
    assert out.sections["rationale"]["status"] == rb.FAILED
    assert out.sections["rationale"]["causes"] == ["decisions_for_paths:RuntimeError"]
    assert out.sections["paths"]["status"] == rb.SERVED


@pytest.mark.asyncio
async def test_unknown_pr_is_missing_not_an_error(wired: dict[str, Any]) -> None:
    repo = _HandlerRepo(stored=None)
    wired["repo"] = repo
    out = await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=1))
    assert out.source == "missing" and out.sections == {} and out.decisions == {}
    assert repo.inserted == [] and wired["built"] == []
    assert wired["traces"][-1]["abstain"] is True and wired["traces"][-1]["verdict"] == "no_brief"


def test_input_validates_names_and_the_key_rule_is_the_handlers() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CodeReviewBriefInput(pr_number=1, sections=["impact"])
    with pytest.raises(ValidationError):
        CodeReviewBriefInput(pr_number=1, tier="deep")
    with pytest.raises(ValidationError):
        CodeReviewBriefInput(merge_sha="not-a-sha")
    assert CodeReviewBriefInput(pr_number=1).tier == "standard"
    # a keyless body parses: the namespace gate must answer a refused
    # principal before any shape complaint (integration leak suite posts
    # pr_number=0 and no merge_sha at every code read op)
    assert CodeReviewBriefInput(pr_number=0).merge_sha == ""


@pytest.mark.asyncio
async def test_keyless_request_is_a_validation_error_after_the_gate(wired: dict[str, Any]) -> None:
    from server.core.errors import ValidationError

    repo = _HandlerRepo(stored=None)
    wired["repo"] = repo
    with pytest.raises(ValidationError):
        await h.handle_code_review_brief(CodeReviewBriefInput(pr_number=0))
    assert repo.reads == [] and repo.inserted == [] and wired["built"] == []


# ---- ingest: the brief rides the pack's trigger ---------------------------


@pytest.mark.asyncio
async def test_ingest_builds_the_brief_after_the_pack_and_a_failure_is_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace as NS_

    import server.code.app.index.ingest_pass as ci
    from server.code.app.index import pass_budget as ingest_budget
    from server.code.app.index import pass_commit as ingest_commit

    monkeypatch.setattr(ci.settings, "code_ingest_dependents_enabled", False)
    monkeypatch.setattr(ci.settings, "code_closure_repair_enabled", False)
    monkeypatch.setattr(ci.settings, "code_ingest_max_files_per_commit", 100)
    monkeypatch.setattr(ci.settings, "code_chunk_max_chars", 3000)

    class _Git:
        async def diff_files(self, git_dir: str, base: str, head: str) -> list[Any]:
            return [NS_(status="M", path="src/a.cpp")]

        async def show_file(self, git_dir: str, sha: str, path: str) -> bytes:
            return b"int f() { return 1; }"

        async def commit_subject(self, git_dir: str, sha: str) -> str:
            return "Fix a thing (#117354)"

    stats = NS_(
        excluded=False,
        skipped=False,
        error="",
        chunks_new=1,
        chunks_kept=0,
        embedded=0,
        embed_stubs=0,
        symbols_new=1,
        edges_new=0,
        described=0,
        symbol_summaries=[],
    )

    async def ingest_file(*a: Any, **k: Any) -> Any:
        return stats

    async def build_pack(*a: Any, **k: Any) -> str:
        return "# pack"

    class _Repo2:
        def __init__(self) -> None:
            self.tables: list[str] = []

        async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
            self.tables.append(table)

    built: list[dict[str, Any]] = []

    async def build_brief(repo: Any, org: str, ns: str, **kw: Any) -> None:
        built.append(kw)
        if kw["pr_number"] == 117354 and len(built) == 2:
            raise RuntimeError("brief down")

    from server.code.store.index import build_row

    result = ingest_budget.CodeIngestResult()
    for _ in range(2):
        repo = _Repo2()
        await ingest_commit._ingest_commit(
            repo,
            ORG,
            NS,
            "/mirror.git",
            PARENT,
            SHA,
            embedder=NS_(model_name="m"),
            llm=None,
            describe=False,
            prefixes=("src/",),
            result=result,
            build_pack=build_pack,
            estimate_tokens=lambda t: 1,
            build_row=build_row,
            gitrepo=_Git(),
            ingest_file=ingest_file,
            tombstone_file=None,
            build_brief=build_brief,
        )
        assert repo.tables == ["code_context_packs"]  # the brief's insert is the builder's
    assert result.packs_built == 2 and result.briefs_built == 1 and result.briefs_failed == 1
    assert built[0]["pr_number"] == 117354 and built[0]["merge_sha"] == SHA
    assert built[0]["parent_sha"] == PARENT and built[0]["changed_paths"] == ["src/a.cpp"]
    assert built[0]["title"] == "Fix a thing (#117354)" and built[0]["git_dir"] == "/mirror.git"


@pytest.mark.asyncio
async def test_brief_builder_reads_through_the_handler_op_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The service holds no handler imports: the worker hands it the handler-owned seam."""
    from types import SimpleNamespace as NS_

    from server.code.app.index import pass_stream as ingest_stream

    seen: dict[str, Any] = {}

    async def _build_for_merge(repo: Any, org: str, ns: str, **kw: Any) -> dict[str, Any]:
        seen.update(kw)
        return {}

    monkeypatch.setattr(review_brief_build, "build_for_merge", _build_for_merge)
    build = ingest_stream._brief_builder(NS_(path_prefixes=("src/",)))
    await build(object(), ORG, NS, pr_number=1)
    assert seen["call"] is h.call_code_op and seen["path_prefixes"] == ("src/",)


def test_review_brief_builder_imports_no_handler_or_operation() -> None:
    import ast
    import inspect

    names: list[str] = []
    for mod in (rb, review_brief_build, review_brief_sources):
        tree = ast.parse(inspect.getsource(mod))
        names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not [
        m
        for m in names
        if m.startswith(("server.code.app.review_brief", "server.code.api", "server.operations"))
    ]
