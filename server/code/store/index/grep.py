"""code.grep over live chunk content: file and chunk matches, the message
grep's chunk pre-filter, and the literal presence checks.
"""

from __future__ import annotations

from typing import Any

from server.code.domain import deadline as code_deadline
from server.code.domain.index import grep_sql
from server.code.domain.index.grep_plan import (
    GREP_FINAL_SETTINGS,
    any_literal_guard,
    content_predicate,
)
from server.code.domain.search_scope import SearchScope
from server.code.store.index.base import CodeRepoBase
from server.code.store.index.sql import _batched, _scope_path_sql

# A raising or logging statement carries one of these; the message grep's
# chunk pre-filter, mirrored by the ranker in
# server/code/app/message_grep.py::STATEMENT_KEYWORDS.
_MESSAGE_STATEMENT_KEYWORDS: tuple[str, ...] = (
    "throw",
    "Exception(",
    "onError",
    "ErrorCodes::",
    "LOG_",
)


class GrepMixin(CodeRepoBase):
    """Content matches over live chunks."""

    async def grep_files(
        self,
        org: str,
        ns: str,
        pattern: str,
        *,
        regex: bool = False,
        case_sensitive: bool = True,
        path_prefix: str = "",
        limit: int = 200,
        scope: SearchScope | None = None,
        max_execution_s: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Files whose chunks match, with the matching-chunk count and the
        first matching line, path-ordered, plus the total distinct file count
        before the limit. Powers code.grep mode=files (no chunk bodies read).

        One FINAL-free scan statement carrying the total as a window.
        `max_execution_s` stops the scan there and returns what it read."""
        cond, cond_params = content_predicate(
            pattern, regex=regex, case_sensitive=case_sensitive, index_guard=False
        )
        prefix_clause = "AND startsWith(path, {prefix:String})" if path_prefix else ""
        path_sql, path_params = _scope_path_sql(scope)
        params = {
            "org": org,
            "ns": ns,
            **cond_params,
            "prefix": path_prefix,
            "limit": limit,
            **path_params,
        }
        rows = await self._ch.query_all(
            grep_sql.grep_files_sql(
                cond, prefix_clause, path_sql, code_deadline.break_settings(max_execution_s)
            ),
            params,
        )
        files = [
            {
                "path": str(r["path"]),
                "chunks": int(r["chunks"]),
                "first_line": int(r["first_line"] or 0),
            }
            for r in rows
        ]
        return files, int(rows[0]["total"]) if rows else 0

    async def grep_files_many(
        self,
        org: str,
        ns: str,
        patterns: list[str],
        *,
        path_prefixes: list[str],
        limit: int,
    ) -> dict[tuple[str, str], tuple[list[dict[str, Any]], int]]:
        """`grep_files` (literal, case-sensitive) for a round of patterns over
        a set of path prefixes in ONE statement: (pattern,
        prefix) -> (files, total). Read per (term, tree), the coverage claim's
        test-tree greps would run this shape twice — the page and its
        `uniqExact` total — one statement each, many times inside the ingest
        pass. Here every chunk under any of the prefixes is
        matched against the round's patterns once (the ARRAY JOIN emits one
        row per pattern the chunk contains), grouped by (pattern, path) like
        the per-pattern read groups by path, and paged in memory: each
        request's files are its paths in path order up to `limit`, its total
        the number of its groups — the rows `grep_files` returns for that
        pattern and prefix, and its total, without the second statement. The
        index-visible guard is the OR of the patterns' literal plans (an OR
        guard carries every pattern or none); ≤ 32 patterns per statement."""
        wanted = list(dict.fromkeys(p for p in patterns if p))
        prefixes = list(dict.fromkeys(p for p in path_prefixes if p))
        out: dict[tuple[str, str], tuple[list[dict[str, Any]], int]] = {
            (pat, pre): ([], 0) for pat in wanted for pre in prefixes
        }
        if not wanted or not prefixes:
            return out
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for batch in _batched(wanted, 32):
            guard, gparams = any_literal_guard(batch, prefix="gm")
            guard_sql = f"AND {guard}" if guard else ""
            prefix_sql = " OR ".join(
                f"startsWith(path, {{pre_{i}:String}})" for i in range(len(prefixes))
            )
            params: dict[str, Any] = {"org": org, "ns": ns, "pats": batch, **gparams}
            params.update({f"pre_{i}": p for i, p in enumerate(prefixes)})
            rows = await self._ch.query_all(
                f"""
                SELECT pi, path, count() AS chunks, min(start_line) AS first_line
                FROM loom.code_chunks FINAL
                ARRAY JOIN arrayFilter(
                    i -> position(content, {{pats:Array(String)}}[i]) > 0,
                    arrayEnumerate({{pats:Array(String)}})
                ) AS pi
                WHERE org = {{org:String}} AND namespace = {{ns:String}}
                  {guard_sql}
                  AND ({prefix_sql})
                  AND tombstoned_at IS NULL
                GROUP BY pi, path
                ORDER BY pi, path
                {GREP_FINAL_SETTINGS}
                """,
                params,
            )
            for r in rows:
                pat = batch[int(r["pi"]) - 1]
                path = str(r["path"])
                row = {
                    "path": path,
                    "chunks": int(r["chunks"]),
                    "first_line": int(r["first_line"] or 0),
                }
                for pre in prefixes:
                    if path.startswith(pre):
                        groups.setdefault((pat, pre), []).append(row)
        for key, rows_ in groups.items():
            rows_.sort(key=lambda r: str(r["path"]))
            out[key] = (rows_[:limit], len(rows_))
        return out

    async def grep_chunks(
        self,
        org: str,
        ns: str,
        pattern: str,
        *,
        regex: bool = False,
        case_sensitive: bool = True,
        path_prefix: str = "",
        limit: int = 60,
        scope: SearchScope | None = None,
        max_execution_s: float | None = None,
    ) -> list[dict[str, Any]]:
        """Chunks whose content matches a literal (default) or RE2 regex.

        Powers code.grep — the exact-literal lookup, in place of shell grep.
        Returns whole chunk rows (content + scope + span + symbol_ids); the
        handler extracts matching lines and attaches the enclosing unit's
        span to each. Chunk coverage is not
        byte-complete for a file, so results are a floor."""
        cond, cond_params = content_predicate(
            pattern, regex=regex, case_sensitive=case_sensitive, index_guard=False
        )
        prefix_clause = "AND startsWith(path, {prefix:String})" if path_prefix else ""
        path_sql, path_params = _scope_path_sql(scope)
        return await self._ch.query_all(
            grep_sql.grep_chunks_sql(
                cond, prefix_clause, path_sql, code_deadline.break_settings(max_execution_s)
            ),
            {
                "org": org,
                "ns": ns,
                **cond_params,
                "prefix": path_prefix,
                "limit": limit,
                **path_params,
            },
        )

    async def grep_message_chunks(
        self,
        org: str,
        ns: str,
        needles: list[str],
        *,
        path_prefix: str = "",
        limit: int = 60,
        scope: SearchScope | None = None,
        max_execution_s: float | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Chunks that carry a raising or logging statement AND any of the
        message's literal shingles (code.grep mode=message), most shingles
        first. Plain WHERE; one scan. The second value is the distinct files
        the shingles admit — the constrained-search pre-count, taken as a window
        over the matched rows so the cap costs no second statement."""
        wanted = [str(n) for n in needles if str(n)][:32]
        if not wanted:
            return [], 0
        prefix_clause = "AND startsWith(path, {prefix:String})" if path_prefix else ""
        path_sql, path_params = _scope_path_sql(scope)
        sql = grep_sql.grep_message_sql(
            prefix_clause, path_sql, code_deadline.break_settings(max_execution_s)
        )
        rows = await self._ch.query_all(
            sql,
            {
                "org": org,
                "ns": ns,
                "needles": wanted,
                "keywords": list(_MESSAGE_STATEMENT_KEYWORDS),
                "prefix": path_prefix,
                "limit": limit,
                **path_params,
            },
        )
        admitted = int(rows[0].get("admitted_files") or 0) if rows else 0
        return rows, admitted

    async def chunk_contains(
        self, org: str, ns: str, path: str, literal: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Chunks in `path` whose stored content contains `literal` (case
        sensitive). Chunk coverage is not byte-complete for a file, so an
        empty result is a floor — code.verify discloses that."""
        if not literal:
            return []
        cond, cond_params = content_predicate(
            literal, regex=False, case_sensitive=True, param="lit"
        )
        return await self._ch.query_all(
            f"""
            SELECT start_line, end_line, scope,
                   substring(content, greatest(1, position(content, {{lit:String}}) - 60), 160)
                       AS excerpt
            FROM loom.code_chunks FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND path = {{path:String}} AND tombstoned_at IS NULL
              AND {cond}
            ORDER BY start_line
            LIMIT {{limit:UInt32}}
            {GREP_FINAL_SETTINGS}
            """,
            {"org": org, "ns": ns, "path": path, **cond_params, "limit": limit},
        )

    async def paths_containing(
        self, org: str, ns: str, paths: list[str], pattern: str
    ) -> list[str]:
        """Which of `paths` hold a chunk containing the literal `pattern` — a
        membership check over a candidate set (≤ 400 paths), so a fingerprint
        part that matches thousands of files (`ENGINE = MergeTree`) is never
        fetched as a list and then truncated."""
        cands = sorted({str(x) for x in paths if x})[:400]
        if not cands or not pattern:
            return []
        cond, cond_params = content_predicate(pattern, regex=False, case_sensitive=True)
        rows = await self._ch.query_all(
            f"""
            SELECT DISTINCT path FROM loom.code_chunks FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND path IN {{paths:Array(String)}} AND {cond}
              AND tombstoned_at IS NULL
            ORDER BY path
            {GREP_FINAL_SETTINGS}
            """,
            {"org": org, "ns": ns, "paths": cands, **cond_params},
        )
        return [str(r["path"]) for r in rows]
