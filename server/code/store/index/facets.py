"""Test intelligence: the per-statement test facets (loom.code_test_facets),
the closest-test scoring over them, and the source-to-test map
(loom.code_test_map).
"""

from __future__ import annotations

from typing import Any

from server.code.store.index.base import CodeRepoBase
from server.code.store.index.sql import _batched

# How much a facet kind says about what a test IS. A statement kind or a
# function name places a test; a tag or a format is shared by hundreds; the
# statement_kind multiset is the test's shape.
CLOSEST_KIND_WEIGHTS: dict[str, float] = {
    "function": 1.0,
    "engine": 1.0,
    "setting": 0.9,
    "error_code": 0.9,
    "system_table": 0.8,
    "statement_kind": 0.6,
    "type": 0.5,
    "format": 0.4,
    "tag": 0.3,
}


def closest_scores(
    want: list[tuple[str, str]], rows: list[dict[str, Any]], *, limit: int = 10
) -> list[dict[str, Any]]:
    """Weighted Jaccard between the asked facet set and each candidate test's
    facet set, per kind (a normalized structural signature picks a better
    exemplar than text similarity).
    `similarity` = Σ_k w_k · |A_k ∩ B_k| / |A_k ∪ B_k| over the kinds either
    side names, divided by Σ_k w_k of those kinds; `shared` and `missing`
    say what the candidate has of the ask and what the ask has that the
    candidate lacks — the diff a new test would add. Ties break on the
    candidate with fewer extra facets (the tighter exemplar), then path."""
    asked: dict[str, set[str]] = {}
    for k, n in want:
        asked.setdefault(k, set()).add(n)
    out: list[dict[str, Any]] = []
    for r in rows:
        have: dict[str, set[str]] = {}
        for k, n in r.get("facets") or []:
            have.setdefault(str(k), set()).add(str(n))
        kinds = set(asked) | set(have)
        num = den = 0.0
        for k in kinds:
            w = CLOSEST_KIND_WEIGHTS.get(k, 0.5)
            a, b = asked.get(k, set()), have.get(k, set())
            union = a | b
            if not union:
                continue
            num += w * len(a & b) / len(union)
            den += w
        if den <= 0:
            continue
        shared = sorted((k, n) for k, ns_ in asked.items() for n in ns_ if n in have.get(k, set()))
        missing = sorted(
            (k, n) for k, ns_ in asked.items() for n in ns_ if n not in have.get(k, set())
        )
        extra = sum(len(b - asked.get(k, set())) for k, b in have.items())
        out.append(
            {
                "path": str(r["path"]),
                "similarity": round(num / den, 4),
                "shared": [{"kind": k, "name": n} for k, n in shared],
                "missing": [{"kind": k, "name": n} for k, n in missing],
                "extra_facets": extra,
            }
        )
    out.sort(key=lambda x: (-x["similarity"], x["extra_facets"], x["path"]))
    return out[:limit]


