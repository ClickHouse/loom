"""What code.index_status and code.repos report: coverage and its gaps,
index errors, recent indexed commits, live row counts per table (cached
per process on the state fingerprint, and published as shared state), and
chunk embed coverage.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from server.code.domain.extract.lang_reference import UNEMBEDDED_BY_DESIGN
from server.code.domain.index.state import IndexState, StateEntry
from server.code.store.index.base import CodeRepoBase

logger = logging.getLogger(__name__)


# (org, ns) -> (state fingerprint, monotonic time, counts). See CodeRepo.table_counts.
_COUNTS_CACHE: dict[tuple[str, str], tuple[str, float, dict[str, int]]] = {}


# Every compiler tier's watermark moves the symbol and edge
# counts, not only the C++ and Python ones.
_FINGERPRINT_KEYS = (
    "git_head",
    "scip_commit",
    "scip_commit_py",
    "scip_commit_ts",
    "scip_commit_go",
    "pr_cursor",
)


# The counts row's own fingerprint leaves pr_cursor out — the
# PR-history cursor moves every few minutes and only code_pr_history's count
# follows it, which the max-age ceiling covers; busting seven scans on it
# would make every code.repos poll rescan the tables.
_COUNTS_KEYS = ("git_head", "scip_commit", "scip_commit_py", "scip_commit_ts", "scip_commit_go")


def parse_shared_coverage(
    row: StateEntry | None, fingerprint: str
) -> tuple[dict[str, Any], dict[str, int] | None] | None:
    """The published coverage when the row's
    fingerprint is the caller's; None otherwise (missing, stale, unparsable)."""
    if not row or str(row.get("detail") or "") != fingerprint:
        return None
    try:
        parsed = json.loads(row.get("value") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("cov"), dict):
        return None
    gaps = parsed.get("gaps")
    counts = {str(k): int(v) for k, v in gaps.items()} if isinstance(gaps, dict) else None
    return parsed["cov"], counts


def _json_time(o: Any) -> str:
    """JSON for the published rows: a datetime as ISO text, anything else as str."""
    iso = getattr(o, "isoformat", None)
    return iso() if callable(iso) else str(o)


def counts_fingerprint(state: IndexState) -> str:
    """The watermarks whose move changes the code_* row counts (not the PR
    cursor — see _COUNTS_KEYS)."""
    return "|".join(str((state.get(k) or {}).get("value") or "") for k in _COUNTS_KEYS)


def state_fingerprint(state: IndexState) -> str:
    """The watermarks that move when a writer changes row counts, joined into
    one comparable string. Any other state key (calibration, cursors of
    read-only jobs) is deliberately excluded so it cannot bust the cache."""
    return "|".join(str((state.get(k) or {}).get("value") or "") for k in _FINGERPRINT_KEYS)


