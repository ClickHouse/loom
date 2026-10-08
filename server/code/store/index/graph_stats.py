"""The weekly graph-stat pass's tables: symbol fan-in and definition sites,
loom.code_symbol_stats, loom.code_unit_risk, and the published summaries
of both.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, TypedDict

from server.code.store.index.base import CodeRepoBase
from server.code.store.index.sql import _gather_batched
from server.code.store.index.symbol_rank import SYMBOL_NOISE_PATH_RE

logger = logging.getLogger(__name__)


def summary_stamp(at: datetime | None) -> str:
    """The `symbol_stats_at` / `unit_risk_at` wire form of a table's newest
    `computed_at`: ISO-8601 with an offset, '' when the table is empty."""
    if at is None:
        return ""
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).isoformat()


def parse_published_summary(row: Any) -> tuple[int, str] | None:
    """A `symbol_stats_summary` / `unit_risk_summary`
    state row as (rows, at); None for a missing row or one that does not
    parse — the caller then counts and publishes."""
    if not isinstance(row, dict):
        return None
    try:
        parsed = json.loads(str(row.get("value") or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or "n" not in parsed:
        return None
    try:
        n = int(parsed["n"])
    except (TypeError, ValueError):
        return None
    return n, str(parsed.get("at") or "")


def parse_summary_stamp(stamp: str) -> datetime | None:
    """A published summary's `at` back as a datetime; None for '' (an empty
    table) or a stamp that does not parse."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


_GRAPH_STATE_KEYS = ("symbol_stats_summary", "unit_risk_summary", "test_map_summary", "git_head")


def _stats_summaries(raw: dict[str, str]) -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    for key in ("symbol_stats_summary", "unit_risk_summary"):
        if key in raw and (parsed := parse_published_summary({"value": raw[key]})) is not None:
            out[key] = parsed
    return out


class LastTestMapRun(TypedDict):
    """When the test-map leg last ran and wrote no row, at which commit, and the
    namespace's `git_head` to compare it with."""

    test_map_ran_at: datetime | None
    test_map_ran_commit: str
    git_head: str


class GraphStatsState(LastTestMapRun):
    """What the weekly graph-stats pass has left for one namespace (see
    `graph_stats_state`)."""

    files: bool
    ranks: bool
    test_map: bool
    symbol_stats_rows: int
    symbol_stats_at: datetime | None
    unit_risk_rows: int
    unit_risk_at: datetime | None


def _test_map_ran(raw: dict[str, str]) -> LastTestMapRun:
    """`test_map_ran_at` / `test_map_ran_commit` from a `test_map_summary`
    row with no rows written (None / '' otherwise), and `git_head`."""
    ran_at: datetime | None = None
    commit = ""
    parsed = parse_published_summary({"value": raw.get("test_map_summary", "")})
    if parsed is not None and parsed[0] == 0:
        ran_at = parse_summary_stamp(parsed[1])
        try:
            commit = str(json.loads(raw["test_map_summary"]).get("commit") or "")
        except (TypeError, ValueError, AttributeError):
            ran_at = None
    return LastTestMapRun(
        test_map_ran_at=ran_at, test_map_ran_commit=commit, git_head=raw.get("git_head", "")
    )


