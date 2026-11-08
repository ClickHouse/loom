"""code.conventions: the repository's convention checks as
facts with evidence — each rule fires on a positive control, stays quiet on
its satisfied twin, and says `unknown` when a read failed instead of
claiming a clean bill."""

from __future__ import annotations

import pytest

import server.code.app.conventions as h
from server.code.api.models_conventions import CodeConventionsInput
from server.code.domain.index import conventions as cv
from server.code.domain.index import conventions_rules
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("mirror_here", "scalar_code_repo")

ORG, NS = "clickhouse", "code-clickhouse"

SETTINGS_DIFF = """diff --git a/src/Core/Settings.cpp b/src/Core/Settings.cpp
index 1..2 100644
--- a/src/Core/Settings.cpp
+++ b/src/Core/Settings.cpp
@@ -1943,0 +1944,3 @@
+    DECLARE(Bool, use_new_thing, false, R"(
+Use the new thing.
+)", 0) \\
@@ -2001 +2004 @@
-    DECLARE(UInt64, max_widgets, 10, R"(
+    DECLARE(UInt64, max_widgets, 20, R"(
@@ -2100 +2103 @@
-    DECLARE(Bool, keep_same, true, R"(Old words.)", 0) \\
+    DECLARE(Bool, keep_same, true, R"(New words.)", 0) \\
"""
HISTORY_DIFF = """diff --git a/src/Core/SettingsChangesHistory.cpp b/src/Core/SettingsChangesHistory.cpp
--- a/src/Core/SettingsChangesHistory.cpp
+++ b/src/Core/SettingsChangesHistory.cpp
@@ -30,0 +31,2 @@
+            {"use_new_thing", false, false, "New setting"},
+            {"max_widgets", 10, 20, "More widgets"},
"""
ERROR_DIFF = """diff --git a/src/Storages/Foo.cpp b/src/Storages/Foo.cpp
--- a/src/Storages/Foo.cpp
+++ b/src/Storages/Foo.cpp
@@ -40,0 +41 @@
+        throw Exception(ErrorCodes::BAD_ARGUMENTS, "no");
@@ -80,0 +82 @@
+        throw Exception(ErrorCodes::NEW_CODE, "no");  // ErrorCodes::values is API
@@ -90,0 +93 @@
+    // ErrorCodes::COMMENTED_OUT
diff --git a/src/Storages/Bar.cpp b/src/Storages/Bar.cpp
--- a/src/Storages/Bar.cpp
+++ b/src/Storages/Bar.cpp
@@ -10,0 +11 @@
+    extern const int ADDED_HERE;
@@ -50,0 +52 @@
+        throw Exception(ErrorCodes::ADDED_HERE, "x");
"""
TEST_FILES = [
    "src/Interpreters/A.cpp",
    "tests/queries/0_stateless/04652_new_case.sql",
    "tests/queries/0_stateless/04652_new_case.reference",
    "tests/queries/0_stateless/04652_no_ref.sh",
]
TEST_STATUSES = {p: ("M" if p.startswith("src/") else "A") for p in TEST_FILES}


# ---- the diff ----------------------------------------------------------------


def test_unified_diff_parser_numbers_added_and_removed_lines() -> None:
    d = cv.parse_unified_diff(SETTINGS_DIFF + ERROR_DIFF)
    s = d.files["src/Core/Settings.cpp"]
    assert s.status == "M" and s.added[0] == (1944, '    DECLARE(Bool, use_new_thing, false, R"(')
    assert [ln for ln, _ in s.removed] == [2001, 2100]
    foo = d.files["src/Storages/Foo.cpp"]
    assert [ln for ln, _ in foo.added] == [41, 82, 93]
    new = cv.parse_unified_diff(
        "diff --git a/x b/x\nnew file mode 100644\n--- /dev/null\n+++ b/x\n@@ -0,0 +1 @@\n+hi\n"
    )
    assert new.files["x"].status == "A" and new.files["x"].added == [(1, "hi")]
    assert cv.parse_unified_diff("garbage\n").files == {}


def test_stateless_stem_strips_the_extension_chain() -> None:
    assert cv.stem_of_stateless("tests/queries/0_stateless/04652_x.sql.j2") == ("04652", "04652_x")
    assert cv.stem_of_stateless("tests/queries/0_stateless/04652_x.reference.j2") == (
        "04652",
        "04652_x",
    )
    assert cv.stem_of_stateless("tests/queries/0_stateless/04652_x.reference") == (
        "04652",
        "04652_x",
    )
    assert cv.stem_of_stateless("tests/queries/1_stateful/00001_x.sql") is None


# ---- rules 1, 2 ---------------------------------------------------------------