class StatusMixin(CodeRepoBase):
    """Coverage, row counts and index health."""

    async def refresh_coverage(
        self, org: str, ns: str, *, fingerprint: str | None = None
    ) -> tuple[dict[str, Any], dict[str, int] | None]:
        """Compute `coverage` and `coverage_gaps` once
        and publish them as the shared `coverage` state row (value = JSON,
        detail = counts fingerprint), the way `refresh_table_counts` publishes
        the counts. The ingest pass that moved the rows calls this after its
        counts; a reader with no matching row calls it too, so the first cold
        open pays once for every process and colour. A cold computation reads
        about 1.2 GB and takes 4 s on a large repository; the row is a point
        read."""
        if fingerprint is None:
            fingerprint = counts_fingerprint(await self.all_state(org, ns))
        cov = await self.coverage(org, ns)
        gaps: dict[str, int] | None
        try:
            gaps = await self.coverage_gaps(org, ns)
        except Exception:  # noqa: BLE001 — the gap queries are additive
            gaps = None
        try:
            await self.set_state(
                org,
                ns,
                "coverage",
                json.dumps({"cov": cov, "gaps": gaps}, default=_json_time),
                detail=fingerprint,
            )
        except Exception:  # noqa: BLE001 — the answer stands without the row
            pass
        return cov, gaps

    async def coverage_gaps(self, org: str, ns: str) -> dict[str, int]:
        """Counts of what the index does not know (see CodeCoverageGaps).
        Two FINAL aggregates with anti-join subqueries; ~0.1 s each on the
        350k-symbol ClickHouse namespace. File denominators are the languages
        the symbol extractors cover (cpp, python): a .sql or .sh file with no
        symbols is not a gap, it is the design."""
        p = {"org": org, "ns": ns}
        fn = (
            await self._ch.query_one(
                """
            SELECT count() AS scanned,
                   countIf(symbol_id NOT IN (
                       SELECT to_symbol_id FROM loom.code_edges
                       WHERE org = {org:String} AND namespace = {ns:String}
                         AND edge_kind = 'calls' AND tombstoned_at IS NULL)) AS uncalled
            FROM loom.code_symbols FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
              AND kind IN ('function', 'method') AND tier = 'scip'
              AND NOT startsWith(path, 'tests/')
            """,
                p,
            )
            or {}
        )
        files = (
            await self._ch.query_one(
                """
            SELECT countIf(NOT startsWith(path, 'tests/')) AS source_files,
                   countIf(NOT startsWith(path, 'tests/') AND path NOT IN (
                       SELECT src_path FROM loom.code_test_map FINAL
                       WHERE org = {org:String} AND namespace = {ns:String}
                         AND tombstoned_at IS NULL)) AS untested,
                   count() AS code_files,
                   countIf(path NOT IN (
                       SELECT DISTINCT path FROM loom.code_symbols FINAL
                       WHERE org = {org:String} AND namespace = {ns:String}
                         AND tombstoned_at IS NULL)) AS no_symbols
            FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
              AND lang IN ('cpp', 'python')
            """,
                p,
            )
            or {}
        )
        return {
            "functions_scanned": int(fn.get("scanned") or 0),
            "functions_uncalled": int(fn.get("uncalled") or 0),
            "source_files": int(files.get("source_files") or 0),
            "source_files_untested": int(files.get("untested") or 0),
            "code_files": int(files.get("code_files") or 0),
            "code_files_no_symbols": int(files.get("no_symbols") or 0),
        }

    async def coverage(self, org: str, ns: str) -> dict[str, Any]:
        """Per-language + per-tier coverage with LAST-WRITE timestamps — the
        "when was each index done" answer. Languages come from code_files.lang;
        chunks/symbols join to it by path (rows whose file is not in
        code_files — generated headers outside the prefixes — land in '')."""
        p = {"org": org, "ns": ns}
        live_files = """
            SELECT path, lang FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
        """
        files = await self._ch.query_all(
            """
            SELECT lang, count() AS files, countIf(index_error != '') AS errored,
                   max(created_at) AS last_at, min(created_at) AS first_at
            FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
            GROUP BY lang
            """,
            p,
        )
        chunks = await self._ch.query_all(
            f"""
            SELECT f.lang AS lang, count() AS chunks,
                   countIf(c.embedding_dim > 0) AS embedded,
                   countIf(c.context_source = 'llm') AS described,
                   max(c.created_at) AS last_at
            FROM (
                SELECT path, embedding_dim, context_source, created_at
                FROM loom.code_chunks FINAL
                WHERE org = {{org:String}} AND namespace = {{ns:String}} AND tombstoned_at IS NULL
            ) AS c
            LEFT JOIN ({live_files}) AS f ON f.path = c.path
            GROUP BY lang
            """,
            p,
        )
        symbols = await self._ch.query_all(
            f"""
            SELECT f.lang AS lang, toString(s.tier) AS tier, count() AS symbols,
                   max(s.created_at) AS last_at
            FROM (
                SELECT path, tier, created_at FROM loom.code_symbols FINAL
                WHERE org = {{org:String}} AND namespace = {{ns:String}} AND tombstoned_at IS NULL
            ) AS s
            LEFT JOIN ({live_files}) AS f ON f.path = s.path
            GROUP BY lang, tier
            """,
            p,
        )
        edges = await self._ch.query_all(
            """
            SELECT toString(tier) AS tier, count() AS edges, max(created_at) AS last_at
            FROM loom.code_edges FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
            GROUP BY tier
            """,
            p,
        )
        misc = await self._ch.query_one(
            """
            SELECT
              (SELECT min(merged_at) FROM loom.code_pr_history FINAL
                 WHERE org = {org:String} AND namespace = {ns:String}
                   AND tombstoned_at IS NULL) AS pr_from,
              (SELECT max(merged_at) FROM loom.code_pr_history FINAL
                 WHERE org = {org:String} AND namespace = {ns:String}
                   AND tombstoned_at IS NULL) AS pr_to,
              (SELECT max(created_at) FROM loom.code_pr_history FINAL
                 WHERE org = {org:String} AND namespace = {ns:String}) AS pr_last_at,
              (SELECT max(created_at) FROM loom.code_context_packs FINAL
                 WHERE org = {org:String} AND namespace = {ns:String}
                   AND tombstoned_at IS NULL) AS pack_last_at,
              (SELECT max(created_at) FROM loom.code_summaries FINAL
                 WHERE org = {org:String} AND namespace = {ns:String}) AS summary_last_at
            """,
            p,
        )
        return {
            "files": files,
            "chunks": chunks,
            "symbols": symbols,
            "edges": edges,
            "misc": misc or {},
        }

    async def index_errors(
        self, org: str, ns: str, *, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        p = {"org": org, "ns": ns, "lim": limit}
        rows = await self._ch.query_all(
            """
            SELECT path, lang, index_error, indexed_commit, created_at
            FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND index_error != ''
            ORDER BY created_at DESC LIMIT {lim:UInt32}
            """,
            p,
        )
        total = await self._ch.query_one(
            """
            SELECT count() AS n FROM loom.code_files FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND index_error != ''
            """,
            p,
        )
        return rows, int((total or {}).get("n") or 0)

    async def recent_indexed_commits(
        self, org: str, ns: str, *, limit: int
    ) -> list[dict[str, Any]]:
        """Commits the fast tier wrote files at, newest first, with the PR a
        context pack ties them to (0 when none)."""
        return await self._ch.query_all(
            """
            SELECT f.indexed_commit AS commit, count() AS files,
                   max(f.created_at) AS ingested_at,
                   any(ifNull(p.pr_number, 0)) AS pr_number
            FROM (
                SELECT indexed_commit, created_at FROM loom.code_files FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tombstoned_at IS NULL AND indexed_commit != ''
            ) AS f
            LEFT JOIN (
                SELECT merge_commit_sha, pr_number FROM loom.code_context_packs FINAL
                WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
            ) AS p ON p.merge_commit_sha = f.indexed_commit
            GROUP BY commit ORDER BY ingested_at DESC LIMIT {lim:UInt32}
            """,
            {"org": org, "ns": ns, "lim": limit},
        )

    async def table_counts(
        self,
        org: str,
        ns: str,
        *,
        fingerprint: str | None = None,
        max_age_s: float = 600.0,
        shared_state: StateEntry | None = None,
    ) -> dict[str, int]:
        """Live row counts per code table for one namespace.

        `shared_state`: the namespace's `table_counts`
        state entry when the caller already holds its state (`all_state_many`),
        so the shared row is not read again.

        Change-driven, not call-driven. Seven `count() ... FINAL` scans per
        namespace are expensive: `code.repos` (the UI's repo panel) runs them
        for every configured namespace on every poll, and the code_edges scan
        alone reads millions of rows per call on a large repository. Counts only move when a writer
        moves a watermark, so the caller passes the namespace's state
        fingerprint (git_head, scip_commit, scip_commit_py, pr_cursor) and a
        cached answer is served while it matches, up to `max_age_s` (heals and
        tombstones that leave watermarks alone are covered by the ceiling).
        Process-local; no fingerprint = compute one from all_state.
        """
        if fingerprint is None:
            fingerprint = counts_fingerprint(await self.all_state(org, ns))
        key = (org, ns)
        hit = _COUNTS_CACHE.get(key)
        now = time.monotonic()
        if hit is not None and hit[0] == fingerprint and now - hit[1] < max_age_s:
            return dict(hit[2])
        # The process cache is one of N web workers and dies
        # with every colour; the shared row in code_index_state is written
        # by whoever last computed (the ingest pass after it moved the
        # watermark, or the first reader on a fresh namespace) and read here
        # as a point. A row whose fingerprint lags is still served — a count
        # a few minutes old beats a FINAL scan per table per poll —
        # and the worker refreshes it on its next pass. Only a namespace with
        # no row at all is scanned by a reader.
        shared = (
            self._parse_shared_counts(shared_state.get("value"))
            if shared_state is not None
            else await self._shared_counts(org, ns)
        )
        if shared is not None:
            _COUNTS_CACHE[key] = (fingerprint, now, dict(shared))
            return dict(shared)
        return await self.refresh_table_counts(org, ns, fingerprint=fingerprint)

    async def _shared_counts(self, org: str, ns: str) -> dict[str, int] | None:
        try:
            row = await self._ch.query_one(
                """
                SELECT state_value FROM loom.code_index_state FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND state_key = 'table_counts'
                LIMIT 1
                """,
                {"org": org, "ns": ns},
            )
        except Exception:  # noqa: BLE001 — fall back to the scan
            return None
        return self._parse_shared_counts((row or {}).get("state_value"))

    @staticmethod
    def _parse_shared_counts(raw: Any) -> dict[str, int] | None:
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict) or not parsed:
            return None
        return {str(k): int(v) for k, v in parsed.items()}

    async def refresh_table_counts(
        self, org: str, ns: str, *, fingerprint: str | None = None
    ) -> dict[str, int]:
        """Run the seven FINAL scans once and publish the answer as the shared
        `table_counts` row (value = JSON counts, detail = fingerprint) so
        every other process reads a point. Called by the ingest pass after
        it advanced a watermark, and by a reader that found no row."""
        if fingerprint is None:
            fingerprint = counts_fingerprint(await self.all_state(org, ns))
        out: dict[str, int] = {}
        for table in (
            "code_files",
            "code_symbols",
            "code_edges",
            "code_pr_history",
            "code_summaries",
            "code_context_packs",
        ):
            row = await self._ch.query_one(
                f"""
                SELECT count() AS n FROM loom.{table} FINAL
                WHERE org = {{org:String}} AND namespace = {{ns:String}}
                  AND tombstoned_at IS NULL
                """,
                {"org": org, "ns": ns},
            )
            out[table] = int(row["n"]) if row else 0
        # The chunk count carries its embedding coverage on the
        # same scan (embedded / embeddable), so `code.repos` can say
        # "embedding 41%" without a second read of the namespace's chunks.
        chunks = await self._chunk_embed_row(org, ns)
        out["code_chunks"] = chunks["n"]
        out["code_chunks_embedded"] = chunks["embedded"]
        out["code_chunks_embeddable"] = chunks["embeddable"]
        try:
            await self.set_state(org, ns, "table_counts", json.dumps(out), detail=fingerprint)
        except Exception:  # noqa: BLE001 — the local answer is still good
            logger.debug("code table_counts: shared row not written for %s/%s", org, ns)
        if len(_COUNTS_CACHE) > 256:
            _COUNTS_CACHE.clear()
        _COUNTS_CACHE[(org, ns)] = (fingerprint, time.monotonic(), dict(out))
        return out

    async def _chunk_embed_row(self, org: str, ns: str) -> dict[str, int]:
        """Live chunks of one namespace: `n` (all), `embedded` (a vector is
        stored) and `embeddable` (all but the reference fixtures that are
        unembedded by design). One aggregate scan of the
        UInt16 / String columns; the vector is not read."""
        row = await self._ch.query_one(
            """
            SELECT count() AS n,
                   countIf(embedding_dim > 0) AS embedded,
                   countIf(NOT startsWith(embed_last_error, {unembedded:String})) AS embeddable
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "unembedded": UNEMBEDDED_BY_DESIGN},
        )
        if not row:
            return {"n": 0, "embedded": 0, "embeddable": 0}
        return {k: int(row.get(k) or 0) for k in ("n", "embedded", "embeddable")}

    async def chunk_embed_coverage(self, org: str, ns: str) -> tuple[int, int]:
        """(embeddable, embedded) live chunks, uncached — the
        read the bootstrap uses to decide `embedding` vs `ready` and the
        ingest pass uses to move a building entry to ready. The polled
        surfaces read the same numbers through `table_counts`."""
        row = await self._chunk_embed_row(org, ns)
        return row["embeddable"], row["embedded"]
