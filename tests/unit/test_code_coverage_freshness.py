"""Coverage line numbers need the identity of the source the index parsed."""

from __future__ import annotations

import pytest

from server.code.api.models_testing import CodeCoverageClaimInput
from server.code.app import coverage as app
from server.code.domain.index import coverage_claim as cc
from server.code.domain.index import coverage_runs as cr

ORG, NS = "acme", "code-sample"
SRC = "src/sample.cpp"
TEST = f"{cc.STATELESS}01234_sample.sql"
pytestmark = pytest.mark.usefixtures("scalar_code_repo")


class CoverageRepo:
    def __init__(self, *, indexed="current", measured="old", head="current", lines="[[10,12]]"):
        self.indexed = indexed
        self.measured = measured
        self.head = head
        self.lines = lines
        self.span_commit = indexed

    async def coverage_files_for(self, org, ns, paths):
        assert (org, ns) == (ORG, NS)
        return {p: {"commit_sha": self.measured, "lines": self.lines} for p in paths}

    async def coverage_rows_for(self, org, ns, paths):
        assert (org, ns) == (ORG, NS)
        return [
            {
                "src_path": SRC,
                "test_path": TEST,
                "confidence": 1.0,
                "evidence": cr.pair_evidence(self.measured, [(10, 12)], 3),
            }
        ]

    async def get_state(self, org, ns, key):
        assert (org, ns, key) == (ORG, NS, "git_head")
        return self.head

    async def file_rows_for_paths(self, org, ns, paths):
        assert (org, ns) == (ORG, NS)
        return {p: {"indexed_commit": self.indexed} for p in paths}

    async def test_map_detail(self, org, ns, paths, *, limit):
        return []


async def run_claim(monkeypatch, repo, span, *, resolved_rows=None):
    async def control(*args):
        return True, "probe"

    async def resolve(*args):
        if resolved_rows is not None:
            return resolved_rows, ""
        return [
            {
                "path": SRC,
                "start_line": span[0],
                "end_line": span[1],
                "indexed_commit": repo.span_commit,
            }
        ], ""

    async def terms(*args):
        return {}

    async def empty(*args, **kwargs):
        return []

    monkeypatch.setattr(app, "CodeRepo", lambda: repo)
    monkeypatch.setattr(app, "require_code_intel", lambda: None)
    monkeypatch.setattr(app, "resolve_code_identity", lambda *args: (ORG, NS))
    monkeypatch.setattr(app, "schedule_code_trace", lambda **kwargs: None)
    monkeypatch.setattr(app, "positive_control", control)
    monkeypatch.setattr(app, "_resolve_subject", resolve)
    monkeypatch.setattr(app, "search_terms", terms)
    monkeypatch.setattr(app, "unit_tests_reaching", empty)
    return await app.handle_code_coverage_claim(
        CodeCoverageClaimInput(org=ORG, namespace=NS, symbol="sample")
    )


@pytest.mark.asyncio
async def test_changed_source_does_not_make_old_overlapping_lines_executed(monkeypatch):
    out = await run_claim(monkeypatch, CoverageRepo(), (10, 12))
    assert out.verdict == cc.UNKNOWN
    assert out.strongest_tier == cc.FILE_REFERENCE
    assert out.tests[0]["methods"] == ["coverage_history"]
    assert "unverified" in out.tests[0]["evidence"]
    assert out.measured["freshness"] == "unverified"
    assert not out.measured["measured"] and not out.measured["executed"]


@pytest.mark.asyncio
async def test_shifted_source_does_not_make_old_nonoverlapping_lines_uncovered(monkeypatch):
    out = await run_claim(monkeypatch, CoverageRepo(), (30, 32))
    assert out.verdict == cc.UNKNOWN
    assert out.tests[0]["test_path"] == TEST
    assert out.measured["hits"] == {}