def test_setting_history_fires_on_new_and_redefaulted_not_on_description_edits() -> None:
    d = cv.parse_unified_diff(SETTINGS_DIFF)
    changes = cv.setting_changes(d)
    assert [(c.name, c.kind) for c in changes] == [
        ("use_new_thing", "new"),
        ("max_widgets", "default_changed"),
    ]
    assert changes[1].old_default == "10" and changes[1].new_default == "20"
    r = cv.rule_setting_history(changes, d, history_chunks=[])
    assert r.verdict == cv.VIOLATED and r.epistemic == cv.EXACT
    assert [e["setting"] for e in r.evidence] == ["use_new_thing", "max_widgets"]
    assert r.evidence[0]["path"] == "src/Core/Settings.cpp" and r.evidence[0]["line"] == 1944
    # the entry in the same diff satisfies it
    d2 = cv.parse_unified_diff(SETTINGS_DIFF + HISTORY_DIFF)
    r2 = cv.rule_setting_history(cv.setting_changes(d2), d2, history_chunks=[])
    assert r2.verdict == cv.SATISFIED and {e["where"] for e in r2.evidence} == {"diff"}
    # an entry already in the indexed history file satisfies it too, with its release
    chunks = [
        {
            "start_line": 28,
            "content": 'addSettingsChanges(settings_changes_history, "26.9",\n'
            '{"use_new_thing", false, false, "New setting"},\n'
            '{"max_widgets", 10, 20, "More"},\n',
        }
    ]
    r3 = cv.rule_setting_history(changes, d, history_chunks=chunks)
    assert r3.verdict == cv.SATISFIED and r3.evidence[0]["release"] == "26.9"
    # unread history: unknown, never violated
    assert cv.rule_setting_history(changes, d, history_chunks=None).verdict == cv.UNKNOWN
    # a default re-spelt as a named constant: still a textual change, but the value may hold
    const = cv.parse_unified_diff(
        SETTINGS_DIFF.replace("max_widgets, 20,", "max_widgets, default_widgets,").replace(
            "+    DECLARE(Bool, use_new_thing", "     DECLARE(Bool, use_new_thing"
        )
    )
    ch = [c for c in cv.setting_changes(const) if c.name == "max_widgets"]
    soft = cv.rule_setting_history(ch, const, history_chunks=[])
    assert (
        soft.verdict == cv.VIOLATED
        and soft.epistemic == cv.LOWER_BOUND
        and "expression" in soft.note
    )
    # server settings have no history
    srv = cv.parse_unified_diff(
        SETTINGS_DIFF.replace("src/Core/Settings.cpp", "src/Core/ServerSettings.cpp")
    )
    assert cv.rule_setting_history(cv.setting_changes(srv), srv, []).verdict == cv.NOT_APPLICABLE
    assert cv.rule_setting_history([], d, []).verdict == cv.NOT_APPLICABLE


def test_setting_docs_is_not_applicable_for_generated_pages_and_satisfied_by_a_mention() -> None:
    d = cv.parse_unified_diff(SETTINGS_DIFF)
    changes = cv.setting_changes(d)
    none = cv.rule_setting_docs(changes, d, {"use_new_thing": [], "max_widgets": []})
    assert none.verdict == cv.NOT_APPLICABLE and all(
        e["where"] == "no_docs_mention" for e in none.evidence
    )
    gen = cv.rule_setting_docs(
        changes,
        d,
        {
            "use_new_thing": ["docs/reference/settings/session-settings/use-new.mdx"],
            "max_widgets": [
                "docs/guides/widgets.mdx",
                "docs/reference/settings/session-settings/max.mdx",
            ],
        },
    )
    assert gen.verdict == cv.SATISFIED
    assert (
        gen.evidence[0]["generated"] is True
        and gen.evidence[1]["path"] == "docs/guides/widgets.mdx"
    )
    assert (
        cv.rule_setting_docs(changes, d, {"use_new_thing": None, "max_widgets": []}).verdict
        == cv.UNKNOWN
    )
    # a name past the per-call grep cap is "not checked", not "unread": the verdict rests on the checked ones
    capped = cv.rule_setting_docs(changes, d, {"use_new_thing": []})
    assert capped.verdict == cv.NOT_APPLICABLE and "1 of 2 name(s) not checked" in capped.note
    assert capped.evidence[1]["where"] == "not_checked"
    with_docs = cv.parse_unified_diff(
        SETTINGS_DIFF + "diff --git a/docs/guides/x.md b/docs/guides/x.md\n"
        "--- a/docs/guides/x.md\n+++ b/docs/guides/x.md\n@@ -1,0 +2 @@\n+`use_new_thing` and `max_widgets`\n"
    )
    r = cv.rule_setting_docs(cv.setting_changes(with_docs), with_docs, {})
    assert (
        r.verdict == cv.SATISFIED
        and r.evidence[0]["where"] == "diff"
        and r.evidence[0]["line"] == 2
    )


