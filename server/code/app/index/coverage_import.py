"""Import a CI's per-test line coverage into the test map.

One import, per namespace whose repository the source's runs cover:

1. pick the newest complete master run: the newest job of every coverage
   check within `code_coverage_run_window_hours` of the newest one, each
   job's commit read from the source's `checks` table, the majority commit
   kept;
2. read the run in `code_coverage_shards` slices by file hash — per (file,
   test) the line ranges only few tests (`code_coverage_specific_max_tests`)
   reach, and per file every executed range;
3. keep the files and tests the namespace's index holds live, and write one
   `coverage` test-map row per (file, test) and one `code_coverage_files`
   row per file;
4. retire the live `coverage` rows and files the run no longer has — only
   when every slice was read, so a failed slice never empties the map.

A dry run reads and plans and writes nothing.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.clients.coverage_source import CoverageSource
from server.code.domain.index import coverage_runs as cr
from server.code.store.index.rows import build_row
from server.core.datetimes import naive_utc_now

logger = logging.getLogger("loom.code.coverage_import")

_RUN_JOBS_SQL = """
SELECT check_name, max(check_start_time) AS run_at
FROM checks_coverage_lines
WHERE check_start_time > now() - INTERVAL {days:UInt32} DAY
  AND check_name LIKE {pattern:String}
GROUP BY check_name
"""

_JOB_COMMITS_SQL = """
SELECT check_name, argMax(commit_sha, check_start_time) AS commit_sha
FROM checks
WHERE check_name IN {names:Array(String)} AND pull_request_number = 0
  AND check_start_time BETWEEN {lo:DateTime} AND {hi:DateTime}
GROUP BY check_name
"""

_LINES_CTE = """
WITH lines AS (
    SELECT file, line_start, line_end, test_name, check_name
    FROM checks_coverage_lines
    WHERE check_start_time IN {times:Array(DateTime)}
      AND (check_name, check_start_time) IN {jobs:Array(Tuple(String, DateTime))}
      AND cityHash64(file) % {shards:UInt32} = {shard:UInt32}
)
"""

_PAIRS_SQL = (
    _LINES_CTE
    + """,
per_line AS (
    SELECT file, line_start, uniqExact(test_name) AS t FROM lines GROUP BY file, line_start
)
SELECT file, test_name, any(check_name) AS check_name,
       groupUniqArray((line_start, line_end)) AS ranges,
       min(t) AS min_tests, uniqExact(line_start) AS specific_lines
FROM lines INNER JOIN per_line USING (file, line_start)
WHERE t <= {cap:UInt32}
GROUP BY file, test_name
"""
)

_FILES_SQL = (
    _LINES_CTE
    + """
