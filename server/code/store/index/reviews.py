"""Review reads: inline review comments and reviewers from
loom.code_pr_reviews, and the stored context packs and review briefs of
merged PRs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict, cast

from server.code.store.index.base import CodeRepoBase


class ContextPackRow(TypedDict):
    """A merged PR's stored context pack. The merge sha is a FixedString and may come
    back as bytes."""

    pr_number: int
    merge_commit_sha: str | bytes
    pack: str
    pack_tokens: int
    created_at: datetime


class ReviewsMixin(CodeRepoBase):
    """Review comments, reviewers, context packs and review briefs."""

    async def reviewers_by_pr(
        self, org: str, ns: str, pr_numbers: list[int]
    ) -> dict[int, list[str]]:
        """pr_number -> distinct human inline reviewers. Feeds the reviewer
        signal of owners.rank_owners; one read for the whole PR window."""
        nums = [int(n) for n in pr_numbers if n][:500]
        if not nums:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT pr_number, groupUniqArray(author) AS reviewers
            FROM loom.code_pr_reviews FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND pr_number IN {nums:Array(UInt32)}
              AND is_bot = false AND author != '' AND tombstoned_at IS NULL
            GROUP BY pr_number
            """,
            {"org": org, "ns": ns, "nums": nums},
        )
        return {int(r["pr_number"]): [str(a) for a in (r["reviewers"] or [])] for r in rows}

    async def top_reviewers(
        self, org: str, ns: str, *, days: int = 180, limit: int = 6
    ) -> list[str]:
        """Busiest human inline reviewers of the namespace over the trailing
        window, by distinct PRs reviewed — the prior that tops up owners."""
        rows = await self._ch.query_all(
            """
            SELECT author, uniqExact(pr_number) AS prs
            FROM loom.code_pr_reviews FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND comment_created_at >= now() - INTERVAL {days:UInt32} DAY
              AND is_bot = false AND author != '' AND tombstoned_at IS NULL
            GROUP BY author
            ORDER BY prs DESC, author ASC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "days": int(days), "limit": int(limit)},
        )
        return [str(r["author"]) for r in rows]

    async def review_comments_for(
        self,
        org: str,
        ns: str,
        paths: list[str],
        *,
        exclude_pr: int = 0,
        limit: int = 30,
        humans_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Inline review comments left on these paths in earlier merged PRs,
        newest first. Bots excluded by default (their comments are checks,
        not conventions); replies to other comments excluded (the convention
        is in the opening comment, the thread is negotiation)."""
        if not paths:
            return []
        rows = await self._ch.query_all(
            """
            SELECT pr_number, comment_id, path, line, author, body, comment_created_at
            FROM loom.code_pr_reviews FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path IN {paths:Array(String)} AND tombstoned_at IS NULL
              AND pr_number != {excl:UInt32} AND in_reply_to = 0
              AND comment_id != 0
              AND (NOT {humans:Bool} OR is_bot = false)
              AND length(body) >= 40
            ORDER BY comment_created_at DESC
            LIMIT {limit:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "paths": paths[:50],
                "excl": exclude_pr,
                "humans": humans_only,
                "limit": limit,
            },
        )
        return [dict(r) for r in rows]

    async def review_comments_on_prs(
        self,
        org: str,
        ns: str,
        pr_numbers: list[int],
        paths: list[str],
        *,
        lines: tuple[int, int] = (0, 0),
        per_pr: int = 3,
        bots: list[str] | None = None,
        bot_suffixes: list[str] | None = None,
        excerpt_chars: int = 300,
        limit: int = 400,
    ) -> list[dict[str, Any]]:
        """The human inline review comments these PRs drew on
        these paths, for `code.history` / `code.blame_range` PR rows — one
        read for every PR of the answer.

        Keyed on the sort key's prefix `(org, namespace, pr_number IN …)`
        (the path bloom index prunes the rest); no FINAL: each
        `(pr_number, comment_id)` keeps its newest version (`LIMIT 1 BY`)
        before the tombstone and human filters.
        Per PR, up to `per_pr` comments whose `line` lies inside `lines`
        (`(0, 0)` = any line), newest first, and `on_file`: the PR's count
        on the paths. A PR with comments on the paths but none in the range
        still returns one row (`in_range = 0`) so its count is known."""
        nums = sorted({int(n) for n in pr_numbers if n})[:50]
        if not nums or not paths or per_pr <= 0:
            return []
        lo, hi = lines if lines != (0, 0) else (0, 4294967295)
        return await self._ch.query_all(
            """
            SELECT pr_number, comment_id, path, line, author, excerpt,
                   comment_created_at, on_file, in_range
            FROM (
                SELECT pr_number, comment_id, path, line, author, excerpt,
                       comment_created_at, in_range,
                       count() OVER (PARTITION BY pr_number) AS on_file,
                       row_number() OVER (
                           PARTITION BY pr_number, in_range
                           ORDER BY comment_created_at DESC, comment_id DESC
                       ) AS rn
                FROM (
                    SELECT pr_number, comment_id, path, line, author, is_bot,
                           tombstoned_at, comment_created_at,
                           substringUTF8(body, 1, {chars:UInt32}) AS excerpt,
                           (line >= {lo:UInt32} AND line <= {hi:UInt32}) AS in_range
                    FROM loom.code_pr_reviews
                    WHERE org = {org:String} AND namespace = {ns:String}
                      AND pr_number IN {nums:Array(UInt32)}
                      AND path IN {paths:Array(String)}
                      AND comment_id != 0
                    ORDER BY version DESC
                    LIMIT 1 BY pr_number, comment_id
                )
                WHERE tombstoned_at IS NULL AND is_bot = false AND author != ''
                  AND lower(author) NOT IN {bots:Array(String)}
                  AND NOT startsWith(lower(author), 'robot-')
                  AND NOT arrayExists(s -> endsWith(lower(author), s), {sfx:Array(String)})
            )
            WHERE (in_range AND rn <= {per:UInt32}) OR rn = 1
            ORDER BY pr_number, in_range DESC, comment_created_at DESC, comment_id DESC
            LIMIT {limit:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "nums": nums,
                "paths": list(dict.fromkeys(paths))[:20],
                "lo": max(0, int(lo)),
                "hi": max(0, int(hi)),
                "chars": max(1, int(excerpt_chars)),
                "per": int(per_pr),
                "bots": [b.lower() for b in (bots or [])],
                "sfx": [x.lower() for x in (bot_suffixes or [])],
                "limit": max(1, int(limit)),
            },
        )

    async def context_pack(self, org: str, ns: str, pr_number: int) -> ContextPackRow | None:
        row = await self._ch.query_one(
            """
            SELECT pr_number, merge_commit_sha, pack, pack_tokens, created_at
            FROM loom.code_context_packs FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND pr_number = {pr:UInt32} AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": ns, "pr": pr_number},
        )
        return cast(ContextPackRow, row) if row is not None else None

    async def review_brief(
        self, org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
    ) -> dict[str, Any] | None:
        """The stored review brief of one merged PR, by PR
        number, by merge commit, or both — the newest build when the PR
        has several merge commits. A point read on the sort-key prefix."""
        if not pr_number and not merge_sha:
            return None
        clauses = ""
        params: dict[str, Any] = {"org": org, "ns": ns}
        if pr_number:
            clauses += " AND pr_number = {pr:UInt32}"
            params["pr"] = int(pr_number)
        if merge_sha:
            clauses += " AND merge_sha = {sha:String}"
            params["sha"] = merge_sha
        return await self._ch.query_one(
            f"""
            SELECT pr_number, merge_sha, payload, sections, built_at, indexed_commit,
                   tokens, build_ms
            FROM loom.code_review_briefs FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND tombstoned_at IS NULL{clauses}
            ORDER BY built_at DESC
            LIMIT 1
            """,
            params,
        )

    async def list_context_packs(
        self, org: str, ns: str, *, limit: int, offset: int, pr_number: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """Newest-first page of live packs, decorated with the PR's title /
        author / merged_at when code_pr_history has the PR. Returns
        (rows, total_live_packs)."""
        # The filter sits inside the `p` subquery, where the outer alias is
        # not in scope: a bare `pr_number` (`p.pr_number` here is rejected
        # with UNKNOWN_IDENTIFIER).
        pr_clause = " AND pr_number = {pr:UInt32}" if pr_number else ""
        params: dict[str, Any] = {"org": org, "ns": ns, "lim": limit, "off": offset}
        if pr_number:
            params["pr"] = pr_number
        rows = await self._ch.query_all(
            f"""
            SELECT p.pr_number AS pr_number, p.merge_commit_sha AS merge_commit_sha,
                   p.pack_tokens AS pack_tokens, p.created_at AS created_at,
                   ifNull(h.title, '') AS title, ifNull(h.author, '') AS author,
                   h.merged_at AS merged_at
            FROM (
                SELECT pr_number, merge_commit_sha, pack_tokens, created_at
                FROM loom.code_context_packs FINAL
                WHERE org = {{org:String}} AND namespace = {{ns:String}}
                  AND tombstoned_at IS NULL{pr_clause}
            ) AS p
            LEFT JOIN (
                SELECT pr_number, title, author, merged_at
                FROM loom.code_pr_history FINAL
                WHERE org = {{org:String}} AND namespace = {{ns:String}}
                  AND tombstoned_at IS NULL
            ) AS h ON h.pr_number = p.pr_number
            ORDER BY p.created_at DESC, p.pr_number DESC
            LIMIT {{lim:UInt32}} OFFSET {{off:UInt32}}
            """,
            params,
        )
        total_row = await self._ch.query_one(
            f"""
            SELECT count() AS n FROM loom.code_context_packs FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND tombstoned_at IS NULL{pr_clause}
            """,
            params,
        )
        return rows, int((total_row or {}).get("n") or 0)