# ---- rule 3 -------------------------------------------------------------------


def test_error_code_uses_skip_comments_and_api_names_and_the_registry() -> None:
    d = cv.parse_unified_diff(ERROR_DIFF)
    uses = cv.error_code_uses(d)
    assert [(u.path, u.name, u.line) for u in uses] == [
        ("src/Storages/Foo.cpp", "BAD_ARGUMENTS", 41),
        ("src/Storages/Foo.cpp", "NEW_CODE", 82),
        ("src/Storages/Bar.cpp", "ADDED_HERE", 52),
    ]
    declared = {
        ("src/Storages/Foo.cpp", "BAD_ARGUMENTS"): True,
        ("src/Storages/Foo.cpp", "NEW_CODE"): False,
    }
    r = cv.rule_error_code_declared(uses, d, declared)
    assert (
        r.verdict == cv.VIOLATED
        and r.evidence[0]["error_code"] == "NEW_CODE"
        and r.evidence[0]["line"] == 82
    )
    assert {e["where"] for e in r.evidence[1:]} == {"file", "diff"}
    declared[("src/Storages/Foo.cpp", "NEW_CODE")] = True
    assert cv.rule_error_code_declared(uses, d, declared).verdict == cv.SATISFIED
    declared[("src/Storages/Foo.cpp", "NEW_CODE")] = None
    assert cv.rule_error_code_declared(uses, d, declared).verdict == cv.UNKNOWN
    assert cv.rule_error_code_declared([], d, {}).verdict == cv.NOT_APPLICABLE


# ---- rules 4, 5 ---------------------------------------------------------------


def test_bugfix_signal_and_the_test_rules() -> None:
    assert conventions_rules.bugfix_signal("Fix crash", ["pr-bugfix"], "") == "label"
    assert (
        conventions_rules.bugfix_signal("Better", [], "- Bug Fix (user-visible misbehavior)\n")
        == "changelog_bullet"
    )
    assert (
        conventions_rules.bugfix_signal("Fix typo", [], "- Improvement\n") == ""
    )  # a declared category wins over the title
    assert conventions_rules.bugfix_signal("Fix the thing", [], "") == "title"
    assert conventions_rules.category_of(["comp-x", "pr-performance"], "") == (
        "performance",
        "label:pr-performance",
    )
    assert (
        conventions_rules.category_of(
            [], "### Changelog category\n- Not for changelog (changelog entry is not required)\n"
        )[0]
        == "not_for_changelog"
    )

    files = ["src/A.cpp", "src/B.h"]
    r = conventions_rules.rule_bugfix_has_test(files, {}, "Fix it", ["pr-bugfix"], "")
    assert (
        r.verdict == cv.VIOLATED and r.epistemic == cv.EXACT and r.evidence[0]["signal"] == "label"
    )
    t = conventions_rules.rule_bugfix_has_test(files, {}, "Fix it", [], "")
    assert t.verdict == cv.VIOLATED and t.epistemic == cv.LOWER_BOUND
    ok = conventions_rules.rule_bugfix_has_test(
        files + ["src/Functions/tests/gtest_a.cpp"], {}, "Fix", ["pr-bugfix"], ""
    )
    assert ok.verdict == cv.SATISFIED
    assert (
        conventions_rules.rule_bugfix_has_test(files, {}, "Add feature", ["pr-feature"], "").verdict
        == cv.NOT_APPLICABLE
    )
    bp = conventions_rules.rule_bugfix_has_test(
        files, {}, "Backport #117700 to 26.8: Fix rseq", ["pr-bugfix"], ""
    )
    assert bp.verdict == cv.NOT_APPLICABLE and bp.evidence[0]["backport_of"] == 117700
    assert (
        conventions_rules.rule_test_only_change(["tests/queries/0_stateless/1_a.sql"]).verdict
        == cv.SATISFIED
    )
    assert conventions_rules.rule_test_only_change(files).verdict == cv.NOT_APPLICABLE
    assert conventions_rules.rule_test_only_change(["tests/ci/x.py"]).verdict == cv.NOT_APPLICABLE


