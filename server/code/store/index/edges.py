"""Reads over loom.code_edges, loom.code_call_sites_unresolved and
loom.code_caller_guards: call edges in both directions, declined call
sites, caller guards, caller counts and include edges.
"""

from __future__ import annotations

from typing import Any

from server.code.store.index.base import CodeRepoBase
from server.code.store.index.sql import _batched

# The shape of a registration function's bare name, as the statement in
# `caller_counts` applies it; one source of truth with the
# Python predicate in server/code/domain/extract/registration.py.
REGISTRAR_NAME_RE = r"^(REGISTER_[A-Z0-9_]+|register[A-Z]\w*)$"


class EdgesMixin(CodeRepoBase):
    """Call edges, declined call sites and caller guards."""

    async def live_edges_for_path(
        self, org: str, ns: str, path: str, tier: str
    ) -> list[dict[str, Any]]:
        """Live edge rows at call-site granularity for one source path.
        No FINAL: the newest version per full key wins via argMax, and
        tombstoned keys are dropped in HAVING (RMT merges are only an
        optimization here, not a correctness dependency)."""
        # NOTE: org/namespace are injected app-side below, NOT selected as
        # `{org:String} AS org` — that alias SHADOWS the table column in
        # WHERE (making the tenant filter const-true), so tombstones from
        # other namespaces with later versions would win the argMax and
        # hide live rows.
        rows = await self._ch.query_all(
            """
            SELECT
                tier, edge_kind, to_symbol_id, from_symbol_id, path, line,
                argMax(from_qualified_name, version) AS from_qualified_name,
                argMax(to_qualified_name, version) AS to_qualified_name,
                argMax(confidence, version) AS confidence,
                argMax(indexed_commit, version) AS indexed_commit,
                argMax(created_at, version) AS created_at,
                -- NOT `AS version`: aliasing the aggregate to the source
                -- column name makes the argMax() references resolve as
                -- aggregate-in-aggregate -> ILLEGAL_AGGREGATION (the same
                -- gotcha documented in memory/app/facts.py).
                max(version) AS max_version,
                argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_edges
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String} AND tier = {tier:String}
            GROUP BY tier, edge_kind, to_symbol_id, from_symbol_id, path, line
            HAVING is_live = 1
            """,
            {"org": org, "ns": ns, "path": path, "tier": tier},
        )
        for r in rows:
            r["org"] = org
            r["namespace"] = ns
        return rows

    async def live_edges_for_paths(
        self, org: str, ns: str, paths: list[str], tier: str
    ) -> dict[str, list[dict[str, Any]]]:
        """`live_edges_for_path` for a round of paths in one statement per
        100: the same argMax/HAVING per full key, `path` in the GROUP BY, so
        each path's rows are exactly what its own read
        returns; routed back per path, every requested path present (an
        empty list for one with no live edge)."""
        out: dict[str, list[dict[str, Any]]] = {str(p): [] for p in paths}
        for batch in _batched(sorted(out), 100):
            rows = await self._ch.query_all(
                """
                SELECT
                    tier, edge_kind, to_symbol_id, from_symbol_id, path, line,
                    argMax(from_qualified_name, version) AS from_qualified_name,
                    argMax(to_qualified_name, version) AS to_qualified_name,
                    argMax(confidence, version) AS confidence,
                    argMax(indexed_commit, version) AS indexed_commit,
                    argMax(created_at, version) AS created_at,
                    max(version) AS max_version,
                    argMax(tombstoned_at IS NULL, version) AS is_live
                FROM loom.code_edges
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)} AND tier = {tier:String}
                GROUP BY tier, edge_kind, to_symbol_id, from_symbol_id, path, line
                HAVING is_live = 1
                """,
                {"org": org, "ns": ns, "paths": batch, "tier": tier},
            )
            for r in rows:
                r["org"] = org
                r["namespace"] = ns
                out.setdefault(str(r["path"]), []).append(r)
        return out

    async def live_call_misses_for_paths(
        self, org: str, ns: str, paths: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """`live_call_misses_for_path` for a round of paths in one statement
        per 100. `callee_name` leads the sort key, so a path read scans the
        namespace's slice whatever the path list holds (2.6 MB a statement);
        the round pays it once. Same argMax/HAVING per
        full key, `path` in the GROUP BY; every requested path present."""
        out: dict[str, list[dict[str, Any]]] = {str(p): [] for p in paths}
        for batch in _batched(sorted(out), 100):
            rows = await self._ch.query_all(
                """
                SELECT
                    callee_name, path, line, from_symbol_id,
                    argMax(from_qualified_name, version) AS from_qualified_name,
                    argMax(call_text, version) AS call_text,
                    argMax(disposition, version) AS disposition,
                    argMax(candidates, version) AS candidates,
                    argMax(indexed_commit, version) AS indexed_commit,
                    argMax(created_at, version) AS created_at,
                    max(version) AS max_version,
                    argMax(tombstoned_at IS NULL, version) AS is_live
                FROM loom.code_call_sites_unresolved
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)}
                GROUP BY callee_name, path, line, from_symbol_id
                HAVING is_live = 1
                """,
                {"org": org, "ns": ns, "paths": batch},
            )
            for r in rows:
                r["org"] = org
                r["namespace"] = ns
                out.setdefault(str(r["path"]), []).append(r)
        return out

    async def live_call_guards_for_paths(
        self, org: str, ns: str, paths: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """`live_call_guards_for_path` for a round of paths in one statement
        per 100; same shape, `path` already in the key."""
        out: dict[str, list[dict[str, Any]]] = {str(p): [] for p in paths}
        for batch in _batched(sorted(out), 100):
            rows = await self._ch.query_all(
                """
                SELECT
                    from_symbol_id, path,
                    argMax(guards, version) AS guards,
                    argMax(guard_kinds, version) AS guard_kinds,
                    argMax(sites, version) AS sites,
                    argMax(site_callees, version) AS site_callees,
                    argMax(site_guards, version) AS site_guards,
                    argMax(indexed_commit, version) AS indexed_commit,
                    argMax(created_at, version) AS created_at,
                    argMax(tombstoned_at IS NULL, version) AS is_live
                FROM loom.code_caller_guards
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)}
                GROUP BY from_symbol_id, path
                HAVING is_live = 1
                """,
                {"org": org, "ns": ns, "paths": batch},
            )
            for r in rows:
                r["org"] = org
                r["namespace"] = ns
                out.setdefault(str(r["path"]), []).append(r)
        return out

    async def has_live_scip_override_edges(self, org: str, ns: str) -> bool:
        """Does the compiler tier hold at least one LIVE `overrides` edge in the namespace?
        Same argMax/HAVING liveness per edge key as the readers above: a namespace whose
        override edges were all tombstoned answers False. Stops at the first live key."""
        row = await self._ch.query_one(
            """
            SELECT 1 AS one FROM (
                SELECT argMax(tombstoned_at IS NULL, version) AS is_live
                FROM loom.code_edges
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND edge_kind = 'overrides' AND tier = 'scip'
                GROUP BY tier, edge_kind, to_symbol_id, from_symbol_id, path, line
                HAVING is_live = 1
            )
            LIMIT 1
            """,
            {"org": org, "ns": ns},
        )
        return row is not None

    async def live_call_misses_for_path(self, org: str, ns: str, path: str) -> list[dict[str, Any]]:
        """Live declined-call rows for one source path (same argMax/HAVING
        discipline as live_edges_for_path — see the note there)."""
        rows = await self._ch.query_all(
            """
            SELECT
                callee_name, path, line, from_symbol_id,
                argMax(from_qualified_name, version) AS from_qualified_name,
                argMax(call_text, version) AS call_text,
                argMax(disposition, version) AS disposition,
                argMax(candidates, version) AS candidates,
                argMax(indexed_commit, version) AS indexed_commit,
                argMax(created_at, version) AS created_at,
                max(version) AS max_version,
                argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_call_sites_unresolved
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String}
            GROUP BY callee_name, path, line, from_symbol_id
            HAVING is_live = 1
            """,
            {"org": org, "ns": ns, "path": path},
        )
        for r in rows:
            r["org"] = org
            r["namespace"] = ns
        return rows

    async def live_call_guards_for_path(self, org: str, ns: str, path: str) -> list[dict[str, Any]]:
        """Live caller-guard rows of one source path — the ingest diff's
        read, served by the path bloom index. Same liveness
        shape as the edge readers: argMax over version, tombstones dropped in
        HAVING; the arrays come back as the newest version's."""
        rows = await self._ch.query_all(
            """
            SELECT
                from_symbol_id, path,
                argMax(guards, version) AS guards,
                argMax(guard_kinds, version) AS guard_kinds,
                argMax(sites, version) AS sites,
                argMax(site_callees, version) AS site_callees,
                argMax(site_guards, version) AS site_guards,
                argMax(indexed_commit, version) AS indexed_commit,
                argMax(created_at, version) AS created_at,
                argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_caller_guards
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String}
            GROUP BY from_symbol_id, path
            HAVING is_live = 1
            """,
            {"org": org, "ns": ns, "path": path},
        )
        for r in rows:
            r["org"] = org
            r["namespace"] = ns
        return rows

    async def call_guards_for_callers(
        self, org: str, ns: str, froms: list[str]
    ) -> list[dict[str, Any]]:
        """Live caller-guard rows of the callers of a call chain: a point
        read on the table's own key — (org, namespace,
        from_symbol_id) is its ORDER BY prefix — one statement for the whole
        chain (depth <= 4, so at most four callers). The caller keeps the
        sites it wants by (line, callee tag) — `call_guards.site_guards_of`."""
        if not froms:
            return []
        return await self._ch.query_all(
            """
            SELECT
                from_symbol_id, path,
                argMax(guards, version) AS guards,
                argMax(guard_kinds, version) AS guard_kinds,
                argMax(sites, version) AS sites,
                argMax(site_callees, version) AS site_callees,
                argMax(site_guards, version) AS site_guards,
                argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_caller_guards
            WHERE org = {org:String} AND namespace = {ns:String}
              AND from_symbol_id IN {froms:Array(String)}
            GROUP BY from_symbol_id, path
            HAVING is_live = 1
            ORDER BY from_symbol_id, path
            LIMIT 200
            """,
            {"org": org, "ns": ns, "froms": sorted(set(froms))[:64]},
        )

    async def has_call_guards(self, org: str, ns: str) -> bool:
        """Does the namespace hold any caller-guard row at all? The positive
        control behind an "unguarded" hop: with the
        table empty for the namespace (the guard stage has not run there)
        a missing site means `unknown`, not "no condition". Keyed on the
        (org, namespace) prefix, LIMIT 1; tombstoned rows count as presence
        — the pass ran."""
        rows = await self._ch.query_all(
            """
            SELECT 1 AS present
            FROM loom.code_caller_guards
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            {"org": org, "ns": ns},
        )
        return bool(rows)

    async def call_misses_for_names(
        self, org: str, ns: str, names: list[str]
    ) -> dict[str, dict[str, int]]:
        """bare callee name -> {'unresolved': n, 'ambiguous': n}: how many
        call sites named like this the fast tier declined to bind. What
        code.callers adds beside its count so the floor has a size."""
        if not names:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT callee_name, disposition, count() AS n
            FROM loom.code_call_sites_unresolved FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND callee_name IN {names:Array(String)} AND tombstoned_at IS NULL
            GROUP BY callee_name, disposition
            """,
            {"org": org, "ns": ns, "names": names[:50]},
        )
        out: dict[str, dict[str, int]] = {}
        for r in rows:
            out.setdefault(str(r["callee_name"]), {})[str(r["disposition"])] = int(r["n"])
        return out

    async def live_call_misses_for_names(
        self, org: str, ns: str, names: list[str], *, limit: int = 2000
    ) -> list[dict[str, Any]]:
        """Live declined-call rows whose bare callee is one of `names`, full
        mirrored columns (the closure repair tombstones some and the
        dependents set is the distinct paths of the rest).

        `callee_name` is the third sort-key column after the tenant pair, so
        an IN list of a few hundred names selects a few granules of the
        6-column key and reads them once; no FINAL, the same argMax/HAVING
        discipline as live_call_misses_for_path. `limit` bounds a hub name
        (`parse`, `execute`) that thousands of sites declined — the rows
        beyond it are not lost, their paths re-resolve when the paths
        themselves change."""
        if not names:
            return []
        rows = await self._ch.query_all(
            """
            SELECT
                callee_name, path, line, from_symbol_id,
                argMax(from_qualified_name, version) AS from_qualified_name,
                argMax(call_text, version) AS call_text,
                argMax(disposition, version) AS disposition,
                argMax(candidates, version) AS candidates,
                argMax(indexed_commit, version) AS indexed_commit,
                argMax(created_at, version) AS created_at,
                max(version) AS max_version,
                argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_call_sites_unresolved
            WHERE org = {org:String} AND namespace = {ns:String}
              AND callee_name IN {names:Array(String)}
            GROUP BY callee_name, path, line, from_symbol_id
            HAVING is_live = 1
            ORDER BY callee_name, path, line, from_symbol_id
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "names": names[:1000], "limit": int(limit)},
        )
        for r in rows:
            r["org"] = org
            r["namespace"] = ns
        return rows

    async def live_edges_to_symbols(
        self,
        org: str,
        ns: str,
        symbol_ids: list[str],
        *,
        tier: str = "treesitter",
        edge_kinds: list[str] | None = None,
        max_confidence: float | None = None,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Live edges INTO `symbol_ids` from any path — the reverse lookup the
        dependents set needs for a target this commit tombstoned or made
        ambiguous. (org, namespace, tier, to_symbol_id) is
        the sort-key prefix, so this is the same key read code.callers
        makes; `max_confidence` (compared in Float32, the column's type)
        keeps only the repo-wide name grade when asked."""
        if not symbol_ids:
            return []
        kinds = edge_kinds or ["calls"]
        conf_sql = "AND confidence <= {maxc:Float32}" if max_confidence is not None else ""
        rows = await self._ch.query_all(
            f"""
            SELECT tier, edge_kind, to_symbol_id, from_symbol_id, path, line,
                   argMax(to_qualified_name, version) AS to_qualified_name,
                   argMax(confidence, version) AS confidence,
                   argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_edges
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND tier = {{tier:String}}
              AND to_symbol_id IN {{ids:Array(String)}}
              AND edge_kind IN {{kinds:Array(String)}}
            GROUP BY tier, edge_kind, to_symbol_id, from_symbol_id, path, line
            HAVING is_live = 1 {conf_sql}
            ORDER BY path, line
            LIMIT {{limit:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "tier": tier,
                "ids": symbol_ids[:1000],
                "kinds": kinds,
                "limit": int(limit),
                **({"maxc": float(max_confidence)} if max_confidence is not None else {}),
            },
        )
        return rows

    async def edges_for_symbols(
        self,
        org: str,
        ns: str,
        symbol_ids: list[str],
        *,
        direction: str,  # "callers" (match to_symbol_id) | "callees" (match from)
        edge_kinds: list[str] | None = None,
        limit: int = 200,
        min_confidence: float = 0.0,
        per_match: int | None = None,
    ) -> list[dict[str, Any]]:
        """One hop of live edges. Both tiers returned with tier marked; the
        service layer prefers scip edges per (from,to) pair.

        Both directions are key reads. Callers match `to_symbol_id`, the
        fourth column of the table's sort key; callees match `from_symbol_id`,
        the fourth column of the `by_from` projection, which
        the planner picks by itself because this statement has no
        FINAL (liveness is `argMax(version)` + HAVING). Keep it that way: a
        FINAL here, or a predicate on a column ahead of the symbol id, drops
        the callee read back to the bloom-filter scan (ten anchors: 307 MB
        against 80 MB on the projection).

        `per_match` caps the rows returned PER MATCHED SYMBOL (`LIMIT n BY
        <match_col>`), so a hub with thousands of callees cannot spend the
        whole `limit` on itself and starve the other anchors (a per-anchor k,
        typically 10). Ordering is applied before the cap, so the compiler
        tier and the higher confidences survive it. None keeps the flat LIMIT.

        The two qualified names close the ORDER BY: (tier, confidence, path,
        line) ties whenever one call site has several callees (`a(b())`),
        and a tied sort under LIMIT is decided by merge order, so two
        identical reads could serve different callee slices. The two symbol
        ids close it fully: template instantiations of one name at one call
        line tie on every column above, and which of them a `LIMIT n BY`
        keeps would otherwise depend on the statement's IN list, so a
        per-file and a per-round read could differ.

        Ordered compiler tier first, then by confidence, so when `limit`
        truncates, the parser tier's repo-wide name guesses (0.6) are what
        falls off — not the compiler's edges. `min_confidence` drops the
        weaker parser grades entirely (1.0 same-file unique, 0.8 enclosing-
        class method, 0.6 unique repo-wide name match; scip is always 1.0).
        On the ClickHouse repository about 12% of parser call edges sit at
        0.6, and those are where `parse`/`check`-style name collisions live;
        graph expansion that ignores edge confidence ranks worse."""
        if not symbol_ids:
            return []
        match_col = "to_symbol_id" if direction == "callers" else "from_symbol_id"
        kinds = edge_kinds or ["calls"]
        per_match_sql = f"LIMIT {{per_match:UInt32}} BY {match_col}" if per_match else ""
        rows = await self._ch.query_all(
            f"""
            SELECT tier, edge_kind, to_symbol_id, from_symbol_id, path, line,
                   argMax(from_qualified_name, version) AS from_qualified_name,
                   argMax(to_qualified_name, version) AS to_qualified_name,
                   argMax(confidence, version) AS confidence,
                   argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_edges
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND {match_col} IN {{ids:Array(String)}}
              AND edge_kind IN {{kinds:Array(String)}}
            GROUP BY tier, edge_kind, to_symbol_id, from_symbol_id, path, line
            HAVING is_live = 1 AND confidence >= {{minc:Float32}}
            ORDER BY tier DESC, confidence DESC, path, line, to_qualified_name, from_qualified_name,
                     to_symbol_id, from_symbol_id
            {per_match_sql}
            LIMIT {{limit:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "ids": symbol_ids[:500],
                "kinds": kinds,
                "limit": limit,
                "minc": float(min_confidence),
                **({"per_match": int(per_match)} if per_match else {}),
            },
        )
        return rows

    async def caller_counts(
        self,
        org: str,
        ns: str,
        symbol_ids: list[str],
    ) -> dict[str, dict[str, int]]:
        """Live hop-1 call sites per target symbol, both tiers, one grouped
        read on the edge table's (org, namespace, tier, to_symbol_id) prefix
        (the brief's `callers_hop1` per touched function).
        `{to_symbol_id: {"call_sites": n, "registration": m}}` — `m` of the
        `n` sites are registration functions (the class is
        reached through a registry; `is_registration_caller`'s shape, as
        one regular expression the statement applies to the caller's name)."""
        ids = [s for s in dict.fromkeys(symbol_ids) if s][:200]
        if not ids:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT to_symbol_id, count() AS call_sites,
                   countIf(match(caller_bare, {registrar:String})) AS registration
            FROM (
                SELECT to_symbol_id, from_symbol_id, path, line,
                       argMax(tombstoned_at IS NULL, version) AS is_live,
                       splitByString(
                           '::', splitByChar('(', argMax(from_qualified_name, version))[1]
                       )[-1] AS caller_bare
                FROM loom.code_edges
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND edge_kind = 'calls' AND to_symbol_id IN {ids:Array(String)}
                GROUP BY to_symbol_id, from_symbol_id, path, line
                HAVING is_live = 1
            )
            GROUP BY to_symbol_id
            """,
            {"org": org, "ns": ns, "ids": ids, "registrar": REGISTRAR_NAME_RE},
        )
        return {
            str(r["to_symbol_id"]): {
                "call_sites": int(r["call_sites"]),
                "registration": int(r["registration"]),
            }
            for r in rows
        }

    async def include_edges(self, org: str, ns: str) -> list[tuple[str, str]]:
        """(from_path, include_target) pairs, live, both tiers."""
        rows = await self._ch.query_all(
            """
            SELECT from_qualified_name AS f, to_qualified_name AS t,
                   argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_edges
            WHERE org = {org:String} AND namespace = {ns:String}
              AND edge_kind = 'includes'
            GROUP BY f, t
            HAVING is_live = 1
            """,
            {"org": org, "ns": ns},
        )
        return [(str(r["f"]), str(r["t"])) for r in rows]

    async def live_callers_of_symbols(
        self, org: str, ns: str, symbol_ids: list[str], *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Live `calls` edges into `symbol_ids`, one row per call site (`f`, `t`, `path`,
        `line`), path and line order."""
        rows: list[dict[str, Any]] = await self._ch.query_all(
            """
            SELECT from_qualified_name AS f, to_qualified_name AS t, path, line,
                   argMax(tombstoned_at IS NULL, version) AS is_live
            FROM loom.code_edges
            WHERE org = {org:String} AND namespace = {ns:String}
              AND edge_kind = 'calls' AND to_symbol_id IN {ids:Array(String)}
            GROUP BY f, t, path, line
            HAVING is_live = 1
            ORDER BY path, line
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "ids": symbol_ids, "limit": limit},
        )
        return rows