@pytest.mark.asyncio
async def test_unchanged_file_keeps_verified_coverage_after_repository_moves(monkeypatch):
    repo = CoverageRepo(indexed="old", head="current")
    out = await run_claim(monkeypatch, repo, (10, 12))
    assert out.verdict == cc.COVERED_BY
    assert out.strongest_tier == cc.EXECUTED
    assert out.measured["freshness"] == "verified"
    assert out.measured["commit"] == "old"
    assert out.measured["executed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "indexed,measured,head", [("", "old", "current"), ("old", "", "current"), ("old", "old", "")]
)
async def test_missing_identity_cannot_verify_coverage(monkeypatch, indexed, measured, head):
    repo = CoverageRepo(indexed=indexed, measured=measured, head=head)
    out = await run_claim(monkeypatch, repo, (10, 12))
    assert out.verdict == cc.UNKNOWN
    assert out.measured["freshness"] == "unverified"


@pytest.mark.asyncio
async def test_current_nonexecution_remains_a_measured_negative(monkeypatch):
    repo = CoverageRepo(indexed="current", measured="current")
    out = await run_claim(monkeypatch, repo, (30, 32))
    assert out.verdict == cc.UNCOVERED
    assert out.measured["measured"] and not out.measured["executed"]


@pytest.mark.asyncio
async def test_mixed_commits_are_reported_per_file_without_inventing_one_commit():
    class MixedRepo(CoverageRepo):
        async def coverage_files_for(self, org, ns, paths):
            return {
                SRC: {"commit_sha": "old", "lines": "[[10,12]]"},
                "src/other.cpp": {"commit_sha": "current", "lines": "[]"},
            }

    out = await app.measured_for(MixedRepo(), ORG, NS, [SRC, "src/other.cpp"], None)
    assert out["commits"] == ["current", "old"]
    assert out["commit"] == ""
    assert out["freshness"] == "partial"
    assert out["paths"] == ["src/other.cpp"]
    assert out["unverified_paths"] == [SRC]


@pytest.mark.asyncio
async def test_pair_uses_its_own_commit_even_when_file_measurement_is_current(monkeypatch):
    class OldPairRepo(CoverageRepo):
        async def coverage_rows_for(self, org, ns, paths):
            rows = await super().coverage_rows_for(org, ns, paths)
            rows[0]["evidence"] = cr.pair_evidence("different", [(10, 12)], 3)
            return rows

    repo = OldPairRepo(indexed="current", measured="current")
    out = await run_claim(monkeypatch, repo, (10, 12))
    assert out.strongest_tier == cc.FILE_REFERENCE
    assert out.verdict == cc.UNKNOWN
    assert out.measured["freshness"] == "verified"


@pytest.mark.asyncio
async def test_missing_source_file_does_not_match_a_measurement(monkeypatch):
    class MissingRepo(CoverageRepo):
        async def file_rows_for_paths(self, org, ns, paths):
            return {}

    out = await run_claim(monkeypatch, MissingRepo(), (10, 12))
    assert out.verdict == cc.UNKNOWN
    assert out.measured["files"][SRC]["indexed_commit"] == ""


@pytest.mark.asyncio
async def test_unmeasured_source_prevents_a_complete_negative():
    class MissingMeasurementRepo(CoverageRepo):
        async def coverage_files_for(self, org, ns, paths):
            return {SRC: {"commit_sha": "current", "lines": "[]"}}

    out = await app.measured_for(MissingMeasurementRepo(), ORG, NS, [SRC, "src/other.cpp"], None)
    assert out["freshness"] == "partial"
    assert out["unmeasured_paths"] == ["src/other.cpp"]
    assert not out["executed"]


@pytest.mark.asyncio
async def test_independent_strong_evidence_still_establishes_coverage(monkeypatch):
    class StrongRepo(CoverageRepo):
        async def test_map_detail(self, org, ns, paths, *, limit):
            return [{"test_path": TEST, "method": "pr_cooccurrence", "confidence": 0.8}]

    out = await run_claim(monkeypatch, StrongRepo(), (10, 12))
    assert out.verdict == cc.COVERED_BY
    assert out.strongest_tier == cc.PR_COOCCURRENCE
    assert out.measured["freshness"] == "unverified"