SELECT file, groupUniqArray((line_start, line_end)) AS ranges, uniqExact(test_name) AS tests
FROM lines
GROUP BY file
"""
)


@dataclass
class CoverageRun:
    commit: str
    jobs: list[tuple[str, str]]  # (check_name, run_at as 'YYYY-MM-DD hh:mm:ss')
    run_at: datetime | None


@dataclass
class CoverageImportResult:
    namespace: str
    commit: str = ""
    jobs: int = 0
    shards_read: int = 0
    pairs: int = 0
    files: int = 0
    retired_pairs: int = 0
    retired_files: int = 0
    unmapped_tests: int = 0
    unindexed_files: int = 0
    dry_run: bool = False
    skipped: str = ""
    errors: list[str] = field(default_factory=list)


def _as_dt(v: Any) -> datetime:
    return v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", ""))


async def latest_run(src: CoverageSource, *, pattern: str, window_hours: int) -> CoverageRun | None:
    """The newest complete master run, None when the source has none."""
    jobs = await src.rows(_RUN_JOBS_SQL, {"days": 7, "pattern": pattern})
    if not jobs:
        return None
    newest = max(_as_dt(j["run_at"]) for j in jobs)
    recent = [
        (str(j["check_name"]), _as_dt(j["run_at"]))
        for j in jobs
        if _as_dt(j["run_at"]) >= newest - timedelta(hours=window_hours)
    ]
    commits = await src.rows(
        _JOB_COMMITS_SQL,
        {
            "names": [n for n, _ in recent],
            "lo": (newest - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            "hi": newest.strftime("%Y-%m-%d %H:%M:%S"),
        },
    )
    by_name = {str(c["check_name"]): str(c["commit_sha"]) for c in commits if c.get("commit_sha")}
    if not by_name:
        return None
    commit = Counter(by_name.values()).most_common(1)[0][0]
    kept = sorted(
        (n, t.strftime("%Y-%m-%d %H:%M:%S")) for n, t in recent if by_name.get(n) == commit
    )
    return CoverageRun(commit=commit, jobs=kept, run_at=newest)


def _shard_params(run: CoverageRun, shards: int, shard: int, cap: int) -> dict[str, Any]:
    return {
        "times": sorted({t for _, t in run.jobs}),
        "jobs": [tuple(j) for j in run.jobs],
        "shards": shards,
        "shard": shard,
        "cap": cap,
    }


def plan_pairs(
    plan: cr.ImportPlan, rows: list[dict[str, Any]], live: set[str], commit: str
) -> None:
    """Fold one slice's (file, test) rows into the plan: the files and tests the index
    holds live, their specific ranges merged and scored."""
    for r in rows:
        src = str(r.get("file") or "")
        if src not in live:
            plan.unindexed_files += 1
            continue
        name, check = str(r.get("test_name") or ""), str(r.get("check_name") or "")
        test = cr.resolve_test_path(name, check, live)
        if not test:
            plan.unmapped_tests.add(name)
            continue
        ranges = cr.merge_ranges(r.get("ranges") or [])
        if not ranges:
            continue
        plan.pairs[(src, test)] = {
            "confidence": cr.specificity(int(r.get("min_tests") or 1)),
            "evidence": cr.pair_evidence(commit, ranges, int(r.get("specific_lines") or 0)),
        }


def plan_files(plan: cr.ImportPlan, rows: list[dict[str, Any]], live: set[str]) -> None:
    """Fold one slice's per-file executed ranges into the plan."""
    for r in rows:
        path = str(r.get("file") or "")
        if path not in live:
            continue
        ranges = cr.merge_ranges(r.get("ranges") or [])
        plan.files[path] = {"lines": ranges, "tests": int(r.get("tests") or 0)}


