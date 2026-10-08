"""Reads over loom.code_pr_history, the merged-PR ledger: merge commits,
history of a path, churn, co-change, the history leg of code.search, the
backfill pages, and the facts the convention rules read.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, TypedDict, cast

from server.code.store.index.base import CodeRepoBase
from server.code.store.index.sql import _branch_predicate, _hex


class PrFacts(TypedDict):
    """One merged PR's ledger row as the convention rules read it. `target_branch` is ''
    on a row the backfill has not reached; the merge sha may come back as bytes."""

    pr_number: int
    title: str
    author: str
    labels: list[str]
    body_excerpt: str
    files: list[str]
    merge_commit_sha: str | bytes
    merged_at: datetime
    target_branch: str


class PrHistoryMixin(CodeRepoBase):
    """The merged-PR ledger."""

    async def merge_commits_of(self, org: str, ns: str, pr_numbers: list[int]) -> dict[int, str]:
        """The ledger's merge commit for each PR, one statement."""
        nums = sorted({int(n) for n in pr_numbers if int(n) > 0})
        if not nums:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT pr_number, any(merge_commit_sha) AS sha FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND pr_number IN {prs:Array(UInt32)} AND tombstoned_at IS NULL
              AND merge_commit_sha != ''
            GROUP BY pr_number
            """,
            {"org": org, "ns": ns, "prs": nums},
        )
        return {int(r["pr_number"]): _hex(r.get("sha")) for r in rows or [] if r.get("sha")}

    async def merge_commit_of(self, org: str, ns: str, pr_number: int) -> str:
        """The merge commit the ledger records for a PR, or ''."""
        rows = await self._ch.query_all(
            """
            SELECT merge_commit_sha FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND pr_number = {pr:UInt32} AND tombstoned_at IS NULL AND merge_commit_sha != ''
            LIMIT 1
            """,
            {"org": org, "ns": ns, "pr": int(pr_number)},
        )
        if not rows:
            return ""
        return _hex(rows[0].get("merge_commit_sha")).strip()

    async def recent_prs(self, org: str, ns: str, *, days: int, limit: int) -> list[dict[str, Any]]:
        """Most recently merged PRs with their file lists — the input to a
        context-pack regeneration."""
        return await self._ch.query_all(
            """
            SELECT pr_number, title, merge_commit_sha, files, merged_at
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND merge_commit_sha != ''
              AND merged_at >= now() - INTERVAL {days:UInt32} DAY
            ORDER BY merged_at DESC
            LIMIT {lim:UInt32}
            """,
            {"org": org, "ns": ns, "days": days, "lim": limit},
        )

    async def merged_prs_full(self, org: str, ns: str, numbers: list[int]) -> list[dict[str, Any]]:
        """Merged PRs by number with their stored lexemes:
        the candidate pool of code.pr_check, hydrated once so every leg's
        rows score through one function."""
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:400]
        if not nums:
            return []
        return await self._ch.query_all(
            """
            SELECT pr_number, title, author, labels, files, merged_at, opened_at,
                   linked_issues, lexemes
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND pr_number IN {nums:Array(UInt32)}
            """,
            {"org": org, "ns": ns, "nums": nums},
        )

    async def prs_by_number(
        self, org: str, ns: str, numbers: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Authoritative merged-PR facts (author, merged_at, title, files,
        linked_issues) keyed by pr_number, for grounding a memory answer's PR
        claims against loom's own code plane and for the history section.
        Only merged PRs are recorded here, so a miss means the number is an
        issue, an unmerged PR, or outside the indexed window."""
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:64]
        if not nums:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT pr_number, any(title) AS title, any(author) AS author,
                   min(merged_at) AS merged_at, max(opened_at) AS opened_at,
                   any(files) AS files, any(linked_issues) AS linked_issues
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND pr_number IN {nums:Array(UInt32)}
            GROUP BY pr_number
            """,
            {"org": org, "ns": ns, "nums": nums},
        )
        return {int(r["pr_number"]): r for r in rows}

    async def pr_records_by_number(
        self, org: str, ns: str, numbers: list[int]
    ) -> dict[int, dict[str, Any]]:
        """The ledger row a candidate's marks read: title,
        body excerpt, merge commit and merge time per PR number (≤ 64) — the
        revert / re-land / names-the-function reads of `code.bisect_candidates`,
        and the sha its bounded diff runs on. Keyed by pr_number; a PR the
        ledger lacks is absent (marked nothing, never demoted)."""
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:64]
        if not nums:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT pr_number, any(title) AS title, any(body_excerpt) AS body_excerpt,
                   any(merge_commit_sha) AS merge_commit_sha, min(merged_at) AS merged_at,
                   any(linked_issues) AS linked_issues
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND pr_number IN {nums:Array(UInt32)}
            GROUP BY pr_number
            """,
            {"org": org, "ns": ns, "nums": nums},
        )
        return {int(r["pr_number"]): r for r in rows}

    async def prs_linking_issue(
        self,
        org: str,
        ns: str,
        numbers: list[int],
        *,
        limit: int = 9,
    ) -> list[dict[str, Any]]:
        """Merged PRs whose description closes / fixes / resolves any of the
        issue numbers: `hasAny(linked_issues, N)` under the
        identity prefix — the bloom_filter index prunes to
        the PR's granules — newest first, bot authors excluded, reverts
        marked. Plain WHERE."""
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:16]
        if not nums:
            return []
        rows = await self._ch.query_all(
            """
            SELECT pr_number, title, author, merged_at, files, linked_issues,
                   (match(title, {revert_re:String})
                    OR positionCaseInsensitive(body_excerpt, 'this reverts commit') > 0
                   ) AS is_revert
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL
              AND NOT endsWith(author, '[bot]') AND NOT startsWith(author, 'robot-')
              AND hasAny(linked_issues, {nums:Array(UInt32)})
            ORDER BY merged_at DESC
            LIMIT {limit:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "nums": nums,
                "limit": max(1, int(limit)),
                # a parameter, not a literal: nothing for the SQL lexer to unescape
                "revert_re": r"(?i)^\s*revert\b",
            },
        )
        return rows

    async def pr_history_for(
        self,
        org: str,
        ns: str,
        *,
        paths: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not paths:
            return []
        return await self._ch.query_all(
            """
            SELECT pr_number, title, author, labels, merged_at, files, assignees,
                   body_excerpt, linked_issues, additions, deletions,
                   -- mirrors code/domain/index/pr_signals.is_revert_pr
                   (match(title, '(?i)^\\s*revert\\b')
                    OR positionCaseInsensitive(body_excerpt, 'this reverts commit') > 0
                   ) AS is_revert
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND hasAny(files, {paths:Array(String)}) AND tombstoned_at IS NULL
            ORDER BY merged_at DESC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "paths": paths[:50], "limit": limit},
        )

    async def pr_history_under(
        self, org: str, ns: str, *, prefix: str, limit: int = 60
    ) -> list[dict[str, Any]]:
        """Merged PRs touching any file under `prefix` — the
        co-change of a directory whose files are each touched once (a
        migration, a worker module): what adding one there also touches."""
        if not prefix:
            return []
        return await self._ch.query_all(
            """
            SELECT pr_number, title, merged_at, files,
                   (match(title, '(?i)^\\s*revert\\b')
                    OR positionCaseInsensitive(body_excerpt, 'this reverts commit') > 0
                   ) AS is_revert
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND arrayExists(f -> startsWith(f, {prefix:String}), files)
              AND tombstoned_at IS NULL
            ORDER BY merged_at DESC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "prefix": prefix, "limit": int(limit)},
        )

    async def co_change_counts(
        self, org: str, ns: str, *, def_paths: list[str], near_paths: list[str]
    ) -> dict[str, int]:
        """For each definition path, how many merged PRs touched it together
        with at least one of `near_paths` — the co-change key of
        code.symbol's context ranking. Both `hasAny` are
        served by the files bloom index; PRs touching more than 200 files
        (mass renames, formatter sweeps) co-change with everything and do not
        vote. Returns {def_path: prs} for the paths that co-changed at all."""
        defs = list(dict.fromkeys(p for p in def_paths if p))[:50]
        near = list(dict.fromkeys(p for p in near_paths if p))[:50]
        if not defs or not near:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT def_path, count() AS prs
            FROM (
                SELECT arrayJoin(arrayIntersect(files, {defs:Array(String)})) AS def_path
                FROM loom.code_pr_history FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tombstoned_at IS NULL AND length(files) <= 200
                  AND hasAny(files, {near:Array(String)})
                  AND hasAny(files, {defs:Array(String)})
            )
            GROUP BY def_path
            """,
            {"org": org, "ns": ns, "defs": defs, "near": near},
        )
        return {str(r["def_path"]): int(r["prs"]) for r in rows}

    async def pr_history_in_window(
        self,
        org: str,
        ns: str,
        path: str,
        *,
        since: datetime,
        until: datetime,
        limit: int = 200,
        branch: str = "",
    ) -> list[dict[str, Any]]:
        """Merged PRs whose file list names `path` and whose merge landed in
        `[since, until]` — the ledger leg of code.blame_range.
        `merge_commit_sha` rides along so the caller can keep only
        the merges inside the window's commit graph; `body_excerpt` feeds the
        `introduced` stage's revert / reverted / names-the-function reads.
        `branch`: the namespace's branch —
        a row the ledger knows merged elsewhere (a backport filed under the
        master namespace before the base filter) is not a candidate."""
        if not path:
            return []

        return await self._ch.query_all(
            f"""
            SELECT pr_number, title, author, labels, merged_at, merge_commit_sha, linked_issues,
                   body_excerpt
            FROM loom.code_pr_history FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND has(files, {{path:String}}) AND tombstoned_at IS NULL
              AND merged_at >= {{since:DateTime}} AND merged_at <= {{until:DateTime}}
              {_branch_predicate(branch)}
            ORDER BY merged_at DESC
            LIMIT {{limit:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "path": path,
                "since": since,
                "until": until,
                "limit": limit,
                "branch": branch,
            },
        )

    async def pr_churn_touching(
        self,
        org: str,
        ns: str,
        *,
        paths: list[str],
        prefix: str = "",
        since: datetime,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Merged PRs since `since` whose file list names one of `paths` or a
        file under `prefix` — the churn leg of tracker.test_signal: how often
        the test's home file(s) and the sources the test map
        ties to it changed in the ledger's window. Newest first."""
        wanted = sorted({str(p) for p in paths if p})[:40]
        pref = str(prefix or "")
        if not wanted and not pref:
            return []
        conds = []
        if wanted:
            conds.append("hasAny(files, {paths:Array(String)})")
        if pref:
            conds.append("arrayExists(f -> startsWith(f, {prefix:String}), files)")
        touch = " OR ".join(conds)
        return await self._ch.query_all(
            f"""
            SELECT pr_number, title, merged_at
            FROM loom.code_pr_history FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND tombstoned_at IS NULL AND merged_at >= {{since:DateTime}}
              AND ({touch})
            ORDER BY merged_at DESC
            LIMIT {{limit:UInt32}}
            """,
            {"org": org, "ns": ns, "paths": wanted, "prefix": pref, "since": since, "limit": limit},
        )

    async def labelled_fixes_in_window(
        self,
        org: str,
        ns: str,
        *,
        since: datetime,
        label_prefix: str = "pr-must-backport",
        limit: int = 2000,
    ) -> list[dict[str, Any]]:
        """Merged trunk PRs since `since` carrying a label that starts with
        `label_prefix` — the maintainers' own "this fix is owed to a branch"
        (`pr-must-backport`, `pr-must-backport-synced`), the
        automation's own "Backport #N" and "Cherry pick" rows never. One
        statement over the namespace's window; newest first."""
        return await self._ch.query_all(
            """
            SELECT pr_number, title, labels, merged_at, merge_commit_sha
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND merged_at >= {since:DateTime}
              AND arrayExists(l -> startsWith(l, {prefix:String}), labels)
              AND NOT startsWith(title, 'Backport #') AND NOT startsWith(title, 'Cherry pick')
            ORDER BY merged_at DESC
            LIMIT {limit:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "since": since,
                "prefix": str(label_prefix),
                "limit": max(1, min(int(limit), 5000)),
            },
        )

    # Merged-PR text scored like the chunk lexical leg: sum idf x tf over the
    # app-tokenized `lexemes` column, idf bound positionally. Bot authors and
    # reverts do not vote; a PR with no files has nothing to vote for. Plain
    # WHERE throughout (no PREWHERE in the code plane).

    _PR_VOTER_FILTER = (
        "AND tombstoned_at IS NULL AND notEmpty(files)\n"
        "              AND NOT endsWith(author, '[bot]') AND NOT startsWith(author, 'robot-')\n"
        # Rendered SQL carries \\s and \\b: ClickHouse's string literal
        # unescapes once, and a single \b would be a backspace.
        "              AND NOT match(title, '(?i)^\\\\s*revert\\\\b')\n"
    )

    _PR_HAYSTACK = "concat(' ', lexemes, ' ')"

    async def pr_history_search(
        self,
        org: str,
        ns: str,
        tokens: list[str],
        idfs: list[float],
        *,
        pool: int = 30,
        before: Any = None,
    ) -> list[dict[str, Any]]:
        """Top `pool` merged PRs by lexical score with the files they touched.
        `before` pins the history to PRs merged strictly before it."""
        if not tokens:
            return []
        hay = self._PR_HAYSTACK
        where_before = "AND merged_at < {before:DateTime}" if before is not None else ""
        return await self._ch.query_all(
            f"""
            SELECT pr_number, title, author, files, merged_at, linked_issues,
                   arraySum(
                       arrayMap(
                           (tok, idf) -> idf * countSubstrings({hay}, concat(' ', tok, ' ')),
                           {{tokens:Array(String)}}, {{idfs:Array(Float32)}}
                       )
                   ) AS pr_score
            FROM loom.code_pr_history FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              {self._PR_VOTER_FILTER}
              AND multiSearchAny(
                      {hay}, arrayMap(t -> concat(' ', t, ' '), {{tokens:Array(String)}})
                  )
              {where_before}
            ORDER BY pr_score DESC, merged_at DESC
            LIMIT {{pool:UInt32}}
            """,
            {
                "org": org,
                "ns": ns,
                "tokens": tokens,
                "idfs": idfs,
                "pool": pool,
                **({"before": before} if before is not None else {}),
            },
        )

    async def pr_corpus_stats(
        self,
        org: str,
        ns: str,
        tokens: list[str],
        *,
        before: Any = None,
    ) -> tuple[list[int], int]:
        """(df per token — positional, same order as `tokens` — and N voting
        PRs) over the PR corpus itself, not the chunk corpus: a word that is
        rare in code ("crash", "segfault") is common in bug-fix descriptions
        and must weigh accordingly."""
        if not tokens:
            return [], 0
        hay = self._PR_HAYSTACK
        where_before = "AND merged_at < {before:DateTime}" if before is not None else ""
        row = await self._ch.query_one(
            f"""
            SELECT toUInt64(count()) AS n,
                   sumForEach(
                       arrayMap(t -> toUInt64(position({hay}, concat(' ', t, ' ')) > 0),
                                {{tokens:Array(String)}})
                   ) AS dfs
            FROM loom.code_pr_history FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              {self._PR_VOTER_FILTER}
              {where_before}
            """,
            {
                "org": org,
                "ns": ns,
                "tokens": tokens,
                **({"before": before} if before is not None else {}),
            },
        )
        if not row:
            return [0] * len(tokens), 0
        dfs = [int(x) for x in (row.get("dfs") or [])]
        dfs += [0] * (len(tokens) - len(dfs))
        return dfs[: len(tokens)], int(row.get("n") or 0)

    async def merge_shas_page(
        self, org: str, ns: str, *, below_pr: int = 0, limit: int = 500
    ) -> list[tuple[int, str]]:
        """(pr_number, merge sha) of live ledger rows with pr_number below
        `below_pr` (0 = from the top), newest number first — one page of the
        ingest worker's missing-merge sweep. Keyed on the
        sort key; reads two narrow columns."""
        rows = await self._ch.query_all(
            """
            SELECT pr_number, toString(merge_commit_sha) AS sha
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
              AND ({below:UInt32} = 0 OR pr_number < {below:UInt32})
              AND merge_commit_sha != ''
            ORDER BY pr_number DESC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "below": max(0, int(below_pr)), "limit": max(1, int(limit))},
        )
        return [(int(r["pr_number"]), str(r["sha"] or "").rstrip("\x00")) for r in rows]

    async def pr_churn_for(
        self, org: str, ns: str, paths: list[str], *, days: int = 365
    ) -> dict[str, int]:
        """Merged-PR count per path over the trailing window. A FLOOR:
        code_pr_history only reaches as far back as the PR backfill has run,
        so a path can be hotter than its count says — never colder."""
        if not paths:
            return {}
        rows = await self._ch.query_all(
            """
            SELECT f AS path, uniqExact(pr_number) AS prs
            FROM loom.code_pr_history FINAL
            ARRAY JOIN files AS f
            WHERE org = {org:String} AND namespace = {ns:String}
              AND f IN {paths:Array(String)}
              AND merged_at > now() - INTERVAL {days:UInt32} DAY
              AND tombstoned_at IS NULL
            GROUP BY f
            """,
            {"org": org, "ns": ns, "paths": paths[:50], "days": days},
        )
        return {str(r["path"]): int(r["prs"]) for r in rows}

    async def pr_churn_ranked(
        self,
        org: str,
        ns: str,
        *,
        days: int = 365,
        prefixes: Sequence[str] = (),
        exclude: Sequence[str] = (),
        limit: int = 2000,
    ) -> list[tuple[str, int]]:
        """(path, merged PRs) over the trailing window, hottest first — the
        churn order. `prefixes` keeps paths under any of them (all paths when
        empty); `exclude` drops paths under any of them at any depth. Same
        floor caveat as pr_churn_for: the history reaches as far back as the
        PR backfill has run. Plain WHERE."""
        params: dict[str, Any] = {
            "org": org,
            "ns": ns,
            "days": days,
            "limit": limit,
            "prefixes": list(prefixes),
            "exclude": list(exclude),
        }
        prefix_clause = (
            "AND arrayExists(p -> startsWith(f, p), {prefixes:Array(String)})" if prefixes else ""
        )
        exclude_clause = (
            "AND NOT arrayExists(p -> startsWith(f, p) OR position(f, concat('/', p)) > 0, "
            "{exclude:Array(String)})"
            if exclude
            else ""
        )
        rows = await self._ch.query_all(
            f"""
            SELECT f AS path, uniqExact(pr_number) AS prs
            FROM loom.code_pr_history FINAL
            ARRAY JOIN files AS f
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND merged_at > now() - INTERVAL {{days:UInt32}} DAY
              AND tombstoned_at IS NULL
              {prefix_clause}
              {exclude_clause}
            GROUP BY f
            ORDER BY prs DESC, f
            LIMIT {{limit:UInt32}}
            """,
            params,
        )
        return [(str(r["path"]), int(r["prs"])) for r in rows]

    async def pr_facts(self, org: str, ns: str, pr_number: int) -> PrFacts | None:
        """One merged PR's ledger row with what the convention rules read:
        title, labels, the body excerpt (the changelog bullet), the file
        list, the merge commit, the merge time and the branch it merged into
        (`target_branch`; '' on a row the backfill has not
        reached). Keyed on the sort key."""
        if int(pr_number) <= 0:
            return None
        row = await self._ch.query_one(
            """
            SELECT pr_number, title, author, labels, body_excerpt, files,
                   merge_commit_sha, merged_at, target_branch
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND pr_number = {pr:UInt32} AND tombstoned_at IS NULL
            LIMIT 1
            """,
            {"org": org, "ns": ns, "pr": int(pr_number)},
        )
        return cast(PrFacts, row) if row is not None else None

    async def first_owner_of_paths(
        self, org: str, ns: str, paths: list[str], *, branch: str = ""
    ) -> dict[str, tuple[int, str]]:
        """path -> (pr_number, merged_at ISO) of the EARLIEST merged PR whose
        file list names it: who added the file, as far as the ledger sees. A
        path the ledger never saw predates it. `branch` keeps the owner on
        the namespace's branch: a backport re-adding master's test never
        owns the path."""
        wanted = sorted({str(p) for p in paths if p})[:200]
        if not wanted:
            return {}

        rows = await self._ch.query_all(
            f"""
            SELECT f AS path, argMin(pr_number, merged_at) AS first_pr,
                   min(merged_at) AS first_merged_at
            FROM loom.code_pr_history FINAL
            ARRAY JOIN files AS f
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND hasAny(files, {{paths:Array(String)}}) AND tombstoned_at IS NULL
              AND f IN {{paths:Array(String)}}
              {_branch_predicate(branch)}
            GROUP BY f
            """,
            {"org": org, "ns": ns, "paths": wanted, "branch": branch},
        )
        out: dict[str, tuple[int, str]] = {}
        for r in rows:
            ts = r.get("first_merged_at")
            iso = ts.isoformat(sep=" ") if isinstance(ts, datetime) else str(ts or "")
            out[str(r["path"])] = (int(r.get("first_pr") or 0), iso)
        return out

    async def pr_category_census(
        self, org: str, ns: str, prefix: str, *, days: int = 365, branch: str = ""
    ) -> dict[str, int]:
        """`pr-*` label -> merged PRs carrying it among PRs that touched
        `prefix` over the trailing window — the changelog-category prior of
        code.conventions. Bots, reverts and file-less PRs do not vote (the
        history leg's population); a PR the ledger knows merged into another
        branch does not either. Plain WHERE."""
        if not prefix:
            return {}

        rows = await self._ch.query_all(
            f"""
            SELECT label, uniqExact(pr_number) AS prs
            FROM loom.code_pr_history FINAL
            ARRAY JOIN labels AS label
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND merged_at > now() - INTERVAL {{days:UInt32}} DAY
              {self._PR_VOTER_FILTER}
              AND startsWith(label, 'pr-')
              AND arrayExists(f -> startsWith(f, {{prefix:String}}), files)
              {_branch_predicate(branch)}
            GROUP BY label
            ORDER BY prs DESC
            LIMIT 40
            """,
            {
                "org": org,
                "ns": ns,
                "days": max(1, int(days)),
                "prefix": prefix.rstrip("/") + "/",
                "branch": branch,
            },
        )
        return {str(r["label"]): int(r["prs"]) for r in rows}

    async def pr_merge_facts(
        self, org: str, ns: str, *, pr_number: int = 0, merge_sha: str = ""
    ) -> dict[str, Any] | None:
        """One merged PR's title, file list, author and merge commit from the
        ledger, by number or by merge commit — the inputs a review brief built
        on a miss needs (the author for the revert prior). None when the
        ledger has no such PR."""
        if not pr_number and not merge_sha:
            return None
        clauses = ""
        params: dict[str, Any] = {"org": org, "ns": ns}
        if pr_number:
            clauses += " AND pr_number = {pr:UInt32}"
            params["pr"] = int(pr_number)
        if merge_sha:
            clauses += " AND merge_commit_sha = {sha:String}"
            params["sha"] = merge_sha
        return await self._ch.query_one(
            f"""
            SELECT pr_number, any(title) AS title, any(files) AS files, any(author) AS author,
                   any(merge_commit_sha) AS merge_sha, min(merged_at) AS merged_at
            FROM loom.code_pr_history FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND tombstoned_at IS NULL AND merge_commit_sha != ''{clauses}
            GROUP BY pr_number
            ORDER BY merged_at DESC
            LIMIT 1
            """,
            params,
        )

    async def backport_prs_titled(
        self, org: str, ns: str, upstream: int, *, limit: int = 8
    ) -> list[dict[str, Any]]:
        """Merged PRs in THIS namespace's ledger titled as the backport of
        `upstream` ("Backport #N to X: …") — a release-branch namespace's
        record of a cherry-pick that landed, with its merge commit (the
        ledger leg beside the tracker record). The title prefix is a
        parameter; the ledger holds merged PRs only, so a row is a merge. The
        aggregates are aliased away from their source columns: `title` is in
        the WHERE, and `any(title) AS title` is the analyzer's code 184."""
        n = int(upstream)
        if n <= 0:
            return []
        return await self._ch.query_all(
            """
            SELECT pr_number, any(title) AS pr_title, any(merge_commit_sha) AS merge_sha,
                   min(merged_at) AS merged
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND tombstoned_at IS NULL AND startsWith(title, {prefix:String})
            GROUP BY pr_number
            ORDER BY merged DESC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "prefix": f"Backport #{n} ", "limit": max(1, int(limit))},
        )

    async def pr_file_pairs(
        self, org: str, ns: str, *, min_cooccur: int = 2, limit: int = 20000
    ) -> list[dict[str, Any]]:
        """(src_path, test_path, n) pairs of source+test files that changed
        together in >= min_cooccur merged PRs."""
        return await self._ch.query_all(
            """
            SELECT src AS src_path, tst AS test_path, count() AS n
            FROM (
                SELECT pr_number,
                       arrayJoin(arrayFilter(f -> NOT startsWith(f, 'tests/'), files)) AS src,
                       arrayJoin(arrayFilter(f -> startsWith(f, 'tests/'), files)) AS tst
                FROM loom.code_pr_history FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tombstoned_at IS NULL
            )
            GROUP BY src, tst
            HAVING n >= {min_c:UInt32}
            ORDER BY n DESC
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "min_c": min_cooccur, "limit": limit},
        )

    async def prs_overlapping_paths(
        self, org: str, ns: str, paths: list[str], *, exclude_pr: int
    ) -> list[dict[str, Any]]:
        """The merged PRs that touched any of `paths` (`pr_number`, `title`, `overlap` =
        how many of them), most overlapping first, `exclude_pr` left out."""
        rows: list[dict[str, Any]] = await self._ch.query_all(
            """
            SELECT pr_number, any(title) AS title,
                   length(arrayIntersect(any(files), {paths:Array(String)})) AS overlap
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND hasAny(files, {paths:Array(String)})
              AND pr_number != {pr:UInt32} AND tombstoned_at IS NULL
            GROUP BY pr_number
            ORDER BY overlap DESC, pr_number DESC
            LIMIT 10
            """,
            {"org": org, "ns": ns, "paths": paths, "pr": exclude_pr},
        )
        return rows

    async def co_change_rows(
        self, org: str, ns: str, *, prs: int, min_together: int, min_conf: float, limit: int
    ) -> list[dict[str, Any]]:
        """File pairs changed together in the last `prs` merged PRs of 2–30 files:
        (`a`, `b`, `together`, `support_a`, `conf` = together / support of `a`), at least
        `min_together` PRs and `min_conf`, most frequent first."""
        rows: list[dict[str, Any]] = await self._ch.query_all(
            """
            WITH recent AS (
                SELECT pr_number, files FROM loom.code_pr_history FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tombstoned_at IS NULL AND length(files) BETWEEN 2 AND 30
                ORDER BY merged_at DESC LIMIT {prs:UInt32}
            ),
            support AS (SELECT a, count() AS n FROM recent ARRAY JOIN files AS a GROUP BY a)
            SELECT a, b, count() AS together, any(sa.n) AS support_a,
                   together / support_a AS conf
            FROM recent ARRAY JOIN files AS a ARRAY JOIN files AS b
            JOIN support AS sa ON sa.a = a
            WHERE a != b
            GROUP BY a, b
            HAVING together >= {k:UInt32} AND conf >= {conf:Float64}
            ORDER BY together DESC, a, b
            LIMIT {limit:UInt32}
            """,
            {"org": org, "ns": ns, "prs": prs, "k": min_together, "conf": min_conf, "limit": limit},
        )
        return rows

    async def prs_touching_in_window(
        self,
        org: str,
        ns: str,
        *,
        paths: list[str],
        dirs: list[str],
        before: datetime,
        after: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Merged PRs inside `after..before` that touched any of `paths` or a file under
        any of `dirs` (each ending in `/`), newest first."""
        rows: list[dict[str, Any]] = await self._ch.query_all(
            """
            SELECT pr_number, title, author, merged_at, files, additions, deletions
            FROM loom.code_pr_history FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
              AND merged_at < {before:DateTime} AND merged_at >= {after:DateTime}
              AND (hasAny(files, {paths:Array(String)})
                   OR arrayExists(f -> arrayExists(d -> startsWith(f, d), {dirs:Array(String)}),
                                  files))
            ORDER BY merged_at DESC
            LIMIT {lim:UInt32}
            """,
            {
                "org": org,
                "ns": ns,
                "before": before,
                "after": after,
                "paths": paths,
                "dirs": dirs,
                "lim": max(1, int(limit)),
            },
        )
        return rows
