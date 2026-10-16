"""Reads over loom.code_gh_items: the issues and pull requests the items
sync holds, open or closed, with their heads.
"""

from __future__ import annotations

from typing import Any

from server.code.store.index.base import CodeRepoBase


class GhItemsMixin(CodeRepoBase):
    """Issues and pull requests from the items sync."""

    async def open_pr_numbers(self, org: str, ns: str, *, limit: int = 4000) -> list[int]:
        """The PRs the ledger holds open, newest activity first: the set
        whose heads the mirror carries."""
        rows = await self._ch.query_all(
            """
            SELECT number FROM loom.code_gh_items FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND is_pr AND state = 'open'
            ORDER BY updated_at DESC, number DESC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "limit": int(limit)},
        )
        return [int(r["number"]) for r in rows]

    async def open_pr_heads(self, org: str, ns: str, *, limit: int = 4000) -> dict[int, str]:
        """PR number → the head sha the ledger holds for each open PR (the leader's
        mirror's `refs/pull/N/head`), newest activity first."""
        rows = await self._ch.query_all(
            """
            SELECT number, head_sha FROM loom.code_gh_items FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND is_pr AND state = 'open' AND head_sha != ''
            ORDER BY updated_at DESC, number DESC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "limit": int(limit)},
        )
        return {int(r["number"]): str(r["head_sha"]) for r in rows}

    async def open_prs_for_head_fill(
        self, org: str, ns: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Open PR rows with every persisted column: the
        PR-history worker re-upserts each with head_sha / base_sha / files
        from the mirror. Newest activity first, so a pushed head is refilled
        soon; rows already filled are read too (the fill compares the mirror's
        tip to `head_sha` and rewrites only when it moved)."""
        rows = await self._ch.query_all(
            """
            SELECT number, is_pr, state, state_reason, author, title, labels,
                   opened_at, closed_at, merged_at, updated_at, head_sha, base_sha, files
            FROM loom.code_gh_items FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND is_pr AND state = 'open'
            ORDER BY updated_at DESC, number DESC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "limit": int(limit)},
        )
        return [dict(r) for r in rows]

    async def gh_prs_matching(
        self,
        org: str,
        ns: str,
        *,
        state: str,
        tokens: list[str],
        paths: list[str],
        days: int = 0,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """Open (`state='open'`) or closed-without-merge (`state='closed'`)
        PRs of the ledger that share a file with the draft or a title token
        with it — the only rows code.pr_check's rule can
        score above zero, so the filter is lossless — most shared tokens
        first, then a shared file, then newest activity. `days` bounds the
        closed rows by `closed_at`. The title is tokenised here the way
        `code_tokens` does for the filter only; the handler rescores exactly."""
        if not tokens and not paths:
            return []
        where_state = (
            "AND state = 'open'"
            if state == "open"
            else "AND state = 'closed' AND merged_at IS NULL"
        )
        where_days = (
            "AND closed_at >= now() - INTERVAL {days:UInt32} DAY"
            if state != "open" and days > 0
            else ""
        )
        return await self._ch.query_all(
            f"""
            SELECT number, state, author, title, labels, opened_at, closed_at, updated_at, files,
                   title_hits, file_hit
            FROM (
                SELECT number, state, author, title, labels, opened_at, closed_at, updated_at,
                       files,
                       length(arrayIntersect(
                           arrayMap(x -> lower(x),
                                    extractAll(replaceAll(title, '_', ' '), '[A-Za-z0-9]+')),
                           {{tokens:Array(String)}}
                       )) AS title_hits,
                       hasAny(files, {{paths:Array(String)}}) AS file_hit
                FROM loom.code_gh_items FINAL
                WHERE org = {{org:String}} AND namespace = {{ns:String}}
                  AND tombstoned_at IS NULL AND is_pr {where_state} {where_days}
            )
            WHERE title_hits > 0 OR file_hit
            ORDER BY title_hits DESC, file_hit DESC, updated_at DESC, number DESC
            LIMIT {{limit:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "tokens": [t for t in tokens if t][:64],
                "paths": [p for p in paths if p][:200],
                "limit": int(limit),
                **({"days": int(days)} if where_days else {}),
            },
        )

    async def pr_heads_by_number(
        self, org: str, ns: str, numbers: list[int]
    ) -> dict[int, dict[str, Any]]:
        """number → {head_sha, base_sha, files} for the rows the items sync is
        about to rewrite: a ReplacingMergeTree upsert replaces
        the whole row, so the sync carries the fill forward instead of
        blanking it on every GitHub update."""
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:1000]
        if not nums:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT number, head_sha, base_sha, files FROM loom.code_gh_items FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND number IN {nums:Array(UInt32)}
              AND head_sha != ''
            """,
            {"org": org, "ns": ns, "nums": nums},
        )
        return {int(r["number"]): dict(r) for r in rows}

    async def gh_pr_head(self, org: str, ns: str, number: int) -> dict[str, Any] | None:
        """One PR's ledger row with its head: title, state,
        author, head_sha, base_sha, files. None when the ledger lacks the
        number or it is not a PR."""
        if int(number) <= 0:
            return None
        row = await self._ch.query_one(
            """
            SELECT number, state, author, title, labels, head_sha, base_sha, files, updated_at
            FROM loom.code_gh_items FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND is_pr AND number = {n:UInt32}
            LIMIT 1
            """,
            {"org": org, "ns": ns, "n": int(number)},
        )
        return dict(row) if row else None

    async def items_by_number(
        self, org: str, ns: str, numbers: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Issue / PR facts from loom.code_gh_items (any state) keyed by
        number: is_pr, state, state_reason, author, title, opened_at,
        closed_at, merged_at. The source of record for existence, kind and
        state-as-of; code_pr_history stays the record for merged PRs' files."""
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:64]
        if not nums:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT number, is_pr, state, state_reason, author, title,
                   opened_at, closed_at, merged_at, updated_at
            FROM loom.code_gh_items FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND number IN {nums:Array(UInt32)}
            """,
            {"org": org, "ns": ns, "nums": nums},
        )
        return {int(r["number"]): r for r in rows}

    async def gh_item_titles(self, org: str, ns: str, numbers: list[int]) -> dict[int, str]:
        """number -> title for issues/PRs the items sync already holds; the
        lexeme writer folds linked-issue titles in without a GitHub call."""
        nums = sorted({int(n) for n in numbers if n})[:200]
        if not nums:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT number, title FROM loom.code_gh_items FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND number IN {nums:Array(UInt32)} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "nums": nums},
        )
        return {int(r["number"]): str(r["title"] or "") for r in rows}