class GraphStatsMixin(CodeRepoBase):
    """Symbol stats, unit risk and their published summaries."""

    async def symbol_fan_in(self, org: str, ns: str) -> list[dict[str, Any]]:
        """ONE aggregation over live `calls` edges per namespace: for every
        called symbol, its live in-edge count, the same count with call sites
        under noise paths (tests/, examples/, fuzzers, ci, contrib, vendor)
        excluded, and the distinct caller files. Liveness is argMax(version)
        over the edge key, as edges_for_symbols does; plain WHERE (module
        header). Reads 6 of 15 code_edges columns once a week. Symbols outside
        the repo are dropped by the caller (is_external_symbol), not here."""
        return await self._ch.query_all(
            """
            SELECT to_symbol_id AS symbol_id,
                   max(to_q) AS qualified_name,
                   count() AS fan_in_calls,
                   countIf(NOT match(path, {noise:String})) AS fan_in_nontest,
                   uniqExact(path) AS caller_files
            FROM (
                SELECT to_symbol_id, from_symbol_id, path, line,
                       argMax(tombstoned_at IS NULL, version) AS is_live,
                       argMax(to_qualified_name, version) AS to_q
                FROM loom.code_edges
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND edge_kind = 'calls'
                GROUP BY tier, to_symbol_id, from_symbol_id, path, line
                HAVING is_live = 1
            )
            GROUP BY to_symbol_id
            """,
            {"org": org, "ns": ns, "noise": SYMBOL_NOISE_PATH_RE},
        )

    async def symbol_definition_sites(self, org: str, ns: str) -> dict[str, tuple[int, int, str]]:
        """symbol_id -> (defined_files, is_test, primary path) over live
        code_symbols. is_test = every defining path is a noise path; the
        primary path is the first non-noise defining path (else the first)."""
        rows = await self._ch.query_all(
            """
            SELECT symbol_id, uniqExact(path) AS defined_files,
                   min(match(path, {noise:String})) AS is_test,
                   argMin(path, (match(path, {noise:String}), path)) AS def_path
            FROM (
                SELECT symbol_id, argMax(path, version) AS path,
                       argMax(tombstoned_at IS NULL, version) AS is_live
                FROM loom.code_symbols
                WHERE org = {org:String} AND namespace = {ns:String}
                GROUP BY tier, qualified_name, symbol_id
                HAVING is_live = 1
            )
            GROUP BY symbol_id
            """,
            {"org": org, "ns": ns, "noise": SYMBOL_NOISE_PATH_RE},
        )
        return {
            str(r["symbol_id"]): (int(r["defined_files"]), int(r["is_test"]), str(r["def_path"]))
            for r in rows
        }

    async def unit_risk_rows(
        self, org: str, ns: str, unit_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Point read on the ORDER BY: unit_id -> precomputed risk row
        (churn, amp, tested, callees JSON, computed_at) for the ids that have
        one. 200 ids per statement, a handful of granules each; the caller
        decides freshness from `computed_at`."""
        out: dict[str, dict[str, Any]] = {}
        ids = list(dict.fromkeys(u for u in unit_ids if u))

        async def read(batch: list[Any]) -> list[dict[str, Any]]:
            return await self._ch.query_all(
                """
                SELECT unit_id, path, churn, amp, tested, callees, computed_at
                FROM loom.code_unit_risk FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND unit_id IN {ids:Array(String)}
                """,
                {"org": org, "ns": ns, "ids": batch},
            )

        for rows in await _gather_batched(ids, 200, read):
            for r in rows:
                out[str(r["unit_id"])] = r
        return out

    async def live_symbol_stats_ids(self, org: str, ns: str) -> set[str]:
        """Symbols the previous pass left with callers — the ones a new pass
        must re-emit at zero when they no longer have any."""
        rows = await self._ch.query_all(
            """
            SELECT symbol_id FROM loom.code_symbol_stats FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND fan_in_calls > 0
            """,
            {"org": org, "ns": ns},
        )
        return {str(r["symbol_id"]) for r in rows}

    async def symbol_stats(
        self, org: str, ns: str, symbol_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Point read on the ORDER BY: symbol_id -> stats row for the ids that
        have callers. 200 ids per statement (a handful of granules each)."""
        out: dict[str, dict[str, Any]] = {}
        ids = list(dict.fromkeys(s for s in symbol_ids if s))

        async def read(batch: list[Any]) -> list[dict[str, Any]]:
            return await self._ch.query_all(
                """
                SELECT symbol_id, qualified_name, path, fan_in_calls, fan_in_nontest,
                       fan_in_name, caller_files, defined_files, is_test
                FROM loom.code_symbol_stats FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND symbol_id IN {ids:Array(String)} AND fan_in_calls > 0
                """,
                {"org": org, "ns": ns, "ids": batch},
            )

        for rows in await _gather_batched(ids, 200, read):
            for r in rows:
                out[str(r["symbol_id"])] = r
        return out

    async def top_files_by_callers(
        self, org: str, ns: str, *, path_prefix: str = "", limit: int = 40
    ) -> list[dict[str, Any]]:
        """The files other files call into most (non-test
        call sites, from the weekly stats), live files only — the repo map's
        second centrality for a language whose imports the include graph
        does not carry (a Python tree's pagerank is 0 on every file)."""
        where_prefix = "AND startsWith(path, {prefix:String})" if path_prefix else ""
        return await self._ch.query_all(
            f"""
            SELECT path, max(cf) AS callers, sum(fi) AS calls
            FROM (
              -- a symbol the compiler tier resolved also has a name-resolved
              -- row (json.dumps calls land on a local `dumps`): the smaller wins
              SELECT path, qualified_name,
                     min(caller_files) AS cf, min(fan_in_nontest) AS fi
              FROM loom.code_symbol_stats FINAL
              WHERE org = {{org:String}} AND namespace = {{ns:String}}
                AND is_test = 0 AND fan_in_nontest > 0 AND path != '' {where_prefix}
                AND path IN (
                  SELECT scope_path FROM loom.code_summaries FINAL
                  WHERE org = {{org:String}} AND namespace = {{ns:String}}
                    AND scope_type = 'file' AND tombstoned_at IS NULL
                )
              GROUP BY path, qualified_name
            )
            GROUP BY path
            ORDER BY callers DESC, calls DESC, path
            LIMIT {{limit:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "limit": int(limit),
                **({"prefix": path_prefix} if path_prefix else {}),
            },
        )

    async def top_symbol_stats(
        self,
        org: str,
        ns: str,
        *,
        path_prefix: str = "",
        limit: int = 10,
        include_tests: bool = False,
    ) -> list[dict[str, Any]]:
        """The most-called symbols defined under `path_prefix` by non-test
        call sites, one row per qualified name (overloads and tiers collapse
        to the most-called). Scans this namespace's rows of the stats table
        only — about 100 bytes a symbol."""
        where_prefix = "AND startsWith(path, {prefix:String})" if path_prefix else ""
        where_tests = "" if include_tests else "AND is_test = 0"
        return await self._ch.query_all(
            f"""
            SELECT symbol_id, qualified_name, path, fan_in_calls, fan_in_nontest,
                   fan_in_name, caller_files, defined_files, is_test
            FROM loom.code_symbol_stats FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND fan_in_nontest > 0 {where_prefix} {where_tests}
            ORDER BY fan_in_nontest DESC, qualified_name, symbol_id
            LIMIT 1 BY qualified_name
            LIMIT {{limit:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "limit": int(limit),
                **({"prefix": path_prefix} if path_prefix else {}),
            },
        )

    async def symbol_stats_summary(self, org: str, ns: str) -> tuple[int, datetime | None]:
        """How many live stats rows with callers the weekly
        pass has left for one namespace and the newest pass stamp (None when
        the table is empty) — what the pass publishes as
        `symbol_stats_summary` for `code.index_status` and the first-pass
        check, which read the published row, not this count. Scans the
        namespace's own rows of the stats table (about 100 bytes a symbol)."""
        row = await self._ch.query_one(
            """
            SELECT count() AS n, max(computed_at) AS newest_at
            FROM loom.code_symbol_stats FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND fan_in_calls > 0
            """,
            {"org": org, "ns": ns},
        )
        n = int((row or {}).get("n") or 0)
        at = (row or {}).get("newest_at") if n > 0 else None
        return n, (at if isinstance(at, datetime) else None)

    async def graph_stats_state(self, org: str, ns: str) -> GraphStatsState:
        """What the weekly pass has left in its target tables
        for one namespace — `files` (live indexed files exist), `ranks` (a
        file row with a PageRank), `test_map` (a live map row), and the two
        stats summaries. Existence reads (`LIMIT 1`, plain WHERE) and the
        summaries the pass published as `code_index_state` rows, no counts,
        so the check at supervisor start costs a few granules per table.

        `symbol_stats_rows` / `symbol_stats_at` decide "due": they come from
        the published `symbol_stats_summary` row, and only a namespace with
        no row yet (a pass never finished there) reads the stats table —
        plain WHERE, not FINAL: a stats row with callers exists and its
        newest stamp is the last pass's. `unit_risk_rows` / `unit_risk_at`
        are for operators (not a due condition) and come from the published
        `unit_risk_summary` row alone — (0, None) until one is published.
        `test_map_ran_at` / `test_map_ran_commit` are set when the test-map
        leg last ran and wrote no row (the published `test_map_summary`),
        next to the namespace's `git_head`, so an empty test map that is the
        namespace's answer is told from one never built."""

        async def present(sql: str) -> bool:
            return await self._ch.query_one(sql, {"org": org, "ns": ns}) is not None

        files = await present(
            """
            SELECT 1 AS present FROM loom.code_files
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
            LIMIT 1
            """
        )
        ranks = await present(
            """
            SELECT 1 AS present FROM loom.code_summaries
            WHERE org = {org:String} AND namespace = {ns:String}
              AND scope_type = 'file' AND pagerank > 0 AND tombstoned_at IS NULL
            LIMIT 1
            """
        )
        test_map = await present(
            """
            SELECT 1 AS present FROM loom.code_test_map
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
            LIMIT 1
            """
        )
        raw = await self.published_graph_state(org, ns)
        published = _stats_summaries(raw)
        stats = published.get("symbol_stats_summary")
        if stats is None:
            rows, at = await self.symbol_stats_newest(org, ns)
        else:
            rows, at = stats[0], parse_summary_stamp(stats[1])
        risk = published.get("unit_risk_summary") or (0, "")
        return GraphStatsState(
            files=files,
            ranks=ranks,
            test_map=test_map,
            symbol_stats_rows=rows,
            symbol_stats_at=at,
            unit_risk_rows=risk[0],
            unit_risk_at=parse_summary_stamp(risk[1]),
            **_test_map_ran(raw),
        )

    async def published_graph_state(self, org: str, ns: str) -> dict[str, str]:
        """The state rows the due check reads for one namespace, as key ->
        raw value: the `symbol_stats_summary` / `unit_risk_summary` /
        `test_map_summary` rows the pass published and the ingest
        watermark `git_head`. One point read on the state table's ORDER BY."""
        rows = await self._ch.query_all(
            """
            SELECT state_key, state_value AS value FROM loom.code_index_state FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND state_key IN {keys:Array(String)}
            """,
            {"org": org, "ns": ns, "keys": list(_GRAPH_STATE_KEYS)},
        )
        return {str(r["state_key"]): str(r.get("value") or "") for r in rows}

    async def published_stats_summaries(self, org: str, ns: str) -> dict[str, tuple[int, str]]:
        """The `symbol_stats_summary` / `unit_risk_summary` state rows the
        pass published for one namespace, as key -> (rows, at); a key with no
        row, or one that does not parse, is absent."""
        raw = await self.published_graph_state(org, ns)
        return _stats_summaries(raw)

    async def publish_test_map_summary(
        self, org: str, ns: str, n: int, at: str, commit: str
    ) -> None:
        """The `test_map_summary` state row: how many test-map rows the leg
        wrote, when, and at which ingested commit. A leg that ran and found
        no tests leaves `n` 0 here, which is what tells the due check that
        an empty test map is the namespace's answer, not a leg never run."""
        await self.set_state(
            org, ns, "test_map_summary", json.dumps({"n": int(n), "at": at, "commit": commit})
        )

    async def symbol_stats_newest(self, org: str, ns: str) -> tuple[int, datetime | None]:
        """The due check's fallback for a namespace with no published
        summary: whether a stats row with callers exists (1 or 0, not a
        count) and the newest stamp among them. Plain WHERE: FINAL would
        merge every sort-key column to answer what `max(computed_at)` over
        the stored versions already says — every row of a pass carries the
        pass's stamp, so the newest one is the last pass's."""
        row = await self._ch.query_one(
            """
            SELECT max(computed_at) AS newest_at FROM loom.code_symbol_stats
            WHERE org = {org:String} AND namespace = {ns:String} AND fan_in_calls > 0
            HAVING count() > 0
            """,
            {"org": org, "ns": ns},
        )
        at = (row or {}).get("newest_at")
        if not isinstance(at, datetime):
            return 0, None
        return 1, at

    async def unit_risk_summary(self, org: str, ns: str) -> tuple[int, datetime | None]:
        """Live precomputed risk rows for one namespace and
        the newest stamp (None when the table is empty) — the two
        `code.index_status` fields that show the weekly leg's progress."""
        row = await self._ch.query_one(
            """
            SELECT count() AS n, max(computed_at) AS newest_at
            FROM loom.code_unit_risk FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            """,
            {"org": org, "ns": ns},
        )
        n = int((row or {}).get("n") or 0)
        at = (row or {}).get("newest_at") if n > 0 else None
        return n, (at if isinstance(at, datetime) else None)

    async def publish_summary(self, org: str, ns: str, key: str, n: int, at: str) -> None:
        """One `code_index_state` row holding what a
        stats table has for the namespace — `{"n": rows, "at": newest stamp}`
        — so `code.index_status` reads a row instead of recounting
        `code_symbol_stats` and `code_unit_risk` under FINAL on every open."""
        await self.set_state(org, ns, key, json.dumps({"n": int(n), "at": at}))

    async def refresh_stats_summaries(
        self, org: str, ns: str, *, unchanged: frozenset[str] = frozenset()
    ) -> dict[str, tuple[int, str]]:
        """What the weekly pass left in its two stats
        tables for one namespace, read once and published as
        `symbol_stats_summary` / `unit_risk_summary` state rows — the pass
        that wrote the tables is the authority on what they hold. A key in
        `unchanged` names a table the leg wrote no row to: its published
        row is reused as it stands, with no count, and only a key with no
        published row yet is counted. A read that fails publishes nothing
        for its key. Returns what it published or reused."""
        out: dict[str, tuple[int, str]] = {}
        published: dict[str, tuple[int, str]] = {}
        if unchanged:
            try:
                published = await self.published_stats_summaries(org, ns)
            except Exception as e:  # noqa: BLE001 — unknown published rows: count
                logger.debug("refresh_stats_summaries: state read failed for %s: %s", ns, e)
        reads = (
            ("symbol_stats_summary", self.symbol_stats_summary),
            ("unit_risk_summary", self.unit_risk_summary),
        )
        for key, read in reads:
            if key in unchanged and key in published:
                out[key] = published[key]
                continue
            try:
                n, at = await read(org, ns)
            except Exception as e:  # noqa: BLE001 — the other key still publishes
                logger.debug("refresh_stats_summaries: %s unavailable for %s: %s", key, ns, e)
                continue
            stamp = summary_stamp(at)
            await self.publish_summary(org, ns, key, int(n), stamp)
            out[key] = (int(n), stamp)
        return out

    async def unit_risk_path_stamps(self, org: str, ns: str) -> dict[str, datetime]:
        """Path -> newest `computed_at` of its precomputed risk
        rows, in one statement on the (org, namespace) prefix — what the
        weekly leg reads first so a pass cut by its budget resumes with the
        paths it has not written and a healthy pass rewrites only the rows
        that would go stale before the next one. No FINAL: the newest stamp
        over every version is the same row FINAL would keep."""
        rows = await self._ch.query_all(
            """
            SELECT path, max(computed_at) AS newest_at
            FROM loom.code_unit_risk
            WHERE org = {org:String} AND namespace = {ns:String}
            GROUP BY path
            """,
            {"org": org, "ns": ns},
        )
        out: dict[str, datetime] = {}
        for r in rows:
            at = r.get("newest_at")
            if isinstance(at, datetime):
                out[str(r["path"])] = at.replace(tzinfo=None) if at.tzinfo else at
        return out

    async def related_tests_for_paths(self, org: str, ns: str, paths: list[str]) -> list[str]:
        """The tests the test map ties to `paths`, structural evidence (path convention,
        include graph, measured coverage) first, then co-occurrence weighted by how many of
        the paths a test is tied to. Harness config, CI scripts, oracles, fuzzers and
        `.reference` files are never related tests: PR co-occurrence alone drags them in
        beside the one real test."""
        rows = await self._ch.query_all(
            """
            SELECT test_path,
                   maxIf(1, method IN ('path_convention', 'include_graph', 'coverage'))
                       AS structural,
                   uniqExact(src_path) AS linked_files,
                   sum(confidence) AS weight
            FROM loom.code_test_map FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND src_path IN {paths:Array(String)} AND tombstoned_at IS NULL
              AND NOT startsWith(test_path, 'tests/config/')
              AND NOT startsWith(test_path, 'tests/ci/')
              AND position(test_path, 'casa_del_dolor') = 0
              AND position(test_path, 'fuzz') = 0
              AND position(test_path, '/configs/') = 0
              AND test_path != 'tests/clickhouse-test'
              AND NOT endsWith(test_path, '.reference')
            GROUP BY test_path
            HAVING structural = 1 OR weight >= 1.0
            ORDER BY structural DESC, linked_files DESC, weight DESC, test_path
            LIMIT 20
            """,
            {"org": org, "ns": ns, "paths": paths},
        )
        return [str(r["test_path"]) for r in rows]
