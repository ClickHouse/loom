"""Measured coverage: the test map's `coverage` rows and `loom.code_coverage_files`.

An import writes one test-map row per (source file, test) a run's per-test line
coverage ties together and one `code_coverage_files` row per source file with
the lines any test executed; the coverage claim reads both back.
"""

from __future__ import annotations

from typing import Any

from server.code.store.index.base import CodeRepoBase
from server.code.store.index.sql import _batched


class CoverageMixin(CodeRepoBase):
    async def live_coverage_pairs(self, org: str, ns: str) -> set[tuple[str, str]]:
        """Every live (src_path, test_path) `coverage` row of the namespace —
        what the next import tombstones when its run no longer has the pair."""
        rows = await self._ch.query_all(
            """
            SELECT src_path, test_path FROM loom.code_test_map FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND method = 'coverage' AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns},
        )
        return {(str(r["src_path"]), str(r["test_path"])) for r in rows}

    async def live_coverage_files(self, org: str, ns: str) -> set[str]:
        """Every live `code_coverage_files` path of the namespace."""
        rows = await self._ch.query_all(
            """
            SELECT path FROM loom.code_coverage_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns},
        )
        return {str(r["path"]) for r in rows}

    async def coverage_rows_for(
        self, org: str, ns: str, paths: list[str], *, limit: int = 2000
    ) -> list[dict[str, Any]]:
        """The `coverage` test-map rows of the first 50 `paths`, most specific
        first (the order total): the claim filters them by the subject's span,
        so it reads them all rather than the flat strongest page."""
        if not paths:
            return []
        return await self._ch.query_all(
            """
            SELECT src_path, test_path, confidence, evidence
            FROM loom.code_test_map FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND src_path IN {paths:Array(String)} AND method = 'coverage'
              AND tombstoned_at IS NULL
            ORDER BY confidence DESC, src_path, test_path
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "paths": paths[:50], "limit": limit},
        )

    async def coverage_files_for(
        self, org: str, ns: str, paths: list[str]
    ) -> dict[str, dict[str, Any]]:
        """path -> its live `code_coverage_files` row (commit, run time, the
        executed ranges as JSON, the test count), for `paths` batched by 200."""
        out: dict[str, dict[str, Any]] = {}
        for batch in _batched(list(dict.fromkeys(p for p in paths if p)), 200):
            rows = await self._ch.query_all(
                """
                SELECT path, commit_sha, run_at, lines, tests
                FROM loom.code_coverage_files FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
                """,
                {"org": org, "ns": ns, "paths": batch},
            )
            for r in rows:
                out[str(r["path"])] = dict(r)
        return out