def _fold_facet_hits(row: dict[str, Any], pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """One `tests_for_facets` row: the per-facet ordinal sets folded into the
    path's answer."""
    by_pair: dict[tuple[str, str], set[int]] = {p: set() for p in pairs}
    lines: set[int] = set()
    conf = 1.0
    for kind, name, ordinal, line, confidence in row.get("hits") or []:
        by_pair.setdefault((str(kind), str(name)), set()).add(int(ordinal))
        lines.add(int(line))
        conf = min(conf, float(confidence))
    ordinals: set[int] = set().union(*by_pair.values()) if by_pair else set()
    common = set.intersection(*by_pair.values()) if by_pair else set()
    return {
        "path": str(row["path"]),
        "ordinals": sorted(ordinals),
        "lines": sorted(lines),
        "confidence": round(conf, 3),
        "same_statement": bool(common),
    }


class FacetsMixin(CodeRepoBase):
    """Test facets and the test map."""

    async def live_facets_for_path(self, org: str, ns: str, path: str) -> list[dict[str, Any]]:
        """One path's live facet rows — every column, so the key-diff in
        ingest can mirror a vanished row into its tombstone. The table is
        keyed (org, namespace, path, facet_kind, name, ordinal), so this is
        one keyed range of a granule or two."""
        return await self._ch.query_all(
            """
            SELECT org, namespace, facet_kind, name, path, ordinal, line, confidence,
                   indexed_commit, tombstoned_at, created_at, version
            FROM loom.code_test_facets FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path = {path:String} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "path": path},
        )

    async def live_facets_for_paths(
        self, org: str, ns: str, paths: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """`live_facets_for_path` for a round of paths in one statement per
        100, routed back per path; every requested path present (an empty
        list for one with no live facet)."""
        out: dict[str, list[dict[str, Any]]] = {str(p): [] for p in paths}
        for batch in _batched(sorted(out), 100):
            rows = await self._ch.query_all(
                """
                SELECT org, namespace, facet_kind, name, path, ordinal, line, confidence,
                       indexed_commit, tombstoned_at, created_at, version
                FROM loom.code_test_facets FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
                """,
                {"org": org, "ns": ns, "paths": batch},
            )
            for r in rows:
                out.setdefault(str(r["path"]), []).append(r)
        return out

    async def live_dialect_facets(self, org: str, ns: str) -> list[dict[str, Any]]:
        """Every live file-level `dialect` facet of a namespace (the folder
        vote's input): `[{path, name, confidence}]`. Every file carries one,
        so this reads the namespace's slice of the table."""
        return await self._ch.query_all(
            """
            SELECT path, name, confidence
            FROM loom.code_test_facets FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND facet_kind = 'dialect' AND ordinal = 0 AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns},
        )

    async def tests_for_facets(
        self, org: str, ns: str, facets: list[tuple[str, str]], *, limit: int = 50
    ) -> tuple[list[dict[str, Any]], int]:
        """Tests whose statement records carry EVERY named facet:
        `[(kind, name), …]` → `[{path, ordinals, lines, confidence,
        same_statement}]` ordered by confidence (keyword-bound names first)
        then path, plus the total before `limit`. One read over the
        namespace, pruned to the granules the name bloom filter admits,
        folded per path in ClickHouse;
        a path counts when its distinct matched pairs equal the number
        asked for. Rows whose facets co-occur on one statement (the ordinal
        sets intersect) rank first and carry `same_statement` — "expects X
        from a call to Y" wants that, "sets A and creates B" does not."""
        if not facets:
            return [], 0
        pairs = sorted(set(facets))
        params: dict[str, Any] = {
            "org": org,
            "ns": ns,
            "pairs": pairs,
            "want": len(pairs),
            "lim": limit + 1,
        }
        # co-occurrence first when several facets are asked for: the ordinal
        # sets of every facet intersect on at least one statement
        co = "1"
        if len(pairs) > 1:
            sets = []
            for i, (k, n) in enumerate(pairs):
                params[f"k{i}"], params[f"n{i}"] = k, n
                sets.append(
                    f"groupUniqArrayIf(ordinal, facet_kind = {{k{i}:String}} "
                    f"AND name = {{n{i}:String}})"
                )
            co = f"length(arrayIntersect({', '.join(sets)})) > 0"
        rows = await self._ch.query_all(
            f"""
            SELECT path,
                   uniqExact(facet_kind, name) AS matched,
                   groupArray((facet_kind, name, ordinal, line, confidence)) AS hits,
                   {co} AS co_occurs
            FROM loom.code_test_facets FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND (facet_kind, name) IN {{pairs:Array(Tuple(String, String))}}
              AND tombstoned_at IS NULL
            GROUP BY path
            HAVING matched = {{want:UInt32}}
            ORDER BY co_occurs DESC, min(confidence) DESC, path
            LIMIT {{lim:UInt32}}
            """,
            params,
        )
        total = len(rows)
        if total > limit:
            counted = await self._ch.query_all(
                """
                SELECT count() AS n FROM (
                    SELECT path FROM loom.code_test_facets FINAL
                    WHERE org = {org:String} AND namespace = {ns:String}
                      AND (facet_kind, name) IN {pairs:Array(Tuple(String, String))}
                      AND tombstoned_at IS NULL
                    GROUP BY path
                    HAVING uniqExact(facet_kind, name) = {want:UInt32}
                )
                """,
                {"org": org, "ns": ns, "pairs": pairs, "want": len(pairs)},
            )
            total = int(counted[0]["n"]) if counted else total
        return [_fold_facet_hits(r, pairs) for r in rows[:limit]], total

    async def facet_sets_sharing(
        self, org: str, ns: str, facets: list[tuple[str, str]], *, limit: int = 400
    ) -> list[dict[str, Any]]:
        """The candidate set of `closest_tests`: every test that shares at
        least one of the named facets, with ITS whole facet
        set (`(kind, name)` pairs) — one grouped read for the candidates
        (pruned by the name bloom filter), one keyed read over the
        candidates' paths for their sets. `limit` bounds the candidates by how many of the named
        facets they share (most first), so a common tag alone cannot flood
        the read."""
        if not facets:
            return []
        pairs = sorted(set(facets))
        return await self._ch.query_all(
            """
            WITH cand AS (
                SELECT path
                FROM loom.code_test_facets FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND (facet_kind, name) IN {pairs:Array(Tuple(String, String))}
                  AND tombstoned_at IS NULL
                GROUP BY path
                ORDER BY uniqExact(facet_kind, name) DESC, path
                LIMIT {lim:UInt32}
            )
            SELECT path, groupUniqArray((facet_kind, name)) AS facets
            FROM loom.code_test_facets FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path IN cand AND tombstoned_at IS NULL
            GROUP BY path
            """,
            {"org": org, "ns": ns, "pairs": pairs, "lim": max(1, limit)},
        )

    async def facets_present(self, org: str, ns: str) -> bool:
        """Does the namespace hold ANY facet row? The positive control behind
        an empty `code.tests_for`: rows but no match
        is an answer, no rows at all means the SQL tree predates the records."""
        rows = await self._ch.query_all(
            """
            SELECT 1 FROM loom.code_test_facets
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            {"org": org, "ns": ns},
        )
        return bool(rows)

    async def live_facets_of_kind(
        self, org: str, ns: str, kind: str, *, name_like: str = "", limit: int = 5000
    ) -> list[dict[str, Any]]:
        """The live rows of one facet kind, optionally with `name LIKE` —
        the glob leg of code.tests_for reads the `builds`
        patterns a CMake lane wrote (dozens per repository); the facet_kind
        set index skips the granules without the kind."""
        like = "AND name LIKE {like:String}" if name_like else ""
        rows = await self._ch.query_all(
            f"""
            SELECT path, name, ordinal, line, confidence
            FROM loom.code_test_facets FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND facet_kind = {{kind:String}} {like} AND tombstoned_at IS NULL
            ORDER BY path, ordinal
            LIMIT {{limit:UInt32}}
            """,
            {"org": org, "ns": ns, "kind": kind, "like": name_like, "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    async def facet_kinds_present(self, org: str, ns: str, kinds: list[str]) -> set[str]:
        """Which of the asked facet kinds hold ANY row in the namespace? The
        per-kind positive control behind an empty `code.tests_for`: the
        facet_kind set index skips every granule without an asked kind — a
        namespace whose lanes never wrote a kind (a TypeScript
        tree asked for `setting`, an older index asked for `column_read`) is
        not covered for it, and the caller falls back to the grep."""
        if not kinds:
            return set()
        rows = await self._ch.query_all(
            """
            SELECT DISTINCT facet_kind FROM loom.code_test_facets
            WHERE org = {org:String} AND namespace = {ns:String}
              AND facet_kind IN {kinds:Array(String)}
            """,
            {"org": org, "ns": ns, "kinds": sorted(set(kinds))},
        )
        return {str(r["facet_kind"]) for r in rows}

    async def sources_of_tests(
        self, org: str, ns: str, test_paths: list[str], *, limit: int = 20
    ) -> list[str]:
        """The source files the test map ties to `test_paths` (the inverse of
        `test_map_for`, under the bloom index on `test_path`), strongest
        first — the sources whose churn a failing test inherits."""
        wanted = sorted({str(p) for p in test_paths if p})[:20]
        if not wanted:
            return []
        rows = await self._ch.query_all(
            """
            SELECT src_path, max(confidence) AS best_confidence
            FROM loom.code_test_map FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND test_path IN {paths:Array(String)} AND tombstoned_at IS NULL
            GROUP BY src_path
            ORDER BY best_confidence DESC, src_path
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "paths": wanted, "limit": limit},
        )
        return [str(r["src_path"]) for r in rows]

    async def test_map_for(
        self, org: str, ns: str, paths: list[str], *, limit: int = 30
    ) -> list[dict[str, Any]]:
        """The strongest `limit` (src_path, test_path) rows for the first 50
        `paths`. The order is total: nearly every row ties
        at one confidence, and a page cut from a tied `ORDER BY confidence
        DESC` follows the server's whim — two processes could serve different
        `impacted_tests` pages for one shape. `src_path, test_path` closes
        the tie; the caller decides which 50 paths arrive (and in what
        order), so it must hand them over in a fixed order too."""
        if not paths:
            return []
        return await self._ch.query_all(
            """
            SELECT src_path, test_path, max(confidence) AS confidence
            FROM loom.code_test_map FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND src_path IN {paths:Array(String)} AND tombstoned_at IS NULL
            GROUP BY src_path, test_path
            ORDER BY confidence DESC, src_path, test_path
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "paths": paths[:50], "limit": limit},
        )

    async def test_map_detail(
        self, org: str, ns: str, paths: list[str], *, limit: int = 60
    ) -> list[dict[str, Any]]:
        """Test-map rows for `paths` WITH their evidence class: method,
        confidence and the evidence string, one row per (src_path, test_path,
        method), strongest first, the order total. The coverage claim reports
        the class each test was found by. `coverage` rows are left to
        `coverage_rows_for`: the claim filters them by the subject's span, and
        a file's many measured rows must not crowd its other evidence out of
        this page."""
        if not paths:
            return []
        return await self._ch.query_all(
            """
            SELECT src_path, test_path, method, max(confidence) AS confidence,
                   anyLast(evidence) AS evidence
            FROM loom.code_test_map FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND src_path IN {paths:Array(String)} AND tombstoned_at IS NULL
              AND method != 'coverage'
            GROUP BY src_path, test_path, method
            ORDER BY confidence DESC, src_path, test_path, method
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "paths": paths[:50], "limit": limit},
        )

    async def tested_paths(self, org: str, ns: str, paths: list[str] | None = None) -> set[str]:
        """Source paths a mapped test reaches (any method) — every one, not
        the flat top-30 the served-set read keeps (test_map_for): the
        precomputed row answers for one file at a time. `paths` restricts
        the read to a keyed set (batched by 200); None scans the namespace's
        rows of the table once (the weekly pass)."""
        if paths is None:
            rows = await self._ch.query_all(
                """
                SELECT DISTINCT src_path FROM loom.code_test_map FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tombstoned_at IS NULL
                """,
                {"org": org, "ns": ns},
            )
            return {str(r["src_path"]) for r in rows}
        out: set[str] = set()
        for batch in _batched(list(dict.fromkeys(paths)), 200):
            rows = await self._ch.query_all(
                """
                SELECT DISTINCT src_path FROM loom.code_test_map FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND src_path IN {paths:Array(String)} AND tombstoned_at IS NULL
                """,
                {"org": org, "ns": ns, "paths": batch},
            )
            out |= {str(r["src_path"]) for r in rows}
        return out