@pytest.mark.asyncio
@pytest.mark.parametrize("span_commit", ["old", ""])
async def test_stale_or_unknown_symbol_identity_cannot_use_current_line_ranges(
    monkeypatch, span_commit
):
    repo = CoverageRepo(indexed="current", measured="current")
    repo.span_commit = span_commit
    out = await run_claim(monkeypatch, repo, (10, 12))
    assert out.verdict == cc.UNKNOWN
    assert out.strongest_tier == cc.FILE_REFERENCE
    assert not out.measured["measured"]


@pytest.mark.asyncio
@pytest.mark.parametrize("test_commit", [None, "current"])
async def test_deleted_or_changed_test_cannot_retain_an_exact_execution_claim(
    monkeypatch, test_commit
):
    class ChangedTestRepo(CoverageRepo):
        async def file_rows_for_paths(self, org, ns, paths):
            rows = await super().file_rows_for_paths(org, ns, paths)
            if TEST in rows:
                if test_commit is None:
                    rows.pop(TEST)
                else:
                    rows[TEST]["indexed_commit"] = test_commit
            return rows

    out = await run_claim(monkeypatch, ChangedTestRepo(indexed="old"), (10, 12))
    assert out.verdict == cc.UNKNOWN
    assert out.strongest_tier == cc.FILE_REFERENCE
    assert out.tests[0]["methods"] == ["coverage_history"]


@pytest.mark.asyncio
async def test_old_nonexecution_cannot_refute_tests_added_after_the_run(monkeypatch):
    repo = CoverageRepo(indexed="old", measured="old", head="current")
    out = await run_claim(monkeypatch, repo, (30, 32))
    assert out.verdict == cc.UNKNOWN
    assert out.measured["freshness"] == "verified"
    assert not out.measured["run_current"]


@pytest.mark.asyncio
async def test_symbol_file_association_without_span_identity_stays_historical():
    repo = CoverageRepo(indexed="current", measured="current")
    rows, _ = await app.claim_tests(repo, ORG, NS, "symbol", "sample", [SRC], limit=5, terms={})
    assert rows[0]["tier"] == cc.FILE_REFERENCE
    assert rows[0]["methods"] == ["coverage_history"]


@pytest.mark.asyncio
async def test_measured_execution_without_a_specific_test_is_not_uncovered(monkeypatch):
    class SharedLinesRepo(CoverageRepo):
        async def coverage_rows_for(self, org, ns, paths):
            return []

    repo = SharedLinesRepo(indexed="current", measured="current")
    out = await run_claim(monkeypatch, repo, (10, 12))
    assert out.measured["executed"] and out.measured["run_current"]
    assert out.verdict == cc.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("extent", [(0, 0), (10, 0), (12, 10)])
async def test_unknown_symbol_line_extent_cannot_become_whole_file_execution(monkeypatch, extent):
    repo = CoverageRepo(indexed="current", measured="current")
    out = await run_claim(monkeypatch, repo, extent)
    assert out.verdict == cc.UNKNOWN
    assert out.strongest_tier == cc.FILE_REFERENCE
    assert not out.measured["measured"]


@pytest.mark.asyncio
async def test_symbol_commit_without_a_line_span_is_only_a_file_association():
    repo = CoverageRepo(indexed="current", measured="current")
    rows, _ = await app.claim_tests(
        repo,
        ORG,
        NS,
        "symbol",
        "sample",
        [SRC],
        limit=5,
        terms={},
        span_commits={SRC: {"current"}},
    )
    assert rows[0]["tier"] == cc.FILE_REFERENCE


@pytest.mark.asyncio
async def test_one_unknown_definition_prevents_a_partial_symbol_extent_claim(monkeypatch):
    repo = CoverageRepo(indexed="current", measured="current")
    rows = [
        {"path": SRC, "indexed_commit": "current", "start_line": 10, "end_line": 12},
        {"path": SRC, "indexed_commit": "current", "start_line": 0, "end_line": 0},
    ]
    out = await run_claim(monkeypatch, repo, (10, 12), resolved_rows=rows)
    assert out.verdict == cc.UNKNOWN
    assert out.measured["freshness"] == "unverified"
