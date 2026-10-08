"""tracker.similar's legs (the query item, the BM25F vocabulary and scan,
frames, mentions, the upstream vector) and tracker.duplicate_check's gold.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from server.core import bm25
from server.settings import settings
from server.tracker.store.source.base import SourceBase
from server.tracker.store.source.rows import similar_row, split_source, str_list, upstream_dt

# Never let the upstream answer a `hasAnyTokens`
# predicate from the text index alone under ORDER BY … LIMIT (rows go missing).
_TEXT_INDEX_SETTINGS: dict[str, Any] = {"query_plan_direct_read_from_text_index": 0}


class SimilarReads(SourceBase):
    """Readers behind tracker.similar and the duplicate fit."""

    def _similar_budget(self) -> int:
        """One leg of tracker.similar: a text-index candidate read plus a body
        scan over ≤ DF_BUDGET rows, 130–330 ms live."""
        return int(settings.tracker_live_text_budget_ms)

    async def similar_item(self, org: str, ns: str, number: int) -> dict[str, Any] | None:
        """The query item's own fields for tracker.similar: title, the head of the
        body, component, affected versions, error code, created_at (the time
        wall: nothing created after it is a candidate) and whether its
        upstream embedding is non-zero. The issue row first; a PR when no
        issue carries the number. The first hop of the op, so it carries the
        text budget: a cold connection alone is ~300 ms."""
        repos = self.repos()
        if not repos or int(number) <= 0:
            return None
        refs = [f"{repo}#{int(number)}" for repo in repos]
        priv = self._privacy()
        issues, prs = await asyncio.gather(
            self._rows(
                "similar_item_issue",
                f"""SELECT 'issue' AS kind, source_id, title, substring(body, 1, 6000) AS body,
                           primary_component, affected_versions, error_code, created_at,
                           version_affected,
                           arraySum(arrayMap(x -> abs(x), embedding)) > 0 AS has_embedding
                    FROM {self.db}.github_issues FINAL
                    WHERE {priv}source_id IN {{refs:Array(String)}} LIMIT 1""",
                {"refs": refs},
                cache_key=["item", refs],
                budget_ms=self._similar_budget(),
            ),
            self._rows(
                "similar_item_pr",
                f"""SELECT 'pr' AS kind, source_id, title, substring(body, 1, 6000) AS body,
                           primary_component, CAST([] AS Array(String)) AS affected_versions,
                           toUInt32(0) AS error_code, created_at, '' AS version_affected,
                           arraySum(arrayMap(x -> abs(x), embedding)) > 0 AS has_embedding
                    FROM {self.db}.github_prs FINAL
                    WHERE {priv}source_id IN {{refs:Array(String)}} LIMIT 1""",
                {"refs": refs},
                cache_key=["item", refs],
                budget_ms=self._similar_budget(),
            ),
        )
        rows = issues or prs
        if not rows:
            return None
        r = rows[0]
        return {
            "kind": str(r.get("kind") or ""),
            "source_id": str(r.get("source_id") or ""),
            "title": str(r.get("title") or ""),
            "body": str(r.get("body") or ""),
            "component": str(r.get("primary_component") or ""),
            "versions": str_list(r.get("affected_versions")),
            "error_code": int(r.get("error_code") or 0),
            "created_at": upstream_dt(r.get("created_at")),
            "has_embedding": bool(int(r.get("has_embedding") or 0)),
            # The first minor version the body names (QA's
            # `version_affected`; `affected_versions` is set on 35 rows)
            "version": str(r.get("version_affected") or ""),
        }

    async def token_stats(self, org: str, ns: str) -> dict[str, Any]:
        """The issue corpus's vocabulary for BM25F: document frequency per
        lower-cased token (df ≥ 3; 37,784 tokens, ~300 ms live), the document
        count and the average title / body lengths. One read per process per
        `LOOM_TRACKER_SIMILAR_IDF_TTL_S`; the upstream index cannot count per
        token inside a budget (1.3 s for eight tokens, verified live), so the
        vocabulary is read whole and kept."""
        repos = self.repos()
        if not repos:
            return {}
        priv = self._privacy()
        ttl = float(settings.tracker_similar_idf_ttl_s)
        budget = int(settings.tracker_live_bisect_budget_ms)
        vocab, totals = await asyncio.gather(
            self._rows(
                "similar_vocab",
                f"""SELECT tok, count() AS df FROM (
                        SELECT arrayJoin(arrayDistinct(tokens(lower(
                                   concat(title, ' ', substring(body, 1, 6000)))))) AS tok
                        FROM {self.db}.github_issues FINAL
                        WHERE {priv}splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                    ) GROUP BY tok HAVING df >= 3""",
                {"repos": repos},
                cache_key=["vocab", repos],
                budget_ms=budget,
                ttl_s=ttl,
            ),
            self._rows(
                "similar_totals",
                f"""SELECT count() AS n, avg(length(tokens(lower(title)))) AS avg_lt,
                           avg(length(substring(body, 1, 6000)) / 6) AS avg_lb
                    FROM {self.db}.github_issues FINAL
                    WHERE {priv}splitByChar('#', source_id)[1] IN {{repos:Array(String)}}""",
                {"repos": repos},
                cache_key=["totals", repos],
                budget_ms=budget,
                ttl_s=ttl,
            ),
        )
        t = totals[0] if totals else {}
        return {
            "n": int(t.get("n") or 0),
            "avg_title_len": float(t.get("avg_lt") or 10.0),
            "avg_body_len": float(t.get("avg_lb") or 540.0),
            "df": {str(r.get("tok") or ""): int(r.get("df") or 0) for r in vocab},
        }

    async def similar_lexical(
        self,
        org: str,
        ns: str,
        *,
        kind: str,
        terms: list[str],
        idf: list[float],
        avg_title_len: float,
        avg_body_len: float,
        before: datetime | None,
        exclude: str = "",
        limit: int = 50,
        title_weight: float = 3.0,
    ) -> list[dict[str, Any]]:
        """BM25F over title (weighted) and the head of the body, computed
        upstream on the rows the text index admits for the selected terms
        (any spelling), created before `before`, the query item itself left
        out. Title term frequency is exact (`tokens(lower(title))`); body
        term frequency is the case-insensitive substring count over the
        first 6,000 characters (a tokenised body costs 6× more per
        candidate, verified live). `idf` is per term from the
        corpus vocabulary; the statement only applies it."""
        repos = self.repos()
        toks = [t for t in terms if t]
        if not repos or not toks or kind not in ("issue", "pr"):
            return []
        from server.tracker.domain.similar import case_variants

        variants = sorted({v for t in toks for v in case_variants(t)})
        table = "github_issues" if kind == "issue" else "github_prs"
        extra = (
            "state_reason, affected_versions, error_code, version_affected, toUInt8(0) AS is_merged"
            if kind == "issue"
            else "'' AS state_reason, CAST([] AS Array(String)) AS affected_versions,"
            " toUInt32(0) AS error_code, '' AS version_affected, is_merged"
        )
        wall = "AND created_at < {before:DateTime64(3)}" if before else ""
        excl = "AND source_id != {exclude:String}" if exclude else ""
        # the length-normalised, field-weighted term frequency, written out
        # twice because a lambda cannot name a sibling alias
        tfn = (
            "({wt} * ft / (1 - {b} + {b} * lt / {avg_lt}) + "
            "fb / (1 - {b} + {b} * lb / {avg_lb}))".replace("{wt}", "{wt:Float64}")
            .replace("{b}", "{b:Float64}")
            .replace("{avg_lt}", "{avg_lt:Float64}")
            .replace("{avg_lb}", "{avg_lb:Float64}")
        )
        sql = f"""
            SELECT '{kind}' AS kind, source_id, url, title, created_at, closed_at,
                   primary_component, {extra},
                   arraySum(arrayMap((ft, fb, w) -> w * ({tfn} / ({{k1:Float64}} + {tfn})),
                                     tf_t, tf_b, {{idf:Array(Float64)}})) AS bm25
            FROM (
                SELECT source_id, url, title, created_at, closed_at, primary_component, {extra},
                       tokens(lower(title)) AS tt,
                       arrayMap(t -> arrayCount(x -> x = t, tt), {{toks:Array(String)}}) AS tf_t,
                       arrayMap(t -> countSubstringsCaseInsensitive(substring(body, 1, 6000), t),
                                {{toks:Array(String)}}) AS tf_b,
                       length(tt) AS lt, length(substring(body, 1, 6000)) / 6 AS lb
                FROM {self.db}.{table} FINAL
                WHERE {self._privacy()}(hasAnyTokens(title, {{variants:Array(String)}})
                       OR hasAnyTokens(body, {{variants:Array(String)}}))
                  {wall} {excl}
                  AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            )
            ORDER BY bm25 DESC, created_at DESC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), 200))
        params: dict[str, Any] = {
            "toks": toks,
            "idf": [float(x) for x in idf][: len(toks)],
            "variants": variants,
            "repos": repos,
            "limit": lim,
            "k1": bm25.K1,
            "b": bm25.B,
            "wt": float(title_weight),
            "avg_lt": max(1.0, float(avg_title_len)),
            "avg_lb": max(1.0, float(avg_body_len)),
        }
        if before:
            params["before"] = before
        if exclude:
            params["exclude"] = exclude
        # The upstream's direct read from the text index answers `hasAnyTokens` from the
        # index alone and LOST rows under ORDER BY … LIMIT (35 of 82 admitted rows on the
        # same shape); the direct read is off on every text-index statement here.
        rows = await self._rows(
            f"similar_lexical_{kind}",
            sql,
            params,
            cache_key=[kind, toks, params["idf"], before, exclude, repos, lim, title_weight],
            budget_ms=self._similar_budget(),
            query_settings=_TEXT_INDEX_SETTINGS,
        )
        return [similar_row(r) for r in rows]

    async def similar_frames(
        self,
        org: str,
        ns: str,
        *,
        kind: str,
        frame_tokens: list[str],
        paths: list[str],
        before: datetime | None,
        exclude: str = "",
        limit: int = 300,
    ) -> list[dict[str, Any]]:
        """Which of the query's frames each candidate carries, in frame
        order, for the caller to weigh by position. Issues: the body-index
        rows carrying any frame token, with one case-insensitive substring
        pass per row. Incidents (restricted namespaces only): `stack_functions`
        holds repository-relative paths, matched by suffix, plus the frame
        tokens in the body."""
        repos = self.repos()
        toks = [t for t in frame_tokens if t]
        if not repos or not toks or kind not in ("issue", "incident"):
            return []
        from server.tracker.domain.similar import case_variants

        if kind == "incident":
            self._require_restricted("incidents")
        # every spelling the index may hold: as passed, lower, UPPER, Capitalised
        # — the body index answered `alterRefreshParams` only in lower case
        # (verified live) while `tokens(body)` keeps the case
        variants = sorted({v for t in toks for v in (t, *case_variants(t.lower()))})
        wall = "AND created_at < {before:DateTime64(3)}" if before else ""
        excl = "AND source_id != {exclude:String}" if exclude else ""
        lim = max(1, min(int(limit), 500))
        params: dict[str, Any] = {"toks": toks, "variants": variants, "repos": repos, "limit": lim}
        if before:
            params["before"] = before
        if exclude:
            params["exclude"] = exclude
        if kind == "issue":
            sql = f"""
                SELECT 'issue' AS kind, source_id, url, title, created_at, closed_at,
                       primary_component, state_reason, affected_versions, error_code,
                       version_affected, toUInt8(0) AS is_merged,
                       arrayMap(p -> p > 0, multiSearchAllPositionsCaseInsensitive(
                           substring(body, 1, 20000), {{toks:Array(String)}})) AS frame_hits,
                       CAST([] AS Array(UInt8)) AS path_hits
                FROM {self.db}.github_issues FINAL
                WHERE {self._privacy()}hasAnyTokens(body, {{variants:Array(String)}})
                  {wall} {excl}
                  AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                ORDER BY created_at DESC
                LIMIT {{limit:UInt32}}
            """
        else:
            params["paths"] = [p for p in paths if p] or ["\x00loom-no-path"]
            sql = f"""
                SELECT 'incident' AS kind, source_id, url, title, created_at, closed_at,
                       '' AS state_reason, primary_component,
                       CAST([] AS Array(String)) AS affected_versions, error_code,
                       toUInt8(0) AS is_merged,
                       arrayMap(p -> p > 0, multiSearchAllPositionsCaseInsensitive(
                           substring(body, 1, 20000), {{toks:Array(String)}})) AS frame_hits,
                       arrayMap(f -> arrayExists(s -> endsWith(s, f), stack_functions),
                                {{paths:Array(String)}}) AS path_hits
                FROM {self.db}.core_incidents FINAL
                WHERE (hasAnyTokens(body, {{variants:Array(String)}})
                       OR arrayExists(s -> arrayExists(f -> endsWith(s, f),
                                                       {{paths:Array(String)}}), stack_functions))
                  {wall} {excl}
                ORDER BY created_at DESC
                LIMIT {{limit:UInt32}}
            """
        rows = await self._rows(
            f"similar_frames_{kind}",
            sql,
            params,
            cache_key=[kind, toks, params.get("paths"), before, exclude, repos, lim],
            budget_ms=self._similar_budget(),
            query_settings=_TEXT_INDEX_SETTINGS,
        )
        return [
            {
                **similar_row(r),
                "frame_hits": [bool(int(x)) for x in (r.get("frame_hits") or [])],
                "path_hits": [bool(int(x)) for x in (r.get("path_hits") or [])],
            }
            for r in rows
        ]

    async def similar_mentions(
        self,
        org: str,
        ns: str,
        *,
        numbers: list[int],
        kinds: list[str],
        before: datetime | None,
        exclude: str = "",
        max_numbers: int = 10,
    ) -> list[dict[str, Any]]:
        """The items a report names by number (≤ `max_numbers`, 10 for the
        mentions leg; the vector leg hydrates its 50 hits through the same
        keyed read), as candidates in the wire shape —
        keyed reads of the issue and PR tables under the wall, in the order
        of `numbers`."""
        repos = self.repos()
        nums = sorted({int(n) for n in numbers if int(n) > 0})[: max(1, min(int(max_numbers), 200))]
        if not repos or not nums:
            return []
        refs = [f"{repo}#{n}" for repo in repos for n in nums]
        wall = "AND created_at < {before:DateTime64(3)}" if before else ""
        excl = "AND source_id != {exclude:String}" if exclude else ""
        params: dict[str, Any] = {"refs": refs}
        if before:
            params["before"] = before
        if exclude:
            params["exclude"] = exclude
        priv = self._privacy()
        parts: list[str] = []
        if "issue" in kinds:
            parts.append(f"""
                SELECT 'issue' AS kind, source_id, url, title, created_at, closed_at,
                       primary_component, state_reason, affected_versions, error_code,
                       version_affected, toUInt8(0) AS is_merged
                FROM {self.db}.github_issues FINAL
                WHERE {priv}source_id IN {{refs:Array(String)}} {wall} {excl}""")
        if "pr" in kinds:
            parts.append(f"""
                SELECT 'pr' AS kind, source_id, url, title, created_at, closed_at,
                       primary_component, '' AS state_reason,
                       CAST([] AS Array(String)) AS affected_versions, toUInt32(0) AS error_code,
                       '' AS version_affected, is_merged
                FROM {self.db}.github_prs FINAL
                WHERE {priv}source_id IN {{refs:Array(String)}} {wall} {excl}""")
        if not parts:
            return []
        rows = await self._rows(
            "similar_mentions",
            " UNION ALL ".join(parts),
            params,
            cache_key=["mentions", refs, sorted(kinds), before, exclude],
            budget_ms=self._similar_budget(),
        )
        out = [similar_row(r) for r in rows]
        order = {n: i for i, n in enumerate(numbers)}
        out.sort(key=lambda r: (order.get(int(r["number"]), 99), r["kind"]))
        return out

    async def similar_vector(
        self,
        org: str,
        ns: str,
        *,
        number: int,
        before: datetime | None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Nearest issues by the upstream `embedding` column to the query
        item's own vector — QA's space, never loom's model. Rows with a zero
        vector are skipped; a full scan of 2,560-dim vectors measured 1.5 s
        live (off by default, `LOOM_TRACKER_SIMILAR_VECTOR_LEG`)."""
        repos = self.repos()
        if not repos or int(number) <= 0:
            return []
        refs = [f"{repo}#{int(number)}" for repo in repos]
        wall = "AND created_at < {before:DateTime64(3)}" if before else ""
        priv = self._privacy()
        sql = f"""
            WITH (SELECT embedding FROM {self.db}.github_issues FINAL
                  WHERE {priv}source_id IN {{refs:Array(String)}} LIMIT 1) AS q
            SELECT 'issue' AS kind, source_id, url, title, created_at, closed_at, primary_component,
                   state_reason, affected_versions, error_code, toUInt8(0) AS is_merged,
                   cosineDistance(embedding, q) AS distance
            FROM {self.db}.github_issues FINAL
            WHERE {priv}source_id NOT IN {{refs:Array(String)}}
              AND arraySum(arrayMap(x -> abs(x), embedding)) > 0
              AND arraySum(arrayMap(x -> abs(x), q)) > 0
              {wall}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            ORDER BY distance ASC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), 200))
        params: dict[str, Any] = {"refs": refs, "repos": repos, "limit": lim}
        if before:
            params["before"] = before
        rows = await self._rows(
            "similar_vector",
            sql,
            params,
            cache_key=["vec", refs, before, repos, lim],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
        )
        return [{**similar_row(r), "distance": float(r.get("distance") or 0.0)} for r in rows]

    # -- tracker.duplicate_check's gold ---------------------------------------------

    async def duplicate_gold(self, org: str, ns: str, *, limit: int = 500) -> list[dict[str, Any]]:
        """The positives of the duplicate fit (the duplicate gold, read here
        for the weekly refit): the issues QA closed as `duplicate` whose
        `related_to` edge names an OLDER issue, each with its creation time
        (the replay's wall) and its targets. Newest first. A pass-time read,
        budgeted as a scan (106 rows in ~200 ms live)."""
        repos = self.repos()
        if not repos:
            return []
        lim = max(1, min(int(limit), 2000))
        priv = self._privacy()
        sql = f"""
            SELECT d.source_id AS dup, d.created_at AS dup_created,
                   groupArray(t.source_id) AS targets
            FROM (SELECT id, source_id, created_at FROM {self.db}.github_issues FINAL
                  WHERE {priv}state_reason = 'duplicate'
                    AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}) AS d
            JOIN {self.db}.edges AS e
              ON (e.from_id = d.id OR e.to_id = d.id) AND e.rel_type = 'related_to'
            JOIN (SELECT id, source_id, created_at FROM {self.db}.github_issues FINAL
                  WHERE {priv}splitByChar('#', source_id)[1] IN {{repos:Array(String)}}) AS t
              ON t.id = if(e.from_id = d.id, e.to_id, e.from_id)
            WHERE t.created_at < d.created_at
            GROUP BY dup, dup_created
            ORDER BY dup_created DESC
            LIMIT {{limit:UInt32}}
        """
        rows = await self._rows(
            "duplicate_gold",
            sql,
            {"repos": repos, "limit": lim},
            cache_key=[repos, lim],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
            ttl_s=3600.0,
        )
        return [
            {
                "number": split_source(str(r.get("dup") or ""))[1],
                "created_at": upstream_dt(r.get("dup_created")),
                "targets": sorted({split_source(str(t))[1] for t in (r.get("targets") or [])}),
                "label": 1,
            }
            for r in rows
        ]

    async def duplicate_negatives(
        self,
        org: str,
        ns: str,
        *,
        since: datetime,
        until: datetime,
        limit: int = 400,
        seed: int = 10000,
    ) -> list[dict[str, Any]]:
        """The negatives of the duplicate fit: issues of the
        same period closed `completed` or `not planned` that carry no
        `related_to` / `duplicate_of` edge in either direction — a
        hash-ordered sample of `limit`, the same rows every pass for one
        `seed`. A negative nobody labelled as a duplicate is the known noise
        of this set. Budgeted as a scan (5,077 candidates live)."""
        repos = self.repos()
        if not repos:
            return []
        lim = max(1, min(int(limit), 5000))
        sql = f"""
            SELECT source_id, created_at
            FROM {self.db}.github_issues FINAL
            WHERE {self._privacy()}state_reason IN ('completed', 'not planned')
              AND created_at >= {{since:DateTime64(3)}} AND created_at <= {{until:DateTime64(3)}}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
              AND id NOT IN (SELECT from_id FROM {self.db}.edges
                             WHERE rel_type IN ('related_to', 'duplicate_of'))
              AND id NOT IN (SELECT to_id FROM {self.db}.edges
                             WHERE rel_type IN ('related_to', 'duplicate_of'))
            ORDER BY cityHash64(source_id, {{seed:UInt64}})
            LIMIT {{limit:UInt32}}
        """
        params = {"repos": repos, "since": since, "until": until, "limit": lim, "seed": int(seed)}
        rows = await self._rows(
            "duplicate_negatives",
            sql,
            params,
            cache_key=[repos, since, until, lim, seed],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
            ttl_s=3600.0,
        )
        return [
            {
                "number": split_source(str(r.get("source_id") or ""))[1],
                "created_at": upstream_dt(r.get("created_at")),
                "targets": [],
                "label": 0,
            }
            for r in rows
        ]
