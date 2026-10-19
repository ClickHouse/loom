"""code.bisect_candidates: the release range is `(good, bad]`
in ClickHouse minors, frames resolve to source files through the code
index, QA's triage row supplies the range when the caller does not, a PR
that names the frame in its diff outranks one that only touched the file,
and every missing input is a named cause on `unknown`, never a guess."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.code.app.bisect as h
import server.tracker.app.binding as binding
from server.code.api.models_history import CodeBisectCandidatesInput
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.tracker.domain import bisect as bi
from server.tracker.domain.errors import TrackerUnavailable

ORG, NS, QA = "clickhouse", "code-clickhouse", "tracker-clickhouse"
REPO = "ClickHouse/ClickHouse"
SRC = "src/Storages/MergeTree/MergeTreeData.cpp"


# ---- pure helpers -------------------------------------------------------------


def test_versions_between_walks_minors_across_the_year() -> None:
    # (good, bad + 1 slack]: the culprit's stamp may lag the reproducing branch
    assert bi.versions_between("25.11", "26.2") == ["25.12", "26.1", "26.2", "26.3"]
    assert bi.versions_between("25.11", "26.2", slack=0) == ["25.12", "26.1", "26.2"]
    # no clean release: a bounded lookback, not an unbounded floor
    assert bi.versions_between("", "26.4") == ["26.1", "26.2", "26.3", "26.4", "26.5"]
    assert bi.versions_between("", "26.2", lookback=1, slack=0) == ["26.1", "26.2"]
    assert bi.versions_between("26.4", "26.4") == ["26.1", "26.2", "26.3", "26.4", "26.5"]
    assert bi.versions_between("26.5", "26.4")[-1] == "26.5"  # good newer than bad: lookback
    assert bi.versions_between("26.1", "nope") == []


def test_hub_paths_leave_the_filter_while_a_specific_path_remains() -> None:
    kept, dropped = bi.drop_hub_paths(
        [
            SRC,
            "src/Core/Settings.cpp",
            "src/Core/SettingsChangesHistory.cpp",
            "docs/en/x.md",
            "tests/queries/0_stateless/1.sql",
        ]
    )
    assert kept == [SRC]
    assert dropped == [
        "src/Core/Settings.cpp",
        "src/Core/SettingsChangesHistory.cpp",
        "docs/en/x.md",
        "tests/queries/0_stateless/1.sql",
    ]
    # hub files only: kept, nothing dropped (some signal beats none)
    assert bi.drop_hub_paths(["src/Core/Settings.cpp"]) == (["src/Core/Settings.cpp"], [])
    assert bi.is_hub_path("src/Storages/MergeTree/MergeTreeSettings.cpp") and not bi.is_hub_path(
        SRC
    )
    assert bi.parse_version("v26.4.1.12") == (26, 4) and bi.parse_version("26.13") is None
    assert (
        bi.oldest(["26.5", "26.4.1", "junk"]) == "26.4" and bi.newest(["26.3", "25.12"]) == "26.3"
    )


def test_frame_names_keep_specific_bare_names_only() -> None:
    frames = bi.frame_names(
        [
            "DB::MergeTreeData::loadDataParts(bool)",
            "DB::IColumn::insert",
            "get",
            "src/Storages/MergeTree/MergeTreeData.cpp",
            "DB::Context::getSettingsRef() const",
            "tests.integration.helpers.cluster.ClickHouseCluster.start",
            "loadDataParts",
        ]
    )
    assert frames == ["loadDataParts", "getSettingsRef"]
    assert bi.bare_name("MergeTreeData.cpp:123") == ""


def test_rank_prefers_frame_hits_then_paths_then_recency() -> None:
    rows = [
        {
            "pr_number": 1,
            "matched_symbols": [],
            "matched_paths": [SRC],
            "merged_at": datetime(2026, 5, 1),
        },
        {
            "pr_number": 2,
            "matched_symbols": ["loadDataParts"],
            "matched_paths": [SRC],
            "merged_at": datetime(2026, 4, 1),
        },
        {
            "pr_number": 3,
            "matched_symbols": [],
            "matched_paths": [SRC],
            "merged_at": datetime(2026, 5, 9),
            "has_test": None,
        },
    ]
    ranked = bi.rank_candidates(rows, frames=["loadDataParts"], paths=[SRC], limit=10)
    assert [c.pr_number for c in ranked] == [2, 3, 1]
    assert ranked[0].score == 3.0 and ranked[1].score == 1.0
    assert ranked[1].as_dict()["has_test"] is None


def test_verdict_names_every_missing_input() -> None:
    assert bi.verdict_of(
        candidates=[], versions=[], paths=[], introduced_commit="", qa_ok=False
    ) == (bi.UNKNOWN, ["tracker_unavailable", "no_failing_version", "no_resolved_paths"])
    assert bi.verdict_of(
        candidates=[], versions=["26.4"], paths=[SRC], introduced_commit="", qa_ok=True
    ) == (bi.NO_CANDIDATES, [])
    assert bi.verdict_of(
        candidates=[], versions=[], paths=[], introduced_commit="abc", qa_ok=True
    ) == (bi.ALREADY_BISECTED, ["introduced_commit_known"])


# ---- handler -----------------------------------------------------------------


class _Repo:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        self.calls.append(name)
        if "loadDataParts" in name:
            return [
                {"path": SRC},
                {"path": "tests/queries/0_stateless/02000_x.sql"},
                {"path": "src/Storages/MergeTree/MergeTreeData.h"},
            ]
        return []


class _Source:
    def __init__(
        self,
        *,
        issue: dict[str, Any] | None = None,
        prs: list[dict[str, Any]] | None = None,
        down: bool = False,
    ) -> None:
        self.issue, self.prs, self.down = issue, prs or [], down
        self.pr_calls: list[dict[str, Any]] = []

    def repos(self) -> list[str]:
        # The tracker names its repositories; no fallback to one
        return ["ClickHouse/ClickHouse"]

    async def items_by_number(self, org, ns, nums):
        if self.down:
            raise TrackerUnavailable("breaker OPEN")
        out = {n: self.issue for n in nums if self.issue and n == self.issue.get("number")}
        for n in nums:
            if n in getattr(self, "pr_rows", {}):
                out[n] = self.pr_rows[n]
        return out

    async def edges_from(self, org, ns, refs, rels=None, *, budget_ms=None):
        if self.down:
            raise TrackerUnavailable("breaker OPEN")
        self.edge_budgets = [*getattr(self, "edge_budgets", []), budget_ms]
        return list(getattr(self, "edges", []))

    async def merged_prs_touching(
        self,
        org,
        ns,
        paths,
        versions,
        *,
        symbols=None,
        patterns=None,
        dirs=None,
        scope_patterns=None,
        pattern_weights=None,
        limit=20,
        before=None,
        after=None,
    ):
        if self.down:
            raise TrackerUnavailable("breaker OPEN")
        self.pr_calls.append(
            {
                "paths": paths,
                "versions": versions,
                "symbols": symbols,
                "patterns": patterns,
                "dirs": dirs,
                "limit": limit,
                "before": before,
                "after": after,
            }
        )
        return list(self.prs)

    async def issue_text(self, org, ns, number, *, chars=6000):
        if self.down:
            raise TrackerUnavailable("breaker OPEN")
        return self.text if hasattr(self, "text") else ""

    async def token_stats(self, org, ns):
        return dict(getattr(self, "stats", {}))

    async def component_dirs(self, org, ns, component, *, limit=8, min_lift=1.5):
        self.component_calls = [*getattr(self, "component_calls", []), component]
        return list(getattr(self, "dirs", []))

    async def pr_change(self, org, ns, number):
        self.change_calls = [*getattr(self, "change_calls", []), number]
        return getattr(self, "changes", {}).get(number)


@pytest.fixture
def wired(monkeypatch):
    repo = _Repo()
    monkeypatch.setattr(h, "CodeRepo", lambda: repo)
    monkeypatch.setattr(h, "require_code_intel", lambda: None)
    monkeypatch.setattr(h, "schedule_code_trace", lambda **kw: None)

    async def _authorized(org, code_ns, *, hides_answer=True):
        return QA if code_ns == NS else ""

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)
    holder: dict[str, Any] = {}

    def _use(source: _Source) -> _Source:
        holder["src"] = source
        return source

    import server.tracker.store.source as qs

    monkeypatch.setattr(qs, "TrackerSource", lambda org, ns: holder["src"])
    return repo, _use


def test_op_is_registered_experimental_viewer_read() -> None:
    op = OPERATIONS_BY_NAME["code.bisect_candidates"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    # the subject rule is not the model's
    assert CodeBisectCandidatesInput(org=ORG, namespace=NS).issue_number == 0


@pytest.mark.asyncio
async def test_missing_subject_is_refused_in_the_handler_after_the_gate(wired) -> None:
    """The "symbols, paths, issue_number" rule lives in the handler, after the
    namespace gate, so a refused principal never learns the
    op's argument shape from a model-level 422."""
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="name at least one of symbols, paths, issue_number"):
        await h.handle_code_bisect_candidates(CodeBisectCandidatesInput(org=ORG, namespace=NS))


