"""The vectors loom computes itself: the embedding window and its items, the
local vector leg, and the crash-fingerprint reads the duplicate check
keys on.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from server.settings import settings
from server.tracker.store.embed import EmbedHit, TrackerEmbedRepo
from server.tracker.store.source.base import SourceBase
from server.tracker.store.source.rows import split_source, upstream_dt

# Error name → code, per (org, namespace)
_ERROR_NAMES: dict[tuple[str, str], tuple[float, dict[str, int]]] = {}
# a ClickHouse server error line: `Code: 9. DB::Exception: … (SIZES_OF_COLUMNS_DOESNT_MATCH)`
_ERROR_LINE_PAT = r"Code: (\d{1,4})\. DB::Exception:[^\n]{0,400}?\(([A-Z][A-Z0-9_]{3,})\)"


class EmbedReads(SourceBase):
    """Readers behind loom's own tracker embeddings."""

    async def embed_window_keys(
        self,
        org: str,
        ns: str,
        *,
        kind: str,
        closed_days: int,
        stamp: str = "",
    ) -> list[dict[str, Any]]:
        """Every item of the embedding window — open, or closed within
        `closed_days` — as (source_id, updated_at) only: the set the pass
        tombstones against. A range scan (~30k PRs, ~11k issues on the public
        record) under the widest budget; `stamp` keys the pass's own cache
        entry so two passes never share one. At most `tracker_embed_window_keys_limit` rows,
        in key order: a read that returns that many may have stopped short,
        and the caller must not tombstone against it."""
        repos = self.repos()
        if not repos or kind not in ("issue", "pr"):
            return []
        table = "github_issues" if kind == "issue" else "github_prs"
        priv = self._privacy()
        rows = await self._rows(
            "embed_keys",
            f"""SELECT source_id, updated_at
                FROM {self.db}.{table} FINAL
                WHERE {priv}splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                  AND (closed_at <= toDateTime64(0, 3)
                       OR closed_at >= now64(3) - INTERVAL {{days:UInt32}} DAY)
                ORDER BY source_id
                LIMIT {{lim:UInt32}}""",
            {
                "repos": repos,
                "days": max(1, int(closed_days)),
                "lim": settings.tracker_embed_window_keys_limit,
            },
            cache_key=["embed_keys", kind, closed_days, stamp],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
            ttl_s=1.0,
        )
        return [
            {
                "source_id": str(r.get("source_id") or ""),
                "updated_at": upstream_dt(r.get("updated_at")),
            }
            for r in rows
        ]

    async def embed_items_by_number(
        self,
        org: str,
        ns: str,
        *,
        kind: str,
        numbers: list[int],
        body_chars: int,
        stamp: str = "",
    ) -> list[dict[str, Any]]:
        """The window's items the table lacks, by number: the
        same columns as `embed_items`, keyed on `source_id IN`, no cursor
        and no window predicate — the caller already knows the numbers are
        in the window. At most 500 numbers per statement."""
        repos = self.repos()
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:500]
        if not repos or kind not in ("issue", "pr") or not nums:
            return []
        table = "github_issues" if kind == "issue" else "github_prs"
        priv = self._privacy()
        refs = [f"{repo}#{n}" for repo in repos for n in nums]
        rows = await self._rows(
            "embed_items_by_number",
            f"""SELECT source_id, title, substring(body, 1, {{chars:UInt32}}) AS body,
                       primary_component, created_at, updated_at, closed_at
                FROM {self.db}.{table} FINAL
                WHERE {priv}source_id IN {{refs:Array(String)}}
                ORDER BY updated_at ASC, source_id ASC
                LIMIT {{limit:UInt32}}""",
            {"refs": refs, "chars": max(1, int(body_chars)), "limit": len(refs)},
            cache_key=["embed_items_by_number", kind, body_chars, refs, stamp],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
            ttl_s=1.0,
        )
        return [self._embed_item(kind, r) for r in rows]

    async def embed_items(
        self,
        org: str,
        ns: str,
        *,
        kind: str,
        since: datetime | None,
        closed_days: int,
        body_chars: int,
        limit: int = 2000,
        stamp: str = "",
        since_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """One page of the window's items changed since the cursor, oldest
        change first: source_id, title, the body head the embedding is
        computed over, component, created / updated / closed times. With
        `since_id` the cursor is the keyset (updated_at, source_id) — the
        page order — so a page boundary inside one `updated_at` second
        resumes after the last row read instead of skipping the rest of
        that second; without it, `updated_at > since` (the catch-up's read)."""
        repos = self.repos()
        if not repos or kind not in ("issue", "pr"):
            return []
        table = "github_issues" if kind == "issue" else "github_prs"
        priv = self._privacy()
        cursor = ""
        if since is not None:
            cursor = (
                "AND updated_at > {since:DateTime64(3)}"
                if since_id is None
                else "AND (updated_at, source_id) > ({since:DateTime64(3)}, {since_id:String})"
            )
        params: dict[str, Any] = {
            "repos": repos,
            "days": max(1, int(closed_days)),
            "chars": max(1, int(body_chars)),
            "limit": max(1, min(int(limit), 5000)),
        }
        if since is not None:
            params["since"] = since
            if since_id is not None:
                params["since_id"] = since_id
        rows = await self._rows(
            "embed_items",
            f"""SELECT source_id, title, substring(body, 1, {{chars:UInt32}}) AS body,
                       primary_component, created_at, updated_at, closed_at
                FROM {self.db}.{table} FINAL
                WHERE {priv}splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                  AND (closed_at <= toDateTime64(0, 3)
                       OR closed_at >= now64(3) - INTERVAL {{days:UInt32}} DAY)
                  {cursor}
                ORDER BY updated_at ASC, source_id ASC
                LIMIT {{limit:UInt32}}""",
            params,
            cache_key=["embed_items", kind, closed_days, body_chars, since, since_id, limit, stamp],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
            ttl_s=1.0,
        )
        return [self._embed_item(kind, r) for r in rows]

    @staticmethod
    def _embed_item(kind: str, r: dict[str, Any]) -> dict[str, Any]:
        _repo, number = split_source(str(r.get("source_id") or ""))
        return {
            "kind": kind,
            "source_id": str(r.get("source_id") or ""),
            "number": number,
            "title": str(r.get("title") or ""),
            "body": str(r.get("body") or ""),
            "component": str(r.get("primary_component") or ""),
            "created_at": upstream_dt(r.get("created_at")),
            "updated_at": upstream_dt(r.get("updated_at")),
            "closed_at": upstream_dt(r.get("closed_at")),
        }

    async def upstream_embeddings_populated(self, org: str, ns: str) -> bool:
        """Whether QA's own `embedding` column is being filled for this
        namespace: at least half of its 200 newest issues carry a NON-ZERO
        vector (the canonical alternative — if the QA team
        fills the column, the `tracker_embed` pass is a no-op and the leg reads
        upstream first, the similar reader). Non-zero, never length: every row
        carries a 2,560-dim vector and it is zeros on all but 2 of 22,265
        public issues and 263 of 62,767 PRs — stragglers, not
        a filled column, and none of them among the newest 200. 200
        vectors, not the table: a full scan of the column measured 1.5 s."""
        repos = self.repos()
        if not repos:
            return False
        priv = self._privacy()
        rows = await self._rows(
            "embed_upstream",
            f"""SELECT countIf(arraySum(arrayMap(x -> abs(x), embedding)) > 0) AS nonzero,
                       count() AS n
                FROM (SELECT embedding FROM {self.db}.github_issues FINAL
                      WHERE {priv}splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                      ORDER BY created_at DESC LIMIT 200)""",
            {"repos": repos},
            cache_key=["embed_upstream", repos],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
            ttl_s=3600.0,
        )
        row = rows[0] if rows else {}
        n, nonzero = int(row.get("n") or 0), int(row.get("nonzero") or 0)
        return n > 0 and nonzero * 2 >= n

    async def similar_vector_local(
        self,
        org: str,
        ns: str,
        *,
        vector: list[float],
        dim: int,
        kinds: list[str],
        before: datetime | None,
        exclude: tuple[str, int] | None,
        limit: int = 50,
        repo: Any = None,
    ) -> list[EmbedHit]:
        """tracker.similar's vector leg over loom's own table:
        the same gate every tracker read passes (`_admit` — kill switch,
        configuration, the source contract, the restricted namespace's
        directory row), then `TrackerEmbedRepo.nearest` on THIS
        namespace's rows only — (kind, number, distance), no text; the hits
        that reach the answer are hydrated by key from this source after
        fusion (`server/tracker/app/similar.py`, `_hydrate`)."""
        await self._admit()
        return await (repo or TrackerEmbedRepo()).nearest(
            org,
            ns,
            vector=vector,
            dim=dim,
            limit=limit,
            kinds=kinds,
            before=before,
            exclude=exclude,
        )

    async def error_name_codes(self, org: str, ns: str) -> dict[str, int]:
        """A server error's NAME → its numeric code, read from
        the record's own bodies (`Code: 9. DB::Exception: … (NAME)`, the most
        frequent code per name). A draft that quotes `SIZES_OF_COLUMNS_DOESNT_MATCH`
        without `Code: 9` then matches the issues carrying code 9. Cached per
        source for `tracker_error_names_ttl_s`; {} without repositories, and a failed
        read raises `TrackerUnavailable`."""
        key = (self.org, self.namespace)
        hit = _ERROR_NAMES.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < settings.tracker_error_names_ttl_s:
            return hit[1]
        repos = self.repos()
        if not repos:
            return {}
        priv = self._privacy()
        rows = await self._rows(
            "error_name_codes",
            f"""SELECT name, argMax(c, n) AS code FROM (
                    SELECT g[2] AS name, toUInt32(g[1]) AS c, count() AS n
                    FROM {self.db}.github_issues FINAL
                    ARRAY JOIN extractAllGroups(substring(body, 1, 20000), {{pat:String}}) AS g
                    WHERE {priv}splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                    GROUP BY name, c
                ) GROUP BY name""",
            {"repos": repos, "pat": _ERROR_LINE_PAT},
            cache_key=["error_name_codes", repos],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
        )
        out = {str(r["name"]): int(r["code"]) for r in rows if r.get("name") and r.get("code")}
        _ERROR_NAMES[key] = (now, out)
        return out

    async def fingerprint_issues(
        self,
        org: str,
        ns: str,
        *,
        error_code: int,
        stid: str,
        before: datetime | None,
        limit: int,
    ) -> list[int]:
        """The issues sharing the draft's crash fingerprint —
        its STID, or its server error code when fewer than `limit` issues
        carry that code (a code on hundreds of issues, `LOGICAL_ERROR`, says
        nothing) — newest first, filed before `before`."""
        repos = self.repos()
        if not repos or (error_code <= 0 and not stid):
            return []
        priv = self._privacy()
        wall = "AND created_at < {before:DateTime64(3)}" if before is not None else ""
        params: dict[str, Any] = {"repos": repos, "code": int(error_code), "stid": stid}
        if before is not None:
            params["before"] = before
        rows = await self._rows(
            "fingerprint_issues",
            f"""SELECT source_id, stid = {{stid:String}} AND {{stid:String}} != '' AS by_stid
                FROM {self.db}.github_issues FINAL
                WHERE {priv}splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                  AND ((error_code = {{code:UInt32}} AND {{code:UInt32}} > 0)
                       OR (stid = {{stid:String}} AND {{stid:String}} != ''))
                  {wall}
                ORDER BY by_stid DESC, created_at DESC
                LIMIT {{lim:UInt32}}""",
            {**params, "lim": int(limit) + 1},
            cache_key=["fingerprint_issues", repos, error_code, stid, str(before)],
        )
        by_stid = [r for r in rows if r.get("by_stid")]
        by_code = [r for r in rows if not r.get("by_stid")]
        if len(rows) > limit:  # the code is too common to be a fingerprint
            by_code = []
        return [split_source(str(r.get("source_id") or ""))[1] for r in [*by_stid, *by_code]][
            :limit
        ]
