"""Measured coverage → test map: the import's planning, writes and retirements, and
the coverage claim's reading of it (the `executed` tier and the span intersection).

The source is a stub keyed on the statement it is asked; no network."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from server.code.app import coverage as app_cov
from server.code.app.index import coverage_import as ci
from server.code.domain.index import coverage_claim as cc
from server.code.domain.index import coverage_runs as cr


@pytest.fixture(autouse=True)
def _registry_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The code CLI runs only once the registry has been read."""
    monkeypatch.setattr("server.code.app.index.repo_registry.registry_read", lambda: True)


pytestmark = pytest.mark.usefixtures("stub_model_command")

# ---------------------------------------------------------------- pure logic


def test_ranges_merge_sorted_and_adjacent() -> None:
    assert cr.merge_ranges([[10, 12], [1, 3], [4, 5], [11, 20], [0, 2], [9, 8]]) == [
        (1, 5),
        (10, 20),
    ]


def test_intersection_is_inclusive() -> None:
    assert cr.intersects([(487, 491)], 491, 500)
    assert not cr.intersects([(487, 491)], 492, 500)


def test_specificity_falls_with_the_tests_sharing_the_line() -> None:
    assert cr.specificity(1) == 1.0
    assert cr.specificity(200) == pytest.approx(0.303, abs=1e-3)
    assert cr.specificity(10) > cr.specificity(100)


@pytest.mark.parametrize(
    ("name", "check", "want"),
    [
        (
            "05234_map_key_lookup_nan.sql",
            "Stateless tests (x, 1/8)",
            "tests/queries/0_stateless/05234_map_key_lookup_nan.sql",
        ),
        ("00001_hits.sql", "Stateless tests (x, 1/8)", "tests/queries/1_stateful/00001_hits.sql"),
        (
            "test_storage_s3/test.py",
            "Integration tests (x, 1/8)",
            "tests/integration/test_storage_s3/test.py",
        ),
        (
            "test_storage_s3/test.py::test_put",
            "Integration tests (x, 1/8)",
            "tests/integration/test_storage_s3/test.py",
        ),
        (
            "00800_merge_join.gen.sql",
            "Stateless tests (x, 1/8)",
            "tests/queries/0_stateless/00800_merge_join.sql.j2",
        ),
        ("99999_gone.sql", "Stateless tests (x, 1/8)", ""),
    ],
)
def test_a_run_test_name_maps_to_its_live_test_file(name: str, check: str, want: str) -> None:
    live = {
        "tests/queries/0_stateless/05234_map_key_lookup_nan.sql",
        "tests/queries/1_stateful/00001_hits.sql",
        "tests/integration/test_storage_s3/test.py",
        "tests/queries/0_stateless/00800_merge_join.sql.j2",
    }
    assert cr.resolve_test_path(name, check, live) == want


def test_evidence_round_trips_and_caps_its_ranges() -> None:
    many = [(i * 10, i * 10 + (i % 3)) for i in range(1, 400)]
    ev = cr.pair_evidence("abc", many, 999)
    data = json.loads(ev)
    assert data["commit"] == "abc" and data["specific_lines"] == 999
    assert len(data["lines"]) == cr.MAX_EVIDENCE_RANGES
    assert cr.evidence_ranges(ev) == sorted(cr.evidence_ranges(ev))
    assert cr.evidence_ranges("not json") == []


def test_measured_execution_tells_executed_from_not() -> None:
    files = {"src/a.cpp": [(10, 20), (480, 486)]}
    hit = cc.measured_execution({"src/a.cpp": [(15, 30)]}, files)
    assert hit["measured"] and hit["executed"] and hit["hits"] == {"src/a.cpp": [[15, 20]]}
    miss = cc.measured_execution({"src/a.cpp": [(487, 491)]}, files)
    assert miss["measured"] and not miss["executed"]
    assert cc.measured_execution({"src/b.cpp": [(1, 5)]}, files)["measured"] is False