@pytest.mark.asyncio
async def test_candidates_from_frames_and_stated_range(wired) -> None:
    repo, use = wired
    src = use(
        _Source(
            prs=[
                {
                    "pr_number": 500,
                    "title": "Refactor parts loading",
                    "author": "a",
                    "matched_paths": [SRC],
                    "matched_symbols": ["loadDataParts"],
                    "merged_at": datetime(2026, 3, 2),
                    "minor_version": "26.3",
                    "has_test": 0,
                    "url": "u/500",
                },
                {
                    "pr_number": 501,
                    "title": "Comment",
                    "author": "b",
                    "matched_paths": [SRC],
                    "matched_symbols": [],
                    "merged_at": datetime(2026, 3, 9),
                    "minor_version": "26.3",
                },
            ]
        )
    )
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            symbols=["DB::MergeTreeData::loadDataParts(bool)", "get"],
            bad_version="26.3",
            good_version="26.1",
        )
    )
    assert out.verdict == "candidates" and out.causes == [] and out.epistemic == "lower_bound"
    assert [c["pr_number"] for c in out.candidates] == [500, 501]
    assert (
        out.candidates[0]["matched_symbols"] == ["loadDataParts"]
        and out.candidates[0]["has_test"] is False
    )
    assert out.versions_searched == ["26.2", "26.3", "26.4"] and out.frames == ["loadDataParts"]
    assert out.window == "clean_to_bad_plus_slack" and out.hub_paths_dropped == []
    # tests never count as a frame's home; the header does
    assert out.paths == [SRC, "src/Storages/MergeTree/MergeTreeData.h"]
    assert out.resolved[0]["symbol"].startswith("DB::MergeTreeData") and out.tracker_namespace == QA
    call = src.pr_calls[0]
    assert call["versions"] == ["26.2", "26.3", "26.4"] and call["symbols"] == ["loadDataParts"]
    assert call["limit"] == 30 and not out.tracker_unavailable
    # Frames present → the candidate set widens to the frames' directories
    assert call["dirs"] == ["src/Storages/MergeTree/"] and out.scope == "directory"
    assert out.patterns == [] and out.candidates[0]["kind"] == "meta"  # "Refactor parts loading"