def test_stateless_number_flags_a_name_collision_from_an_earlier_pr_only() -> None:
    new = conventions_rules.new_stateless_tests(TEST_FILES, TEST_STATUSES)
    assert sorted(t.stem for t in new) == ["04652_new_case", "04652_no_ref"]
    index = [
        "tests/queries/0_stateless/04652_new_case.sql",
        "tests/queries/0_stateless/04652_other.sql",
        "tests/queries/0_stateless/04652_no_ref.sh",
    ]
    owners = {
        "tests/queries/0_stateless/04652_new_case.sql": (100, "2026-09-01 00:00:00"),
        "tests/queries/0_stateless/04652_no_ref.sh": (300, "2026-09-10 00:00:00"),
    }
    # at this PR's merge the .sql was already added by #100 (collision); the .sh
    # came later (#300) — a replay artifact, not this PR's fault
    r = conventions_rules.rule_stateless_test_number(
        new, index, owners, merged_at="2026-09-05 00:00:00", max_number=5217
    )
    assert r.verdict == cv.VIOLATED and [e["owner_pr"] for e in r.evidence] == [100]
    # the PR's own files are what the index holds: satisfied, the shared number reported
    own = {
        "tests/queries/0_stateless/04652_new_case.sql": (42, "2026-09-05 00:00:00"),
        "tests/queries/0_stateless/04652_no_ref.sh": (42, "2026-09-05 00:00:00"),
    }
    s = conventions_rules.rule_stateless_test_number(
        new, index, own, merged_at="2026-09-05 00:00:00", max_number=5217
    )
    assert s.verdict == cv.SATISFIED and s.evidence[0]["other_stems_with_number"] == 1
    assert s.note.startswith("names unique")  # nothing reused: the reused note must not appear
    assert s.evidence[0]["below_max_by"] == 565
    # a path the ledger never saw predates it
    old = conventions_rules.rule_stateless_test_number(
        new, index, {}, merged_at="2026-09-05 00:00:00", max_number=0
    )
    assert old.verdict == cv.VIOLATED and old.evidence[0]["owner_merged_at"] == "before_ledger"
    assert (
        conventions_rules.rule_stateless_test_number(
            new, None, {}, merged_at="", max_number=0
        ).verdict
        == cv.UNKNOWN
    )
    # a name an earlier (reverted) PR used, absent from this PR's pre-image: no collision
    reused = conventions_rules.rule_stateless_test_number(
        new,
        index,
        owners,
        merged_at="2026-09-05 00:00:00",
        max_number=5217,
        preexisting=set(),
    )
    assert reused.verdict == cv.SATISFIED and reused.evidence[0]["in_pre_image"] is False
    # present in the pre-image: the collision stands
    hit = conventions_rules.rule_stateless_test_number(
        new,
        index,
        owners,
        merged_at="2026-09-05 00:00:00",
        max_number=5217,
        preexisting={"tests/queries/0_stateless/04652_new_case.sql"},
    )
    assert hit.verdict == cv.VIOLATED
    # the PR's own conversion: the same-stem file it deletes is no collision
    conv = conventions_rules.rule_stateless_test_number(
        new,
        index,
        owners,
        merged_at="2026-09-05 00:00:00",
        max_number=5217,
        preexisting={"tests/queries/0_stateless/04652_new_case.sql"},
        deleted={"tests/queries/0_stateless/04652_new_case.sql"},
    )
    assert conv.verdict == cv.SATISFIED and conv.evidence[0]["deleted_in_diff"] is True
    # the backport bot's PR is not the one the convention is about
    bp = conventions_rules.rule_stateless_test_number(
        new,
        index,
        owners,
        merged_at="2026-09-05 00:00:00",
        max_number=5217,
        title="Backport #100 to 26.7: Fix x",
    )
    assert bp.verdict == cv.NOT_APPLICABLE and bp.evidence[0]["backport_of"] == 100
    assert (
        conventions_rules.backport_of("Cherry pick #115824 to 26.3: Fix bad projection names")
        == 115824
    )
    assert conventions_rules.backport_of("Fix backport script") == 0
    assert (
        conventions_rules.backport_of(
            'Revert "Revert "Backport #115755 to 26.8: Weigh the adaptive aggregation""'
        )
        == 115755
    )
    assert (
        conventions_rules.rule_stateless_test_number([], [], {}, merged_at="", max_number=0).verdict
        == cv.NOT_APPLICABLE
    )


def test_reference_missing_reads_the_diff_only() -> None:
    new = conventions_rules.new_stateless_tests(TEST_FILES, TEST_STATUSES)
    r = conventions_rules.rule_reference_missing(TEST_FILES, TEST_STATUSES, new)
    assert r.verdict == cv.VIOLATED and r.evidence[0]["stem"] == "04652_no_ref"
    assert r.evidence[0]["expected"] == "tests/queries/0_stateless/04652_no_ref.reference"
    files = TEST_FILES[:3]
    assert (
        conventions_rules.rule_reference_missing(
            files, TEST_STATUSES, conventions_rules.new_stateless_tests(files, TEST_STATUSES)
        ).verdict
        == cv.SATISFIED
    )
    conv = ["tests/queries/0_stateless/04652_x.sql", "tests/queries/0_stateless/04652_x.sh"]
    st = {conv[0]: "A", conv[1]: "D"}
    c = conventions_rules.rule_reference_missing(
        conv, st, conventions_rules.new_stateless_tests(conv, st)
    )
    assert c.verdict == cv.NOT_APPLICABLE