def plan_rows(
    plan: cr.ImportPlan, org: str, ns: str, run: CoverageRun
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The test-map and coverage-file rows the plan writes, retirements as tombstones."""
    now = naive_utc_now()
    test_rows = [
        build_row(
            "code_test_map",
            org=org,
            namespace=ns,
            src_path=src,
            method="coverage",
            test_path=test,
            confidence=v["confidence"],
            evidence=v["evidence"],
        )
        for (src, test), v in sorted(plan.pairs.items())
    ]
    test_rows += [
        build_row(
            "code_test_map",
            org=org,
            namespace=ns,
            src_path=src,
            method="coverage",
            test_path=test,
            tombstoned_at=now,
        )
        for src, test in sorted(plan.retire_pairs)
    ]
    file_rows = [
        build_row(
            "code_coverage_files",
            org=org,
            namespace=ns,
            path=path,
            commit_sha=run.commit,
            run_at=run.run_at,
            lines=json.dumps([list(r) for r in v["lines"]], separators=(",", ":")),
            tests=v["tests"],
        )
        for path, v in sorted(plan.files.items())
    ]
    file_rows += [
        build_row("code_coverage_files", org=org, namespace=ns, path=path, tombstoned_at=now)
        for path in sorted(plan.retire_files)
    ]
    return test_rows, file_rows


async def _read_shards(
    src: CoverageSource,
    run: CoverageRun,
    plan: cr.ImportPlan,
    live: set[str],
    *,
    shards: int,
    cap: int,
    result: CoverageImportResult,
) -> bool:
    """Read every slice into the plan; False when one failed (the plan is partial)."""
    for shard in range(shards):
        params = _shard_params(run, shards, shard, cap)
        try:
            plan_pairs(plan, await src.rows(_PAIRS_SQL, params), live, run.commit)
            plan_files(plan, await src.rows(_FILES_SQL, params), live)
        except Exception as e:  # noqa: BLE001 — one failed slice stops the import, not the worker
            result.errors.append(f"shard {shard}: {type(e).__name__}: {e}")
            return False
        result.shards_read += 1
    return True


async def import_namespace(
    repo: Any,
    src: CoverageSource,
    org: str,
    ns: str,
    *,
    shards: int,
    cap: int,
    pattern: str,
    window_hours: int,
    dry_run: bool = False,
) -> CoverageImportResult:
    """One namespace's import (module docstring). Writes nothing when `dry_run`."""
    result = CoverageImportResult(namespace=ns, dry_run=dry_run)
    run = await latest_run(src, pattern=pattern, window_hours=window_hours)
    if run is None:
        result.skipped = "no_run"
        return result
    result.commit, result.jobs = run.commit, len(run.jobs)
    live = set(await repo.live_file_hashes(org, ns))
    plan = cr.ImportPlan()
    complete = await _read_shards(src, run, plan, live, shards=shards, cap=cap, result=result)
    if complete:
        cr.retirements(
            plan, await repo.live_coverage_pairs(org, ns), await repo.live_coverage_files(org, ns)
        )
    _count(result, plan)
    if dry_run or not plan.pairs and not plan.files:
        return result
    await _write(repo, org, ns, run, plan, result)
    return result


def _count(result: CoverageImportResult, plan: cr.ImportPlan) -> None:
    result.pairs, result.files = len(plan.pairs), len(plan.files)
    result.retired_pairs, result.retired_files = len(plan.retire_pairs), len(plan.retire_files)
    result.unmapped_tests, result.unindexed_files = len(plan.unmapped_tests), plan.unindexed_files


async def _write(
    repo: Any,
    org: str,
    ns: str,
    run: CoverageRun,
    plan: cr.ImportPlan,
    result: CoverageImportResult,
) -> None:
    test_rows, file_rows = plan_rows(plan, org, ns, run)
    for start in range(0, len(test_rows), 5000):
        await repo.insert_rows("code_test_map", test_rows[start : start + 5000])
    for start in range(0, len(file_rows), 5000):
        await repo.insert_rows("code_coverage_files", file_rows[start : start + 5000])
    await repo.set_state(
        org,
        ns,
        "coverage_import",
        run.commit,
        detail=json.dumps(
            {
                "run_at": run.run_at.isoformat() if run.run_at else "",
                "jobs": result.jobs,
                "pairs": result.pairs,
                "files": result.files,
                "retired_pairs": result.retired_pairs,
                "retired_files": result.retired_files,
                "complete": not result.errors,
            }
        ),
    )
    logger.info(
        "coverage_import: %s at %s: %d pairs, %d files, %d/%d retired, %d errors",
        ns,
        run.commit[:12],
        result.pairs,
        result.files,
        result.retired_pairs,
        result.retired_files,
        len(result.errors),
    )


async def import_from_settings(
    org: str, ns: str, *, dry_run: bool = False, repo: Any = None, src: Any = None
) -> CoverageImportResult:
    """`import_namespace` with the `code_coverage_*` settings and the configured source."""
    from server.clients.coverage_source import source_from_settings
    from server.code.store.index import CodeRepo
    from server.settings import settings

    source = src if src is not None else source_from_settings()
    if source is None:
        return CoverageImportResult(namespace=ns, dry_run=dry_run, skipped="no_source")
    return await import_namespace(
        repo if repo is not None else CodeRepo(),
        source,
        org,
        ns,
        shards=settings.code_coverage_shards,
        cap=settings.code_coverage_specific_max_tests,
        pattern=settings.code_coverage_check_pattern,
        window_hours=settings.code_coverage_run_window_hours,
        dry_run=dry_run,
    )


__all__ = [
    "CoverageImportResult",
    "CoverageRun",
    "import_from_settings",
    "import_namespace",
    "latest_run",
    "plan_files",
    "plan_pairs",
    "plan_rows",
]