@pytest.mark.asyncio
async def test_issue_row_supplies_the_range_and_a_converged_bisect_short_circuits(wired) -> None:
    _, use = wired
    issue = {
        "repo": REPO,
        "number": 100,
        "is_pr": 0,
        "reproduces_on": ["26.4", "26.5"],
        "clean_on": ["26.2", "master"],
        "affected_versions": [],
        "introduced_commit": "",
        "primary_component": "MergeTree",
        "labels": [],
        "bisect_outcome": "PARTIAL",
    }
    src = use(_Source(issue=issue, prs=[]))
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            symbols=["DB::MergeTreeData::loadDataParts"],
            issue_number=100,
        )
    )
    assert (
        out.verdict == "no_candidates" and out.bad_version == "26.4" and out.good_version == "26.2"
    )
    assert (
        out.versions_searched == ["26.3", "26.4", "26.5"] and out.issue["component"] == "MergeTree"
    )
    assert src.pr_calls and src.pr_calls[0]["versions"] == ["26.3", "26.4", "26.5"]
    assert out.issue["title"] == "" and out.patterns == []

    issue["introduced_commit"] = "abcdef1234567890"
    src = use(_Source(issue=issue, prs=[]))
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            symbols=["DB::MergeTreeData::loadDataParts"],
            issue_number=100,
        )
    )
    assert out.verdict == "already_bisected" and out.introduced_commit.startswith("abcdef")
    assert src.pr_calls == []  # nothing searched


