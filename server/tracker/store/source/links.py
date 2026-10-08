"""Issue–PR link recovery: the PR corpus's vocabulary, the issue pages and
the BM25F candidate scan per issue.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from server.core import bm25
from server.settings import settings
from server.tracker.domain.links import closes_pattern
from server.tracker.store.source.base import SourceBase
from server.tracker.store.source.rows import str_list, upstream_dt


class LinkReads(SourceBase):
    """Readers behind the link-recovery pass."""

    def _links_budget(self) -> int:
        """The pass's statements are index-time reads: a vocabulary over the
        PR corpus and, per issue, a BM25F candidate scan over a merge window
        (160–215 ms live) — the triage budget, never the 300 ms
        interactive one."""
        return int(settings.tracker_live_bisect_budget_ms)

    async def pr_token_stats(self, org: str, ns: str) -> dict[str, Any]:
        """The merged trunk PR corpus's vocabulary for BM25F over PR title and
        body head: df per lower-cased token (df ≥ 3), the document count and
        the average field lengths — `token_stats`'s shape
        over the other table. One read per process per
        `LOOM_TRACKER_SIMILAR_IDF_TTL_S`."""
        repos = self.repos()
        if not repos:
            return {}
        ttl = float(settings.tracker_similar_idf_ttl_s)
        pred = self._trunk_pr_pred()
        vocab, totals = await asyncio.gather(
            self._rows(
                "links_vocab",
                f"""SELECT tok, count() AS df FROM (
                        SELECT arrayJoin(arrayDistinct(tokens(lower(
                                   concat(title, ' ', substring(body, 1, 6000)))))) AS tok
                        FROM {self.db}.github_prs FINAL
                        WHERE {pred}
                    ) GROUP BY tok HAVING df >= 3""",
                {"repos": repos},
                cache_key=["links_vocab", repos],
                budget_ms=self._links_budget(),
                ttl_s=ttl,
            ),
            self._rows(
                "links_totals",
                f"""SELECT count() AS n, avg(length(tokens(lower(title)))) AS avg_lt,
                           avg(length(substring(body, 1, 6000)) / 6) AS avg_lb
                    FROM {self.db}.github_prs FINAL
                    WHERE {pred}""",
                {"repos": repos},
                cache_key=["links_totals", repos],
                budget_ms=self._links_budget(),
                ttl_s=ttl,
            ),
        )
        t = totals[0] if totals else {}
        return {
            "n": int(t.get("n") or 0),
            "avg_title_len": float(t.get("avg_lt") or 10.0),
            "avg_body_len": float(t.get("avg_lb") or 900.0),
            "df": {str(r.get("tok") or ""): int(r.get("df") or 0) for r in vocab},
        }

    async def link_issues(
        self,
        org: str,
        ns: str,
        *,
        since: datetime,
        before_number: int = 0,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """The issues opened since `since`, newest number first, one page at a
        time (`before_number` continues): title, body head, component,
        created_at, state — the queries of one pass."""
        repos = self.repos()
        if not repos:
            return []
        cursor = "AND number < {before:UInt32}" if before_number else ""
        sql = f"""
            SELECT number, source_id, title, substring(body, 1, 6000) AS body,
                   primary_component, created_at, state_reason, closed_at
            FROM {self.db}.github_issues FINAL
            WHERE {self._privacy()}splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
              AND created_at >= {{since:DateTime64(3)}} {cursor}
            ORDER BY number DESC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), 1000))
        params: dict[str, Any] = {"repos": repos, "since": since, "limit": lim}
        if before_number:
            params["before"] = int(before_number)
        rows = await self._rows(
            "link_issues",
            sql,
            params,
            cache_key=[repos, since, before_number, lim],
            budget_ms=self._links_budget(),
        )
        return [
            {
                "number": int(r.get("number") or 0),
                "source_id": str(r.get("source_id") or ""),
                "title": str(r.get("title") or ""),
                "body": str(r.get("body") or ""),
                "component": str(r.get("primary_component") or ""),
                "created_at": upstream_dt(r.get("created_at")),
                # the issue table carries no `state` column: closed_at says it
                "state": "closed" if upstream_dt(r.get("closed_at")) else "open",
                "state_reason": str(r.get("state_reason") or ""),
                "closed_at": upstream_dt(r.get("closed_at")),
            }
            for r in rows
        ]

    async def link_pr_candidates(
        self,
        org: str,
        ns: str,
        *,
        issue_number: int,
        terms: list[str],
        idf: list[float],
        avg_title_len: float,
        avg_body_len: float,
        since: datetime,
        until: datetime,
        files: list[str] | None = None,
        limit: int = 50,
        title_weight: float = 3.0,
    ) -> list[dict[str, Any]]:
        """The merged trunk PRs of [since, until) the text index admits for
        the issue's terms — or whose body carries the issue's number as a
        token — with BM25F over title and body head split by field (the
        similar statement, the wall turned forward), the closing mention
        (`closes_pattern`: 2 closing keyword / 1 named / 0), and how many of
        the issue's path-frame basenames end a changed file's path. The diff
        is NOT read here (+1.2–1.5 s live for every admitted row); the symbol
        overlap is `prs_facts(names=…)` on the top rows only."""
        from server.tracker.domain.similar import case_variants

        repos = self.repos()
        toks = [t for t in terms if t]
        n = int(issue_number)
        if not repos or not toks or n <= 0:
            return []
        variants = sorted({v for t in toks for v in case_variants(t)})
        tfn_t = "({wt:Float64} * ft / (1 - {b:Float64} + {b:Float64} * lt / {avg_lt:Float64}))"
        tfn_b = "(fb / (1 - {b:Float64} + {b:Float64} * lb / {avg_lb:Float64}))"
        sql = f"""
            SELECT pr_number, title, url, merged_at, minor_version, primary_component,
                   src_components,
                   arraySum(arrayMap((ft, w) -> w * ({tfn_t} / ({{k1:Float64}} + {tfn_t})),
                                     tf_t, {{idf:Array(Float64)}})
                            ) AS bm25_title,
                   arraySum(arrayMap((fb, w) -> w * ({tfn_b} / ({{k1:Float64}} + {tfn_b})),
                                     tf_b, {{idf:Array(Float64)}})
                            ) AS bm25_body,
                   multiIf(match(concat(title, ' ', body), {{closes_re:String}}), 2,
                           position(body, {{hash:String}}) > 0
                           OR position(body, {{url:String}}) > 0, 1,
                           0) AS mention,
                   arrayCount(f -> arrayExists(p -> endsWith(p, f), affected_file_paths),
                              {{files:Array(String)}}) AS file_hits
            FROM (
                SELECT pr_number, title, body, url, merged_at, minor_version, primary_component,
                       src_components, affected_file_paths,
                       tokens(lower(title)) AS tt,
                       arrayMap(t -> arrayCount(x -> x = t, tt), {{toks:Array(String)}}) AS tf_t,
                       arrayMap(t -> countSubstringsCaseInsensitive(substring(body, 1, 6000), t),
                                {{toks:Array(String)}}) AS tf_b,
                       length(tt) AS lt, length(substring(body, 1, 6000)) / 6 AS lb
                FROM {self.db}.github_prs FINAL
                WHERE {self._trunk_pr_pred()}
                  AND merged_at >= {{since:DateTime64(3)}} AND merged_at < {{until:DateTime64(3)}}
                  AND (hasAnyTokens(title, {{variants:Array(String)}})
                       OR hasAnyTokens(body, {{variants:Array(String)}})
                       OR hasAnyTokens(body, {{numtok:Array(String)}}))
            )
            ORDER BY mention DESC, bm25_title + bm25_body DESC, merged_at
            LIMIT {{limit:UInt32}}
        """
        # mentions first: a PR admitted only by the issue's number token ranks
        # low by BM25, and a text-ordered LIMIT can cut the fix (admitted 82nd
        # of 82)
        lim = max(1, min(int(limit), 200))
        repo_url = repos[0] if len(repos) == 1 else ""
        params: dict[str, Any] = {
            "toks": toks,
            "idf": [float(x) for x in idf][: len(toks)],
            "variants": variants,
            "numtok": [str(n)],
            "repos": repos,
            "since": since,
            "until": until,
            "limit": lim,
            "k1": bm25.K1,
            "b": bm25.B,
            "wt": float(title_weight),
            "avg_lt": max(1.0, float(avg_title_len)),
            "avg_lb": max(1.0, float(avg_body_len)),
            "closes_re": closes_pattern(n, repo_url),
            "hash": f"#{n}",
            "url": f"/issues/{n}",
            "files": [str(f) for f in (files or []) if f][:16],
        }
        # The upstream's direct read from the text index answers
        # the WHERE from the index alone and LOST rows here — 35 of the 82
        # admitted rows came back for one issue (LIMIT 50), the target not
        # among them; with the direct read off, 50
        rows = await self._rows(
            "link_pr_candidates",
            sql,
            params,
            cache_key=[n, toks, params["idf"], since, until, lim, title_weight, params["files"]],
            budget_ms=self._links_budget(),
            query_settings={"query_plan_direct_read_from_text_index": 0},
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "pr_number": int(r.get("pr_number") or 0),
                    "title": str(r.get("title") or ""),
                    "url": str(r.get("url") or ""),
                    "merged_at": upstream_dt(r.get("merged_at")),
                    "minor_version": str(r.get("minor_version") or ""),
                    "primary_component": str(r.get("primary_component") or ""),
                    "src_components": str_list(r.get("src_components")),
                    "bm25_title": float(r.get("bm25_title") or 0.0),
                    "bm25_body": float(r.get("bm25_body") or 0.0),
                    "mention": int(r.get("mention") or 0),
                    "file_hits": int(r.get("file_hits") or 0),
                }
            )
        return out