def test_executed_is_the_strongest_tier() -> None:
    assert cc.tier_of_method("coverage") == cc.EXECUTED
    assert cc.TIER_ORDER[0] == cc.EXECUTED and cc.EXECUTED in cc.STRONG_TIERS
    assert cc.coverage_verdict([{"method": "coverage"}], control_ok=True) == cc.COVERED_BY
    assert cc.epistemic_of([{"method": "coverage"}]) == "exact"


def test_measured_unexecuted_turns_weak_evidence_into_uncovered() -> None:
    weak = [{"method": "path_convention"}]
    assert cc.coverage_verdict(weak, control_ok=True) == cc.WEAK_EVIDENCE
    assert cc.coverage_verdict(weak, control_ok=True, measured_unexecuted=True) == cc.UNCOVERED
    strong = [{"method": "pr_cooccurrence"}]
    assert cc.coverage_verdict(strong, control_ok=True, measured_unexecuted=True) == cc.COVERED_BY
    assert cc.coverage_verdict(weak, control_ok=False, measured_unexecuted=True) == cc.UNKNOWN


def test_coverage_rows_count_only_where_they_meet_the_span() -> None:
    rows = [
        {
            "src_path": "src/a.cpp",
            "test_path": "t1",
            "confidence": 0.9,
            "evidence": cr.pair_evidence("c", [(10, 12)], 3),
        },
        {
            "src_path": "src/a.cpp",
            "test_path": "t2",
            "confidence": 0.5,
            "evidence": cr.pair_evidence("c", [(100, 120)], 21),
        },
    ]
    got = app_cov.executed_rows(rows, {"src/a.cpp": [(11, 50)]})
    assert [r["test_path"] for r in got] == ["t1"]
    assert got[0]["method"] == "coverage" and "lines 11-12" in got[0]["evidence"]
    # a path subject (no spans) counts every row
    assert len(app_cov.executed_rows(rows, None)) == 2


def test_subject_spans_from_symbol_rows() -> None:
    rows = [
        {"path": "src/a.cpp", "start_line": 480, "end_line": 495},
        {"path": "src/a.h", "start_line": 7, "end_line": 0},
        {"path": "", "start_line": 1, "end_line": 2},
    ]
    assert app_cov.subject_spans(rows) == {"src/a.cpp": [(480, 495)], "src/a.h": [(7, 7)]}


# ---------------------------------------------------------------- the import