# ---- leads ------------------------------------------------------------------


def test_changelog_category_is_unusual_under_ten_percent_of_the_dominant_prefix() -> None:
    census = {
        "src/Storages": {"bug_fix": 80, "improvement": 15, "documentation": 2},
        "tests/queries": {"bug_fix": 50},
    }
    r = conventions_rules.rule_changelog_category("documentation", "label:pr-documentation", census)
    assert r.verdict == cv.UNUSUAL and r.epistemic == cv.LOWER_BOUND
    assert r.evidence[0]["share"] == 0.021 and r.evidence[0]["mode"] == "bug_fix"
    assert (
        conventions_rules.rule_changelog_category("bug_fix", "label:pr-bugfix", census).verdict
        == cv.SATISFIED
    )
    # a thin history (< 20 PRs) never says unusual
    assert (
        conventions_rules.rule_changelog_category(
            "documentation", "x", {"src/Storages": {"bug_fix": 5}}
        ).verdict
        == cv.SATISFIED
    )
    assert conventions_rules.rule_changelog_category("", "", census).verdict == cv.NOT_APPLICABLE
    assert (
        conventions_rules.rule_changelog_category("bug_fix", "x", {"src/Storages": None}).verdict
        == cv.UNKNOWN
    )
    assert conventions_rules.dominant_prefixes(
        ["src/Storages/A.cpp", "src/Storages/B.cpp", "tests/queries/x.sql"]
    ) == ["src/Storages", "tests/queries"]
    # source areas lead even when the test files outnumber them
    assert conventions_rules.dominant_prefixes(
        ["src/Core/A.cpp", "tests/queries/x.sql", "tests/queries/y.sql"]
    ) == ["src/Core", "tests/queries"]


def test_wide_callers_untested_counts_directories_and_needs_the_control() -> None:
    d = cv.parse_unified_diff(ERROR_DIFF)
    symbols = [
        {
            "symbol_id": "s1",
            "qualified_name": "DB::Foo::run",
            "name": "run",
            "path": "src/Storages/Foo.cpp",
            "start_line": 30,
            "end_line": 60,
            "tier": "treesitter",
        },
        # the compiler tier records the same function as a one-line span; its edges carry its own id
        {
            "symbol_id": "scip1",
            "qualified_name": "DB::Foo::run",
            "name": "run",
            "path": "src/Storages/Foo.cpp",
            "start_line": 30,
            "end_line": 30,
            "tier": "scip",
        },
        {
            "symbol_id": "s2",
            "qualified_name": "DB::Foo::other",
            "name": "other",
            "path": "src/Storages/Foo.cpp",
            "start_line": 100,
            "end_line": 120,
        },
    ]
    funcs = conventions_rules.touched_functions(d, symbols)
    assert [f.qualified_name for f in funcs] == ["DB::Foo::run"] and funcs[0].symbol_ids == [
        "s1",
        "scip1",
    ]
    edges = [
        {"to_symbol_id": "scip1", "path": p, "line": 1}
        for p in (
            "src/Storages/X.cpp",
            "src/Interpreters/Y.cpp",
            "src/Processors/Z.cpp",
            "src/Functions/F.cpp",
            "src/Server/S.cpp",
            "src/Core/C.cpp",
            "programs/client/c.cpp",
        )
    ] + [
        {"to_symbol_id": "s1", "path": "src/Storages/X.cpp", "line": 1}
    ]  # the same call site, parser tier
    conventions_rules.attach_callers(funcs, edges)
    assert funcs[0].callers == 7 and len(funcs[0].caller_dirs) == 7
    r = conventions_rules.rule_wide_callers_untested(
        funcs, {"s1": ("uncovered", 0)}, min_dirs=5, control_ok=True
    )
    assert (
        r.verdict == cv.VIOLATED
        and r.evidence[0]["dirs"] == 7
        and r.evidence[0]["coverage"] == "uncovered"
    )
    assert (
        conventions_rules.rule_wide_callers_untested(
            funcs, {"s1": ("covered_by", 3)}, min_dirs=5, control_ok=True
        ).verdict
        == cv.SATISFIED
    )
    assert (
        conventions_rules.rule_wide_callers_untested(
            funcs, {}, min_dirs=5, control_ok=False
        ).verdict
        == cv.UNKNOWN
    )
    assert (
        conventions_rules.rule_wide_callers_untested(funcs, {}, min_dirs=8, control_ok=True).verdict
        == cv.NOT_APPLICABLE
    )
    assert (
        conventions_rules.rule_wide_callers_untested([], {}, min_dirs=5, control_ok=True).verdict
        == cv.NOT_APPLICABLE
    )