@pytest.mark.asyncio
async def test_missing_inputs_are_named_causes(wired) -> None:
    _, use = wired
    use(_Source(prs=[]))
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            symbols=["DB::Nowhere::unknownFrame"],
            bad_version="26.4",
        )
    )
    assert out.verdict == "unknown" and out.causes == ["no_resolved_paths"]
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            paths=[SRC],
        )
    )
    assert out.verdict == "unknown" and out.causes == ["no_failing_version"]
    use(_Source(down=True))
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            paths=[SRC],
            bad_version="26.4",
        )
    )
    assert (
        out.verdict == "unknown"
        and out.causes == ["tracker_unavailable"]
        and out.tracker_unavailable
    )


@pytest.mark.asyncio
async def test_unbound_code_namespace_is_tracker_unbound(wired) -> None:
    _, use = wired
    use(_Source(prs=[]))
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace="code-other",
            paths=[SRC],
            bad_version="26.4",
        )
    )
    assert (
        out.verdict == "unknown"
        and out.causes == ["tracker_unbound"]
        and out.tracker_namespace == ""
    )
    assert not out.tracker_unavailable


# ---- patterns, added lines, meta demotion, widening --------------------------


def test_derive_patterns_keeps_shaped_identifiers_most_frequent_first() -> None:
    text = (
        "Logical error: applyPatchesMergeOnKey spends 8.6s; apply_patch_parts=0 is fast. "
        "LOGICAL_ERROR in applyPatchesMergeOnKey again. ClickHouse version 26.4. "
        "Received exception from server. The following describes it. lightweight_update_mode"
    )
    pats = bi.derive_patterns(text, exclude=["loadDataParts"])
    assert pats[0] == "applyPatchesMergeOnKey"  # twice
    assert (
        "apply_patch_parts" in pats
        and "LOGICAL_ERROR" in pats
        and "lightweight_update_mode" in pats
    )
    assert "ClickHouse" not in pats and "Received" not in pats and "following" not in pats
    assert (
        bi.derive_patterns("") == []
        and len(bi.derive_patterns(" ".join(f"ident_{i}_x" for i in range(40)))) == 12
    )
    opaque = "AbC9" * 100
    assert opaque not in bi.derive_patterns(f"{opaque} applyPatchesMergeOnKey")
    assert "applyPatchesMergeOnKey" in bi.derive_patterns(f"{opaque} applyPatchesMergeOnKey")