class StubSource:
    def __init__(
        self,
        *,
        pairs: list[dict[str, Any]],
        files: list[dict[str, Any]],
        fail_shard: int | None = None,
    ) -> None:
        self.pairs, self.files, self.fail_shard = pairs, files, fail_shard
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def rows(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if "FROM checks_coverage_lines\nWHERE check_start_time > now()" in sql:
            return [
                {"check_name": "Stateless tests (cov, 1/2)", "run_at": "2026-01-02 06:00:00"},
                {"check_name": "Integration tests (cov, 1/2)", "run_at": "2026-01-02 05:30:00"},
                {"check_name": "Stateless tests (cov, 2/2)", "run_at": "2025-12-20 06:00:00"},
            ]
        if "FROM checks\n" in sql:
            return [
                {"check_name": "Stateless tests (cov, 1/2)", "commit_sha": "c0ffee"},
                {"check_name": "Integration tests (cov, 1/2)", "commit_sha": "c0ffee"},
            ]
        if params.get("shard") == self.fail_shard:
            raise RuntimeError("memory limit")
        if params.get("shard") != 0:
            return []
        return self.pairs if "min(t)" in sql else self.files


class StubRepo:
    def __init__(self, *, live: set[str], pairs: set[tuple[str, str]], files: set[str]) -> None:
        self.live, self.pairs, self.files = live, pairs, files
        self.inserted: dict[str, list[dict[str, Any]]] = {}
        self.state: list[tuple[str, str]] = []

    async def live_file_hashes(self, org: str, ns: str) -> dict[str, str]:
        return {p: "h" for p in self.live}

    async def live_coverage_pairs(self, org: str, ns: str) -> set[tuple[str, str]]:
        return set(self.pairs)

    async def live_coverage_files(self, org: str, ns: str) -> set[str]:
        return set(self.files)

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.setdefault(table, []).extend(rows)

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        self.state.append((key, value))


LIVE = {
    "src/IO/copy.cpp",
    "src/IO/read.cpp",
    "tests/queries/0_stateless/01234_copy.sql",
    "tests/integration/test_backup/test.py",
}
PAIRS = [
    {
        "file": "src/IO/copy.cpp",
        "test_name": "01234_copy.sql",
        "check_name": "Stateless tests (cov, 1/2)",
        "ranges": [[10, 12], [13, 20]],
        "min_tests": 3,
        "specific_lines": 11,
    },
    {
        "file": "src/IO/copy.cpp",
        "test_name": "test_backup/test.py",
        "check_name": "Integration tests (cov, 1/2)",
        "ranges": [[30, 31]],
        "min_tests": 150,
        "specific_lines": 2,
    },
    {
        "file": "src/IO/copy.cpp",
        "test_name": "99999_unknown.sql",
        "check_name": "Stateless tests (cov, 1/2)",
        "ranges": [[1, 2]],
        "min_tests": 1,
        "specific_lines": 2,
    },
    {
        "file": "contrib/x.cpp",
        "test_name": "01234_copy.sql",
        "check_name": "Stateless tests (cov, 1/2)",
        "ranges": [[1, 2]],
        "min_tests": 1,
        "specific_lines": 2,
    },
]
FILES = [
    {"file": "src/IO/copy.cpp", "ranges": [[1, 40], [50, 60]], "tests": 12},
    {"file": "contrib/x.cpp", "ranges": [[1, 2]], "tests": 1},
]


async def _run(
    repo: StubRepo, src: StubSource, *, dry_run: bool = False
) -> ci.CoverageImportResult:
    return await ci.import_namespace(
        repo,
        src,
        "acme",
        "code-x",
        shards=2,
        cap=200,
        pattern="%cov%",
        window_hours=6,
        dry_run=dry_run,
    )


@pytest.mark.asyncio
async def test_the_newest_run_keeps_its_window_and_majority_commit() -> None:
    run = await ci.latest_run(StubSource(pairs=[], files=[]), pattern="%cov%", window_hours=6)
    assert run is not None and run.commit == "c0ffee"
    assert [n for n, _ in run.jobs] == [
        "Integration tests (cov, 1/2)",
        "Stateless tests (cov, 1/2)",
    ]
    assert run.run_at == datetime(2026, 1, 2, 6, 0, 0)


@pytest.mark.asyncio
async def test_an_import_writes_mapped_pairs_files_and_retires_the_rest() -> None:
    repo = StubRepo(
        live=LIVE,
        pairs={
            ("src/IO/copy.cpp", "tests/queries/0_stateless/01234_copy.sql"),
            ("src/IO/read.cpp", "tests/queries/0_stateless/01234_copy.sql"),
        },
        files={"src/IO/read.cpp"},
    )
    res = await _run(repo, StubSource(pairs=PAIRS, files=FILES))

    assert (res.commit, res.shards_read, res.pairs, res.files) == ("c0ffee", 2, 2, 1)
    assert (res.unmapped_tests, res.unindexed_files) == (1, 1)
    assert (res.retired_pairs, res.retired_files) == (1, 1)
    tm = repo.inserted["code_test_map"]
    live_rows = [r for r in tm if r["tombstoned_at"] is None]
    assert {r["test_path"] for r in live_rows} == {
        "tests/queries/0_stateless/01234_copy.sql",
        "tests/integration/test_backup/test.py",
    }
    assert all(r["method"] == "coverage" for r in tm)
    sql_row = next(r for r in live_rows if r["test_path"].endswith("01234_copy.sql"))
    assert json.loads(sql_row["evidence"])["lines"] == [[10, 20]]
    assert (
        sql_row["confidence"]
        > next(r for r in live_rows if r["test_path"].endswith("test.py"))["confidence"]
    )
    retired = [r for r in tm if r["tombstoned_at"] is not None]
    assert [(r["src_path"], r["test_path"]) for r in retired] == [
        ("src/IO/read.cpp", "tests/queries/0_stateless/01234_copy.sql")
    ]
    files = repo.inserted["code_coverage_files"]
    assert [(f["path"], f["tombstoned_at"] is None) for f in files] == [
        ("src/IO/copy.cpp", True),
        ("src/IO/read.cpp", False),
    ]
    assert json.loads(files[0]["lines"]) == [[1, 40], [50, 60]]
    assert repo.state == [("coverage_import", "c0ffee")]


@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing() -> None:
    repo = StubRepo(live=LIVE, pairs=set(), files=set())
    res = await _run(repo, StubSource(pairs=PAIRS, files=FILES), dry_run=True)
    assert res.dry_run and res.pairs == 2 and res.files == 1
    assert repo.inserted == {} and repo.state == []


@pytest.mark.asyncio
async def test_a_failed_slice_retires_nothing() -> None:
    repo = StubRepo(
        live=LIVE,
        pairs={("src/IO/read.cpp", "tests/queries/0_stateless/01234_copy.sql")},
        files={"src/IO/read.cpp"},
    )
    res = await _run(repo, StubSource(pairs=PAIRS, files=FILES, fail_shard=1))
    assert res.errors and res.shards_read == 1
    assert res.retired_pairs == 0 and res.retired_files == 0
    assert all(r["tombstoned_at"] is None for r in repo.inserted["code_test_map"])


@pytest.mark.asyncio
async def test_no_run_is_a_skip() -> None:
    class Empty(StubSource):
        async def rows(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            return []

    repo = StubRepo(live=LIVE, pairs=set(), files=set())
    res = await _run(repo, Empty(pairs=[], files=[]))
    assert res.skipped == "no_run" and repo.inserted == {}


@pytest.mark.asyncio
async def test_no_source_configured_is_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_coverage_source_url", "")
    res = await ci.import_from_settings(
        "acme", "code-x", repo=StubRepo(live=set(), pairs=set(), files=set())
    )
    assert res.skipped == "no_source"


def test_shard_statements_bind_every_value() -> None:
    run = ci.CoverageRun(
        commit="c", jobs=[("Stateless tests (cov, 1/2)", "2026-01-02 06:00:00")], run_at=None
    )
    params = ci._shard_params(run, 64, 7, 200)
    for sql in (ci._PAIRS_SQL, ci._FILES_SQL):
        assert "cityHash64(file) % {shards:UInt32} = {shard:UInt32}" in sql
        assert "'" not in sql.replace("''", "")
    assert params["jobs"] == [("Stateless tests (cov, 1/2)", "2026-01-02 06:00:00")]


def test_source_parameters_render_as_clickhouse_literals() -> None:
    from server.clients.coverage_source import _param_value

    assert _param_value([("a'b", "2026-01-02 06:00:00")]) == "[('a\\'b','2026-01-02 06:00:00')]"
    assert _param_value(["x", "y"]) == "['x','y']"
    assert _param_value(7) == "7"


class _FilesRepo:
    def __init__(self, files: dict[str, dict[str, Any]]) -> None:
        self.files = files

    async def coverage_files_for(self, org: str, ns: str, paths: list[str]) -> dict[str, Any]:
        return {p: f for p, f in self.files.items() if p in paths}


@pytest.mark.asyncio
async def test_the_claim_reports_a_span_no_test_executed() -> None:
    """The catch arm at lines 487-491 of a file whose run executed 470-486 and 492-499:
    measured, not executed."""
    repo: Any = _FilesRepo(
        {"src/IO/copy.cpp": {"commit_sha": "c0ffee", "lines": "[[470,486],[492,499]]"}}
    )
    out = await app_cov.measured_for(
        repo, "acme", "code-x", ["src/IO/copy.cpp"], {"src/IO/copy.cpp": [(487, 491)]}
    )
    assert out["measured"] and not out["executed"] and out["commit"] == "c0ffee"
    whole = await app_cov.measured_for(repo, "acme", "code-x", ["src/IO/copy.cpp"], None)
    assert whole["executed"]
    assert await app_cov.measured_for(repo, "acme", "code-x", ["src/other.cpp"], None) == {}


# ---------------------------------------------------------------- settings, worker, CLI


@pytest.mark.asyncio
async def test_the_settings_drive_the_import(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_coverage_shards", 2)
    monkeypatch.setattr(settings, "code_coverage_specific_max_tests", 50)
    monkeypatch.setattr(settings, "code_coverage_check_pattern", "%cov%")
    monkeypatch.setattr(settings, "code_coverage_run_window_hours", 6)
    src = StubSource(pairs=PAIRS, files=FILES)
    repo = StubRepo(live=LIVE, pairs=set(), files=set())
    res = await ci.import_from_settings("acme", "code-x", dry_run=True, repo=repo, src=src)
    assert res.shards_read == 2
    shard_calls = [p for _, p in src.calls if "shard" in p]
    assert {p["shards"] for p in shard_calls} == {2} and {p["cap"] for p in shard_calls} == {50}
    assert src.calls[0][1]["pattern"] == "%cov%"


def test_the_source_is_built_from_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.clients.coverage_source import HttpCoverageSource, source_from_settings
    from server.settings import settings

    monkeypatch.setattr(settings, "code_coverage_source_url", "https://coverage.example")
    monkeypatch.setattr(settings, "code_coverage_source_user", "reader")
    monkeypatch.setattr(settings, "code_coverage_source_password", "pw")
    monkeypatch.setattr(settings, "code_coverage_source_database", "ci")
    monkeypatch.setattr(settings, "code_coverage_source_timeout_s", 12.0)
    src = source_from_settings()
    assert isinstance(src, HttpCoverageSource)
    assert src._url == "https://coverage.example/" and src._database == "ci"
    assert src._auth is not None and src._timeout.read == 12.0


def test_the_worker_row_is_off_by_default_and_daily() -> None:
    from server.settings import Settings
    from server.workers.registry import _WORKERS

    row = next(r for r in _WORKERS if r.name == "code_coverage_import")
    assert row.enabled == "code_coverage_import_enabled"
    assert row.interval == "code_coverage_import_interval_seconds"
    assert row.max_pass_setting == "code_coverage_import_max_pass_seconds"
    defaults = Settings.model_fields
    assert defaults["code_coverage_import_enabled"].default is False
    assert defaults["code_coverage_import_interval_seconds"].default == 86400.0


@pytest.mark.asyncio
async def test_a_pass_imports_only_the_named_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from server.code.domain.index import repos as repos_mod
    from server.code.workers import coverage_import as worker
    from server.settings import settings

    monkeypatch.setattr(settings, "code_coverage_namespaces", "code-x, code-z")
    monkeypatch.setattr(
        repos_mod,
        "configured_repos",
        lambda: [SimpleNamespace(org="acme", namespace=n) for n in ("code-x", "code-y")],
    )
    seen: list[str] = []

    async def fake(org: str, ns: str) -> ci.CoverageImportResult:
        seen.append(ns)
        return ci.CoverageImportResult(namespace=ns, pairs=3)

    monkeypatch.setattr(worker, "import_from_settings", fake)
    out = await worker.run_pass()
    assert seen == ["code-x"] and out.results[0].pairs == 3 and not out.errors


def test_the_cli_dry_run_prints_counts(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    import server.cli.app as cli
    from server.core import ch

    class _Client:
        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(ch, "get_client", lambda: _Client())
    asked: list[tuple[str, bool]] = []

    async def fake(org: str, ns: str, *, dry_run: bool = False) -> ci.CoverageImportResult:
        asked.append((ns, dry_run))
        return ci.CoverageImportResult(namespace=ns, dry_run=dry_run, commit="c0ffee", pairs=7)

    monkeypatch.setattr(ci, "import_from_settings", fake)
    rc = cli.main(
        ["code", "coverage-import", "--org", "acme", "--namespace", "code-x", "--dry-run"]
    )
    assert rc == 0 and asked == [("code-x", True)]
    out = capsys.readouterr().out
    assert "coverage-import (dry run)" in out and "pairs=7" in out