# ---- the handler ------------------------------------------------------------


class _Repo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def pr_facts(self, org, ns, pr_number):
        self.calls.append("pr_facts")
        if pr_number != 4242:
            return None
        return {
            "pr_number": 4242,
            "title": "Fix widgets",
            "labels": ["pr-bugfix", "comp-x"],
            "body_excerpt": "- Bug Fix\n",
            "files": [],
            "merge_commit_sha": b"a" * 40,
            "merged_at": None,
        }

    async def chunks_of_path(self, org, ns, path, *, limit=400):
        self.calls.append("chunks_of_path")
        return []

    async def chunk_contains(self, org, ns, path, literal, *, limit=10):
        self.calls.append("chunk_contains")
        return []

    async def stateless_paths_for_numbers(self, org, ns, numbers, *, limit=400):
        self.calls.append("stateless_paths")
        return [
            "tests/queries/0_stateless/04652_other.sql",
            "tests/queries/0_stateless/04652_new_case.sql",
        ]

    async def max_stateless_test_number(self, org, ns):
        return 5217

    async def first_owner_of_paths(self, org, ns, paths, *, branch=""):
        self.calls.append("first_owner")
        return {"tests/queries/0_stateless/04652_new_case.sql": (4242, "2026-09-05 00:00:00")}

    async def pr_category_census(self, org, ns, prefix, *, days=365, branch=""):
        self.calls.append("census:" + prefix)
        return {"pr-bugfix": 80, "pr-improvement": 10}

    async def functions_for_paths(
        self, org, ns, paths, *, limit=400, kinds=(), with_extent_only=False
    ):
        return [
            {
                "symbol_id": "s1",
                "qualified_name": "DB::Foo::run",
                "name": "run",
                "path": "src/Storages/Foo.cpp",
                "start_line": 30,
                "end_line": 60,
            }
        ]

    async def edges_for_symbols(
        self,
        org,
        ns,
        ids,
        *,
        direction,
        edge_kinds=None,
        limit=200,
        min_confidence=0.0,
        per_match=None,
    ):
        return [{"to_symbol_id": "s1", "path": f"src/D{i}/x.cpp"} for i in range(7)]

    async def grep_files(
        self, org, ns, pattern, *, regex=False, case_sensitive=True, path_prefix="", limit=200
    ):
        self.calls.append("grep_files:" + pattern)
        if pattern == "arrayJoin":
            return [{"path": "tests/queries/0_stateless/1_a.sql"}], 1
        return [], 0

    async def coverage_rows_for(self, org, ns, paths, *, limit=2000):
        return []

    async def coverage_files_for(self, org, ns, paths):
        return {}

    async def test_map_detail(self, org, ns, paths, *, limit=60):
        return []


class _Git:
    """The mirror: a merge commit `a`*40 whose diff is the fixtures'."""

    def __init__(self, *, fail_diff: bool = False) -> None:
        self.fail_diff = fail_diff
        self.runs: list[tuple[str, ...]] = []

    async def run(self, git_dir, *args, timeout_s=0):
        self.runs.append(args)
        if args[0] == "rev-parse" and args[-1].endswith(
            ":tests/queries/0_stateless/04652_new_case.sql"
        ):
            raise h.gitrepo.GitError("absent from the pre-image")
        if args[0] == "rev-parse":
            return "a" * 40 + "\n"
        if args[0] == "merge-base":
            return "b" * 40 + "\n"
        raise h.gitrepo.GitError("nope")

    async def run_bounded(self, git_dir, *args, max_bytes, timeout_s=0):
        self.runs.append(args)
        if self.fail_diff:
            raise h.gitrepo.GitError("boom")
        return SETTINGS_DIFF + ERROR_DIFF, False

    async def diff_files(self, git_dir, base, head):
        entries = [
            h.gitrepo.DiffEntry("M", "src/Core/Settings.cpp"),
            h.gitrepo.DiffEntry("M", "src/Storages/Foo.cpp"),
            h.gitrepo.DiffEntry("M", "src/Storages/Bar.cpp"),
        ]
        entries += [h.gitrepo.DiffEntry(TEST_STATUSES[p], p) for p in TEST_FILES]
        return entries

    async def head_sha(self, git_dir, branch):
        return "c" * 40

    async def show_file(self, git_dir, sha, path, max_bytes=0):
        if path == "src/Storages/Foo.cpp":
            return b"namespace ErrorCodes { extern const int BAD_ARGUMENTS; }\n"
        return b"namespace ErrorCodes { extern const int ADDED_HERE; }\n"

    async def grep_paths(self, git_dir, rev, literal, pathspecs, *, limit=50, timeout_s=0):
        self.runs.append(("grep", literal))
        return (
            ["docs/reference/settings/session-settings/x.mdx"] if literal == "max_widgets" else []
        )