def test_kind_and_rank_prefer_added_patterns_and_demote_meta() -> None:
    assert (
        bi.kind_of("Refactor parts loading") == "meta"
        and bi.kind_of("Fix race in loadDataParts") == "change"
    )
    rows = [
        {
            "pr_number": 1,
            "title": "Refactor parts loading",
            "matched_symbols": ["loadDataParts"],
            "matched_paths": [SRC],
            "matched_patterns": [],
            "added_patterns": [],
            "merged_at": datetime(2026, 5, 1),
        },
        {
            "pr_number": 2,
            "title": "Speed up patch application",
            "matched_symbols": [],
            "matched_paths": [SRC],
            "matched_patterns": ["applyPatchesMergeOnKey"],
            "added_patterns": ["applyPatchesMergeOnKey"],
            "merged_at": datetime(2026, 4, 1),
        },
        {
            "pr_number": 3,
            "title": "Touch the file",
            "matched_symbols": [],
            "matched_paths": [SRC],
            "matched_patterns": ["applyPatchesMergeOnKey"],
            "added_patterns": [],
            "merged_at": datetime(2026, 4, 9),
        },
    ]
    ranked = bi.rank_candidates(
        rows, frames=["loadDataParts"], paths=[SRC], limit=10, patterns=["applyPatchesMergeOnKey"]
    )
    assert [c.pr_number for c in ranked] == [2, 1, 3]
    assert ranked[0].score == 3.5 and ranked[1].score == 2.25 and ranked[2].score == 2.0
    assert (
        ranked[0].as_dict()["added_patterns"] == ["applyPatchesMergeOnKey"]
        and ranked[1].kind == "meta"
    )


@pytest.mark.asyncio
async def test_issue_text_supplies_patterns_and_paths_only_stays_file_scoped(wired) -> None:
    _, use = wired
    issue = {
        "repo": REPO,
        "number": 100,
        "is_pr": 0,
        "reproduces_on": ["26.4"],
        "clean_on": [],
        "affected_versions": [],
        "introduced_commit": "",
        "primary_component": "MergeTree",
        "labels": [],
        "bisect_outcome": "",
        "title": "LOGICAL_ERROR in applyPatchesMergeOnKey",
    }
    src = use(_Source(issue=issue, prs=[]))
    src.text = "LOGICAL_ERROR in applyPatchesMergeOnKey\nSetting apply_patch_parts=1 triggers it."
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            paths=[SRC],
            issue_number=100,
            patterns=["apply_patch_parts"],
        )
    )
    assert out.patterns == ["apply_patch_parts", "applyPatchesMergeOnKey", "LOGICAL_ERROR"]
    assert out.issue["title"].startswith("LOGICAL_ERROR") and out.scope == "directory"
    assert src.pr_calls[0]["patterns"] == out.patterns
    # a bare file list with nothing to rank inside a wider set stays file-scoped
    use(_Source(prs=[]))
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            paths=[SRC],
            bad_version="26.4",
        )
    )
    assert out.scope == "file" and out.patterns == []


@pytest.mark.asyncio
async def test_a_caused_by_edge_names_the_culprit_before_any_scan(wired) -> None:
    _, use = wired
    issue = {
        "repo": REPO,
        "number": 100,
        "is_pr": 0,
        "reproduces_on": ["26.4"],
        "clean_on": ["26.2"],
        "affected_versions": [],
        "introduced_commit": "",
        "primary_component": "MergeTree",
        "labels": [],
        "bisect_outcome": "",
    }
    src = _Source(
        issue=issue,
        prs=[
            {
                "pr_number": 9,
                "title": "unrelated",
                "author": "x",
                "closed_at": None,
                "minor_version": "26.3",
                "affected_file_paths": [SRC],
                "matched_symbols": [],
            }
        ],
    )
    src.edges = [
        {
            "src_kind": "github_issue",
            "src_ref": f"{REPO}#100",
            "rel_type": "caused_by",
            "dst_kind": "github_pr",
            "dst_ref": f"{REPO}#77",
            "direction": "out",
        }
    ]
    src.pr_rows = {
        77: {
            "repo": REPO,
            "number": 77,
            "is_pr": 1,
            "title": "Refactor part loading",
            "author": "alice",
            "minor_version": "26.3",
            "has_test": 0,
            "url": "u",
        }
    }
    use(src)
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            symbols=["DB::MergeTreeData::loadDataParts"],
            issue_number=100,
        )
    )
    # The graph names the PR; the scan is not run; the fact carries the edge
    assert out.verdict == "graph_named" and out.causes == ["caused_by_edge"]
    # the graph read carries the bisect budget: a cold first read on a fresh colour
    # must not fall through to `unknown`
    assert src.edge_budgets[-1] == 6000
    assert out.named_by_graph == [77] and src.pr_calls == []
    assert (
        out.candidates[0]["pr_number"] == 77 and out.candidates[0]["named_by"] == "caused_by_edge"
    )
    assert (
        out.candidates[0]["title"] == "Refactor part loading"
        and out.candidates[0]["has_test"] is False
    )
    assert out.issue["caused_by"] == [77]  # the fact carries the edge as a number
    assert out.versions_searched == ["26.3", "26.4", "26.5"]  # the range is still reported

    # a recorded commit still wins the verdict; the graph rides along in the fact
    issue["introduced_commit"] = "abc123"
    use(src)
    out = await h.handle_code_bisect_candidates(
        CodeBisectCandidatesInput(
            org=ORG,
            namespace=NS,
            symbols=["DB::MergeTreeData::loadDataParts"],
            issue_number=100,
        )
    )
    assert out.verdict == "already_bisected" and out.named_by_graph == []