class _Cfg:
    org, namespace, git_dir, git_branch = ORG, NS, "/mirror.git", "master"


@pytest.fixture
def wired(monkeypatch):
    from server.code.app import coverage as code_coverage

    code_coverage.reset_control_cache()  # The control is kept per namespace
    repo = _Repo()
    git = _Git()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)
    monkeypatch.setattr(h, "repo_for_namespace", lambda org, ns: _Cfg())
    for name in ("run", "run_bounded", "diff_files", "head_sha", "show_file", "grep_paths"):
        monkeypatch.setattr(h.gitrepo, name, getattr(git, name))
    yield repo, git


def test_op_is_registered_experimental_viewer_read_and_input_needs_a_source() -> None:
    op = OPERATIONS_BY_NAME["code.conventions"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    # the source rule is not the model's; the rule names still are
    assert CodeConventionsInput().pr_number == 0
    with pytest.raises(ValueError):
        CodeConventionsInput(pr_number=1, rules=["nope"])
    assert CodeConventionsInput(pr_number=1, rules=["setting_history"]).rules == ["setting_history"]


@pytest.mark.asyncio
async def test_missing_source_is_refused_in_the_handler_after_the_gate(wired) -> None:
    """The "pr_number, head_sha, or files/diff" rule lives in the handler,
    after the namespace gate, so a refused principal never
    learns the op's argument shape from a model-level 422."""
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="pr_number, head_sha, or files/diff is required"):
        await h.handle_code_conventions(CodeConventionsInput(org=ORG, namespace=NS))


@pytest.mark.asyncio
async def test_handler_replays_a_merged_pr_from_the_ledger_and_the_mirror(wired) -> None:
    repo, git = wired
    out = await h.handle_code_conventions(
        CodeConventionsInput(org=ORG, namespace=NS, pr_number=4242)
    )
    assert out.source == "ledger" and out.merge_commit_sha == "a" * 40 and out.files_total == 7
    by = {r["rule"]: r for r in out.rules}
    assert set(by) == set(cv.RULES)
    assert by["setting_history"]["verdict"] == cv.VIOLATED  # no entry in diff, history file empty
    assert (
        by["setting_docs"]["verdict"] == cv.SATISFIED
        and by["setting_docs"]["evidence"][1]["generated"] is True
    )
    assert (
        by["error_code_declared"]["verdict"] == cv.VIOLATED
        and by["error_code_declared"]["epistemic"] == cv.EXACT
    )
    assert by["error_code_declared"]["evidence"][0]["error_code"] == "NEW_CODE"
    assert by["bugfix_has_test"]["verdict"] == cv.SATISFIED  # the diff adds stateless tests
    assert by["test_only_change"]["verdict"] == cv.NOT_APPLICABLE
    assert (
        by["stateless_test_number"]["verdict"] == cv.SATISFIED
    )  # the index holds this PR's own file
    assert by["reference_missing"]["verdict"] == cv.VIOLATED
    assert (
        by["changelog_category"]["verdict"] == cv.SATISFIED
        and by["changelog_category"]["evidence"][0]["prs"] == 90
    )
    assert by["wide_callers_untested"]["verdict"] == cv.VIOLATED and out.control == {
        "coverage_symbol": "arrayJoin",
        "ok": True,
        "ran": True,
    }
    assert out.summary[cv.VIOLATED] == 4 and out.category == "bug_fix"
    # the merge commit's first parent is the base
    assert ("diff", "-U0", "--no-renames", "--no-color", "a" * 40 + "^1", "a" * 40) in git.runs
    # the census and the docs greps are cached across calls
    n = len([c for c in repo.calls if c.startswith("census")])
    await h.handle_code_conventions(CodeConventionsInput(org=ORG, namespace=NS, pr_number=4242))
    assert len([c for c in repo.calls if c.startswith("census")]) == n


@pytest.mark.asyncio
async def test_a_failed_mirror_diff_leaves_diff_rules_unknown_not_clean(wired, monkeypatch) -> None:
    repo, git = wired
    git.fail_diff = True
    out = await h.handle_code_conventions(
        CodeConventionsInput(org=ORG, namespace=NS, pr_number=4242)
    )
    by = {r["rule"]: r for r in out.rules}
    assert (
        by["setting_history"]["verdict"] == cv.UNKNOWN
        and by["error_code_declared"]["verdict"] == cv.UNKNOWN
    )
    assert by["wide_callers_untested"]["verdict"] == cv.UNKNOWN
    # the ledger's file list still answers the file-shaped rules
    assert by["bugfix_has_test"]["verdict"] == cv.UNKNOWN  # no files in this fixture's ledger row
    assert any("mirror diff failed" in n for n in out.notes)
    unknown = await h.handle_code_conventions(
        CodeConventionsInput(org=ORG, namespace=NS, pr_number=7)
    )
    assert "pr_not_in_ledger" in unknown.notes and all(
        r["verdict"] == cv.UNKNOWN for r in unknown.rules
    )


@pytest.mark.asyncio
async def test_a_supplied_diff_is_read_against_the_branch_tip(wired) -> None:
    repo, git = wired
    out = await h.handle_code_conventions(
        CodeConventionsInput(
            org=ORG,
            namespace=NS,
            files=TEST_FILES,
            diff=ERROR_DIFF,
            rules=["error_code_declared", "reference_missing"],
        )
    )
    assert out.source == "supplied" and [r["rule"] for r in out.rules] == [
        "error_code_declared",
        "reference_missing",
    ]
    assert out.files_total == len(TEST_FILES) + 2  # Foo.cpp and Bar.cpp from the diff
    by = {r["rule"]: r for r in out.rules}
    assert by["error_code_declared"]["verdict"] == cv.VIOLATED
    # a supplied diff's statuses come from the diff; ledger-less files default to M
    assert by["reference_missing"]["verdict"] == cv.NOT_APPLICABLE
    # the pre-image at the branch tip declares BAD_ARGUMENTS; NEW_CODE is the violation
    ev = by["error_code_declared"]["evidence"]
    assert ev[0]["error_code"] == "NEW_CODE" and any(e.get("where") == "file" for e in ev)


def test_mcp_description_and_sdk_method_exist() -> None:
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    assert (
        "code.conventions" in TOOL_DESCRIPTIONS
        and "never opinions" in TOOL_DESCRIPTIONS["code.conventions"]
    )
    from loom.code import CodeClient

    assert hasattr(CodeClient, "conventions")


@pytest.mark.asyncio
async def test_a_supplied_diff_with_a_pr_number_takes_the_ledger_metadata_not_its_files(
    wired,
) -> None:
    repo, git = wired
    out = await h.handle_code_conventions(
        CodeConventionsInput(
            org=ORG,
            namespace=NS,
            pr_number=4242,
            files=["src/Databases/x.cpp"],
            diff="diff --git a/src/Databases/x.cpp b/src/Databases/x.cpp\n--- a/src/Databases/x.cpp\n+++ b/src/Databases/x.cpp\n@@ -1,0 +2 @@\n+int x;\n",
            rules=["bugfix_has_test"],
        )
    )
    assert (
        out.source == "supplied" and out.labels == ["pr-bugfix", "comp-x"] and out.files_total == 1
    )
    assert (
        out.rules[0]["verdict"] == cv.VIOLATED
    )  # the mirror's file list (with tests) is not consulted


@pytest.mark.asyncio
async def test_a_supplied_diff_checks_error_codes_at_the_namespaces_own_branch(
    monkeypatch,
) -> None:
    """With no post-image sha, the declarations are read at the tip of the namespace's
    branch: a release-branch namespace reads its release branch, not the default one."""
    seen: list[str] = []

    async def head_sha(git_dir, branch):
        seen.append(branch)
        return "d" * 40

    async def show_file(git_dir, sha, path, max_bytes=0):
        return b"namespace ErrorCodes { extern const int BAD_ARGUMENTS; }\n"

    monkeypatch.setattr(h.gitrepo, "head_sha", head_sha)
    monkeypatch.setattr(h.gitrepo, "show_file", show_file)
    uses = [cv.ErrorCodeUse(path="src/Foo.cpp", name="BAD_ARGUMENTS", line=1, text="")]
    declared, _exact = await h._externs(_Repo(), ORG, NS, "/mirror.git", "", uses, "26.8")
    assert seen == ["26.8"] and declared[("src/Foo.cpp", "BAD_ARGUMENTS")] is True


@pytest.mark.asyncio
async def test_an_extern_file_over_the_ingest_cap_is_unknown_not_undeclared(monkeypatch) -> None:
    from server.code.app.index import gitrepo

    async def too_large(git_dir: str, sha: str, path: str, max_bytes: int = 0) -> bytes:
        raise gitrepo.BlobTooLarge(path, max_bytes + 1, "b" * 40, max_bytes, b"")

    class Repo:
        async def chunk_contains(self, *a: object, **k: object) -> list[object]:
            return []  # an oversize file has no chunks

    monkeypatch.setattr(gitrepo, "show_file", too_large)
    use = cv.ErrorCodeUse(path="src/Common/ErrorCodes.cpp", name="NEW_CODE", line=1, text="")
    got, exact = await h._externs(Repo(), "o", "ns", "/git", "a" * 40, [use], "main")
    assert got == {("src/Common/ErrorCodes.cpp", "NEW_CODE"): None} and not exact