def test_a_candidate_says_whether_its_diff_touched_one_of_the_querys_paths() -> None:
    """A caller's file-touch rule needs no GitHub round trip: `matched_paths`
    are the query paths (hub paths dropped before the scan) the PR's diff
    changed, and `touches_affected` is that as one boolean."""
    from server.tracker.domain import bisect as bi

    touched = bi.Candidate(
        pr_number=111379,
        title="Fix misreporting of datalake catalog errors",
        author="a",
        merged_at="2026-07-29 10:00:00",
        minor_version="26.8",
        matched_paths=["src/Databases/DataLake/RestCatalog.cpp"],
    )
    untouched = bi.Candidate(
        pr_number=113505,
        title="Other change",
        author="b",
        merged_at="2026-08-05 10:00:00",
        minor_version="26.8",
    )
    assert touched.as_dict()["touches_affected"] is True
    assert touched.as_dict()["matched_paths"] == ["src/Databases/DataLake/RestCatalog.cpp"]
    assert (
        untouched.as_dict()["touches_affected"] is False
        and untouched.as_dict()["matched_paths"] == []
    )


@pytest.mark.asyncio
async def test_an_unindexed_member_resolves_through_its_owner() -> None:
    """A frame member with no symbol row (`KeyCondition::cloneDAGWithInversionPushDown`)
    falls back to the owner class's files and says so. A member that resolves needs no
    fallback; a bare name has no owner; an owner without rows stays empty."""

    class _R:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def symbols_by_name(self, org, ns, name, *, limit=50):
            self.calls.append(name)
            table = {
                "KeyCondition": [
                    {"path": "src/Storages/MergeTree/KeyCondition.cpp"},
                    {"path": "src/Storages/MergeTree/KeyCondition.h"},
                    {"path": "tests/queries/0_stateless/03239_nan_with_fill.sql"},
                ],
                "Range::invertRange": [{"path": "src/Core/Range.cpp"}],
            }
            return table.get(name, [])

    repo = _R()
    out = await h._paths_for_symbols(
        repo,
        "clickhouse",
        "code-clickhouse",
        [
            "KeyCondition::cloneDAGWithInversionPushDown",
            "Range::invertRange",
            "Nowhere::gone",
            "bareName",
        ],
    )
    by = {r["symbol"]: r for r in out}
    fallen = by["KeyCondition::cloneDAGWithInversionPushDown"]
    assert fallen["paths"] == [
        "src/Storages/MergeTree/KeyCondition.cpp",
        "src/Storages/MergeTree/KeyCondition.h",
    ]
    assert fallen["via"] == "owner" and fallen["owner"] == "KeyCondition"
    assert by["Range::invertRange"] == {
        "symbol": "Range::invertRange",
        "paths": ["src/Core/Range.cpp"],
    }
    assert by["Nowhere::gone"]["paths"] == [] and "via" not in by["Nowhere::gone"]
    assert by["bareName"]["paths"] == [] and "via" not in by["bareName"]
    # the owner is asked only when the member found nothing
    assert (
        repo.calls.count("KeyCondition") == 1
        and "Range" not in repo.calls
        and "Nowhere" in repo.calls
    )
