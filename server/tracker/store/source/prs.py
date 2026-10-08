"""One PR's facts: its files, diff, cause statements, test and CI facts;
the fixes and backports around an issue; changelog entries and a person's
activity.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from server.tracker.store.source.base import SourceBase
from server.tracker.store.source.rows import doc_row, split_source, str_list, upstream_dt
from server.tracker.store.source.sql import ORIGINAL_PR_PRED, all_tokens, text_tokens

# the enclosing declaration git prints after every hunk header; bound as a
# parameter so the RE2 escapes survive the client
_HUNK_CONTEXT_RE = r"@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ([^\n]+)"

# A maintainer's cause statement in a PR description — the
# verbs a fix uses to name the change that broke it (live: 4 of 6 stated-culprit
# regressions carry one in the fix PR).
CAUSE_RE = (
    r"(?i)(?:caused by|introduced (?:in|by)|regression (?:from|introduced (?:by|in)|in)|"
    r"regressed (?:in|by)|broken (?:in|by)|broke in)[:\s]+"
    r"(?:https://github\.com/[\w.-]+/[\w.-]+/pull/|#)([0-9]{3,7})"
)


class PrReads(SourceBase):
    """Readers of individual PRs and of the PRs around an issue."""

    async def pr_files(
        self, org: str, ns: str, numbers: list[int], *, max_paths: int = 40
    ) -> dict[int, list[str]]:
        """PR number → the paths its diff touched, across the
        namespace's repositories; a number missing from the result was not a
        PR here."""
        repos = self.repos()
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:64]
        if not repos or not nums:
            return {}
        rows = await self._rows(
            "pr_files",
            f"""
            SELECT pr_number, arraySlice(affected_file_paths, 1, {{cap:UInt32}}) AS paths
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}pr_number IN {{nums:Array(Int64)}}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            LIMIT {{lim:UInt32}}
            """,
            {
                "nums": nums,
                "repos": repos,
                "cap": max(1, min(int(max_paths), 200)),
                "lim": len(nums),
            },
            cache_key=["pr_files", nums, repos, max_paths],
        )
        out: dict[int, list[str]] = {}
        for r in rows:
            try:
                out[int(r.get("pr_number") or 0)] = [str(p) for p in (r.get("paths") or [])]
            except (TypeError, ValueError):
                continue
        return out

    async def pr_change(self, org: str, ns: str, number: int) -> dict[str, Any] | None:
        """One PR's change as the backport question needs it:
        the first release that contains it, its base branch, the files it
        touched, and the enclosing declarations its hunks name — the
        `@@ … @@ <context>` lines extracted UPSTREAM so the diff text never
        leaves the QA service. None when the record has no such PR."""
        repos = self.repos()
        n = int(number)
        if n <= 0 or not repos:
            return None
        refs = [f"{repo}#{n}" for repo in repos]
        sql = f"""
            SELECT source_id, pr_number, title, url, author, base_branch, minor_version,
                   is_merged, is_backport_pr, is_cherrypick_pr, is_reverted, upstream_pr_number,
                   backport_required, changelog_category, src_components, affected_file_paths,
                   closed_at, merged_at,
                   arrayDistinct(extractAll(diff, {{hunk_re:String}})) AS hunk_contexts
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}source_id IN {{refs:Array(String)}}
            LIMIT 1
        """
        # a keyed row, but the hunk extraction runs over a diff of up to 512 KB
        # and a cold read crossed 300 ms once in five (live): the
        # text budget
        rows = await self._rows(
            "pr_change",
            sql,
            {"refs": refs, "hunk_re": _HUNK_CONTEXT_RE},
            cache_key=refs,
            budget_ms=self._text_budget(),
        )
        if not rows:
            return None
        r = rows[0]
        repo, num = split_source(str(r.get("source_id") or ""))
        return {
            "repo": repo,
            "pr_number": int(r.get("pr_number") or num),
            "title": str(r.get("title") or ""),
            "url": str(r.get("url") or ""),
            "author": str(r.get("author") or ""),
            "base_branch": str(r.get("base_branch") or ""),
            "minor_version": str(r.get("minor_version") or ""),
            "is_merged": int(r.get("is_merged") or 0),
            "is_backport_pr": int(r.get("is_backport_pr") or 0),
            "is_cherrypick_pr": int(r.get("is_cherrypick_pr") or 0),
            "is_reverted": int(r.get("is_reverted") or 0),
            "upstream_pr_number": int(r.get("upstream_pr_number") or 0),
            "backport_required": int(r.get("backport_required") or 0),
            "changelog_category": str(r.get("changelog_category") or ""),
            "src_components": str_list(r.get("src_components")),
            "files": str_list(r.get("affected_file_paths")),
            "merged_at": upstream_dt(r.get("merged_at")) or upstream_dt(r.get("closed_at")),
            "hunk_contexts": str_list(r.get("hunk_contexts"))[:64],
        }

    async def pr_diff_sections(self, org: str, ns: str, number: int) -> list[str]:
        """One PR's diff as per-file sections for the hunk-level presence
        test: the diff is split UPSTREAM at every
        `diff --git`, the sections of tests / docs / CI / utilities are
        dropped there, each section is cut at 64 KB and the diff itself at
        512 KB, so the wire carries the source hunks alone. `[]` when the
        record has no such PR or no diff. The text budget: a scan by bytes."""
        from server.tracker.domain.patch_presence import skip_pattern

        repos = self.repos()
        n = int(number)
        if n <= 0 or not repos:
            return []
        refs = [f"{repo}#{n}" for repo in repos]
        sql = f"""
            SELECT arrayMap(
                       s -> substring(s, 1, 65536),
                       arrayFilter(
                           s -> s != '' AND NOT match(s, {{skip_re:String}}),
                           splitByString('\\ndiff --git ',
                                         concat('\\n', substring(diff, 1, 524288)))
                       )
                   ) AS sections
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}source_id IN {{refs:Array(String)}}
            LIMIT 1
        """
        rows = await self._rows(
            "pr_diff_sections",
            sql,
            {"refs": refs, "skip_re": skip_pattern()},
            cache_key=refs,
            budget_ms=self._text_budget(),
        )
        if not rows:
            return []
        return str_list(rows[0].get("sections"))

    async def prs_facts(
        self, org: str, ns: str, numbers: list[int], *, names: list[str] | None = None
    ) -> dict[int, dict[str, Any]]:
        """Per merged PR number (≤ 128): test presence, CI result, first
        release, backport flag, and which of `names` its diff text carries —
        the symbol-level touch loom's file-level PR record cannot see.
        Computed upstream on the keyed rows only."""
        repos = self.repos()
        # 128, not 64: symbol_history's window is 120 PRs and a
        # 64 cap left 28% of its rows `unknown` live; 128 keyed rows with the
        # diff scan read 1.2 GB in 146 ms server-side
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:128]
        want = sorted({str(x) for x in (names or []) if x})[:32]
        if not repos or not nums:
            return {}
        refs = [f"{repo}#{n}" for repo in repos for n in nums]
        sql = f"""
            SELECT source_id, pr_number, has_test, ci_result, minor_version, is_backport_pr,
                   is_merged, closed_at,
                   arrayFilter(s -> position(diff, s) > 0, {{names:Array(String)}})
                       AS matched_symbols
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}source_id IN {{refs:Array(String)}}
        """
        # with names the read touches `diff` (6.7 GB uncompressed): 64 keyed rows
        # pulled 894 MB through the granules in 136 ms warm (live) and
        # tripped the point budget under the brief's six-way fan-out — a scan by
        # bytes, budgeted as one; without names it is a 1 MB point read
        rows = await self._rows(
            "prs_facts",
            sql,
            {"refs": refs, "names": want},
            cache_key=[refs, want],
            budget_ms=self._text_budget() if want else None,
        )
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            repo, number = split_source(str(r.get("source_id") or ""))
            n = int(r.get("pr_number") or number)
            has_test = r.get("has_test")
            out[n] = {
                "repo": repo,
                "pr_number": n,
                "has_test": -1 if has_test is None else int(has_test),
                "ci_result": str(r.get("ci_result") or ""),
                "minor_version": str(r.get("minor_version") or ""),
                "is_backport_pr": int(r.get("is_backport_pr") or 0),
                "is_merged": int(r.get("is_merged") or 0),
                "merged_at": upstream_dt(r.get("closed_at")),
                "matched_symbols": str_list(r.get("matched_symbols")),
            }
        return out

    async def cause_statements(
        self, org: str, ns: str, numbers: list[int], *, since: str
    ) -> list[dict[str, Any]]:
        """Merged PRs, merged on or after `since`, whose description states
        that one of `numbers` caused a regression ("Caused by #N", "introduced
        by #N", "regression from #N"): the maintainer's own
        bisect, read from the record instead of by `gh pr view` on every
        candidate. One bounded read, without FINAL (a replaced row repeats a
        statement; the caller dedupes, and FINAL tripled the scan); each row
        carries the naming PR's number, title, merge time, changed files and
        the numbers it names."""
        repos = self.repos()
        wanted = sorted({int(n) for n in numbers if int(n or 0) > 0})[:40]
        if not repos or not wanted:
            return []
        needles = [f"#{n}" for n in wanted] + [f"/pull/{n}" for n in wanted]
        refs = "arrayMap(x -> toUInt32OrZero(x), extractAll(body, {re:String}))"
        return await self._rows(
            "cause_statements",
            f"""
            SELECT pr_number, title, merged_at, affected_file_paths AS files,
                   arrayFilter(x -> has({{nums:Array(UInt32)}}, x) AND x != pr_number,
                               arrayDistinct({refs})) AS names
            FROM {self.db}.github_prs
            WHERE {self._privacy()}is_merged = 1 AND merged_at >= {{since:String}}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
              AND multiSearchAny(body, {{needles:Array(String)}})
              AND hasAny({refs}, {{nums:Array(UInt32)}})
            LIMIT 50
            """,
            {"re": CAUSE_RE, "since": since, "repos": repos, "needles": needles, "nums": wanted},
            cache_key=["cause_statements", wanted, repos, since],
            budget_ms=self._text_budget(),
        )

    async def causes_named_by(self, org: str, ns: str, number: int) -> list[int]:
        """The PR numbers one PR's own description names as the
        cause of what it fixes ("Caused by #N", the same `CAUSE_RE` as
        `cause_statements`), in the order written. One keyed read."""
        repos = self.repos()
        if not repos or int(number or 0) <= 0:
            return []
        rows = await self._rows(
            "causes_named_by",
            f"""
            SELECT arrayMap(x -> toUInt32OrZero(x), extractAll(body, {{re:String}})) AS names
            FROM {self.db}.github_prs
            WHERE {self._privacy()}pr_number = {{n:UInt32}}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            LIMIT 1
            """,
            {"re": CAUSE_RE, "n": int(number), "repos": repos},
            cache_key=["causes_named_by", int(number), repos],
            budget_ms=self._text_budget(),
        )
        names = [int(x) for x in ((rows[0].get("names") or []) if rows else []) if int(x or 0) > 0]
        return [n for n in dict.fromkeys(names) if n != int(number)]

    async def fix_prs_of_issue(self, org: str, ns: str, number: int) -> list[dict[str, Any]]:
        """The PRs whose `fixes` edge points at issue `number`,
        with the flags that tell the trunk fix from its derivatives. One
        statement: the edge range (`rel_type` leads the sort key, our end is
        derived) feeds a keyed-by-id read of the PR rows; a
        PR's id is not in its sort key, so the read is a scan of two narrow
        columns and gets the text budget, not the 300 ms point-read budget."""
        from server.tracker.domain.namespaces import doc_id, item_kinds, ref_of

        repos = self.repos()
        n = int(number)
        if n <= 0 or not repos:
            return []
        assert self.cfg is not None
        issue_kind = item_kinds(bool(self.cfg.is_private))[0]
        ids = sorted(doc_id(issue_kind, ref_of(repo, n)) for repo in repos)
        sql = f"""
            SELECT source_id, pr_number, title, url, base_branch, minor_version, is_merged,
                   is_backport_pr, is_cherrypick_pr, closed_at
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}id IN (
                SELECT from_id FROM {self.db}.edges
                WHERE rel_type = 'fixes' AND to_id IN {{ids:Array(UUID)}}
            )
            ORDER BY is_merged DESC, is_backport_pr, pr_number DESC
            LIMIT 32
        """
        rows = await self._rows(
            "issue_fixes",
            sql,
            {"ids": ids},
            cache_key=ids,
            budget_ms=self._text_budget(),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            repo, num = split_source(str(r.get("source_id") or ""))
            out.append(
                {
                    "repo": repo,
                    "pr_number": int(r.get("pr_number") or num),
                    "title": str(r.get("title") or ""),
                    "url": str(r.get("url") or ""),
                    "base_branch": str(r.get("base_branch") or ""),
                    "minor_version": str(r.get("minor_version") or ""),
                    "is_merged": int(r.get("is_merged") or 0),
                    "is_backport_pr": int(r.get("is_backport_pr") or 0),
                    "is_cherrypick_pr": int(r.get("is_cherrypick_pr") or 0),
                    "closed_at": upstream_dt(r.get("closed_at")),
                }
            )
        return out

    async def fix_prs_of_issues(
        self, org: str, ns: str, issues: list[tuple[str, int]]
    ) -> dict[tuple[str, int], list[dict[str, Any]]]:
        """`fix_prs_of_issue` for every issue an answer shows,
        in one statement. `issues` are (repo, number); a repo the namespace
        does not index is dropped. The edge's issue end (`to_id`) comes back
        with each PR row so the rows map to their issue: the ids are derived
        locally, never looked up."""
        from server.tracker.domain.namespaces import doc_id, item_kinds, ref_of

        repos = set(self.repos())
        if not repos or self.cfg is None:
            return {}
        issue_kind = item_kinds(bool(self.cfg.is_private))[0]
        by_id: dict[str, tuple[str, int]] = {}
        for repo, n in issues:
            if repo in repos and int(n) > 0:
                by_id[doc_id(issue_kind, ref_of(repo, int(n)))] = (repo, int(n))
        if not by_id:
            return {}
        ids = sorted(by_id)
        sql = f"""
            SELECT toString(e.to_id) AS issue_id, p.pr_number AS pr_number, p.title AS title,
                   p.state AS state, p.base_branch AS base_branch,
                   p.minor_version AS minor_version, p.is_merged AS is_merged,
                   p.is_backport_pr AS is_backport_pr, p.closed_at AS merged_at
            FROM (
                SELECT from_id, to_id FROM {self.db}.edges
                WHERE rel_type = 'fixes' AND to_id IN {{ids:Array(UUID)}}
            ) AS e
            INNER JOIN (
                SELECT id, pr_number, title, state, base_branch, minor_version, is_merged,
                       is_backport_pr, closed_at
                FROM {self.db}.github_prs FINAL
                WHERE {self._privacy()}id IN (
                    SELECT from_id FROM {self.db}.edges
                    WHERE rel_type = 'fixes' AND to_id IN {{ids:Array(UUID)}}
                )
            ) AS p ON p.id = e.from_id
            ORDER BY p.is_merged DESC, p.is_backport_pr, p.pr_number DESC
            LIMIT 256
        """
        rows = await self._rows(
            "issues_fixes",
            sql,
            {"ids": ids},
            cache_key=ids,
            budget_ms=self._text_budget(),
        )
        out: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for r in rows:
            key = by_id.get(str(r.get("issue_id") or ""))
            n = int(r.get("pr_number") or 0)
            # an edge can be written twice upstream: one row per (issue, PR)
            if key is None or n in {p["pr_number"] for p in out.get(key, [])}:
                continue
            out.setdefault(key, []).append(
                {
                    "pr_number": n,
                    "title": str(r.get("title") or ""),
                    "state": str(r.get("state") or ""),
                    "base_branch": str(r.get("base_branch") or ""),
                    "minor_version": str(r.get("minor_version") or ""),
                    "is_merged": int(r.get("is_merged") or 0),
                    "is_backport_pr": int(r.get("is_backport_pr") or 0),
                    "merged_at": upstream_dt(r.get("merged_at")),
                }
            )
        return out

    async def backport_prs_of(self, org: str, ns: str, number: int) -> list[dict[str, Any]]:
        """The backport PRs of one upstream PR: rows flagged
        `is_backport_pr` whose `upstream_pr_number` is the number or whose
        title is the automation's "Backport #N to X:", with the branch they
        target and whether they merged. The flag and the column lag the
        title upstream, so both forms are read."""
        repos = self.repos()
        n = int(number)
        if n <= 0 or not repos:
            return []
        sql = f"""
            SELECT source_id, pr_number, title, url, base_branch, minor_version, is_merged,
                   state, closed_at, merged_at
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}is_backport_pr = 1
              AND (upstream_pr_number = {{n:UInt32}} OR startsWith(title, {{prefix:String}}))
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            ORDER BY base_branch, pr_number
            LIMIT 64
        """
        # the flag admits ~6% of the rows and neither column is in the sort
        # key: a narrow scan, 140 ms alone and over 300 ms beside the diff
        # read (live) — the text budget, not the point-read budget
        rows = await self._rows(
            "backports_of",
            sql,
            {"n": n, "prefix": f"Backport #{n} ", "repos": repos},
            cache_key=[n, repos],
            budget_ms=self._text_budget(),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            repo, num = split_source(str(r.get("source_id") or ""))
            merged = int(r.get("is_merged") or 0)
            state = str(r.get("state") or "")
            if merged:
                state = "merged"
            elif not state:
                state = "closed" if upstream_dt(r.get("closed_at")) else "open"
            out.append(
                {
                    "repo": repo,
                    "pr_number": int(r.get("pr_number") or num),
                    "title": str(r.get("title") or ""),
                    "url": str(r.get("url") or ""),
                    "base_branch": str(r.get("base_branch") or ""),
                    "minor_version": str(r.get("minor_version") or ""),
                    "state": state,
                    "merged_at": upstream_dt(r.get("merged_at"))
                    or (upstream_dt(r.get("closed_at")) if merged else None),
                    "source": "qa",
                }
            )
        return out

    async def backport_wanted_prs(self, org: str, ns: str, *, since: datetime) -> list[int]:
        """The merged trunk PRs since `since` the record marks
        `backport_required` (the QA half of "labelled for
        backport" — `github_prs` carries no labels column, the flag is what
        it has). Backports and cherry-picks never. One narrow scan over the
        window under the text budget (the flag is not in the sort key)."""
        repos = self.repos()
        if not repos:
            return []
        sql = f"""
            SELECT pr_number
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}is_merged = 1 AND backport_required = 1
              AND is_backport_pr = 0 AND is_cherrypick_pr = 0
              AND merged_at >= {{since:DateTime64(3)}}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            ORDER BY pr_number DESC
            LIMIT 5000
        """
        rows = await self._rows(
            "backport_wanted",
            sql,
            {"since": since, "repos": repos},
            cache_key=[repos, since],
            budget_ms=self._text_budget(),
        )
        return [int(r.get("pr_number") or 0) for r in rows if int(r.get("pr_number") or 0) > 0]

    async def changelog_entries(
        self, org: str, ns: str, *, version: str, term: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        """Merged PRs first released in `version` with a changelog entry,
        narrowed to those whose entry or title carries the term's tokens."""
        repos = self.repos()
        if not repos:
            return []
        toks = text_tokens(term)
        lim = max(1, min(int(limit), 100))
        params: dict[str, Any] = {"v": version, "repos": repos, "limit": lim}
        term_cond = (
            f"AND ({all_tokens('changelog_entry', toks, params)}"
            f" OR {all_tokens('title', toks, params)})"
            if len(" ".join(toks)) >= 3
            else ""
        )
        sql = f"""
            SELECT source_id, pr_number, url, title, author, changelog_category,
                   substring(changelog_entry, 1, 400) AS changelog_entry, has_test, closed_at
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}is_merged = 1 AND minor_version = {{v:String}}
              AND changelog_category NOT IN ('', 'not_for_changelog', 'ci', 'build')
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}} {term_cond}
            ORDER BY changelog_category, closed_at DESC
            LIMIT {{limit:UInt32}}
        """
        # categories are the upstream snake_case keys (verified live:
        # improvement, bug_fix, performance, new_feature, … and the three above)
        rows = await self._rows(
            "changelog",
            sql,
            params,
            cache_key=[version, toks, repos, lim],
            budget_ms=self._text_budget(),
        )
        return [
            {
                "pr_number": int(r.get("pr_number") or 0),
                "url": str(r.get("url") or ""),
                "title": str(r.get("title") or "")[:140],
                "author": str(r.get("author") or ""),
                "category": str(r.get("changelog_category") or ""),
                "entry": str(r.get("changelog_entry") or ""),
                "merged_at": upstream_dt(r.get("closed_at")),
                "has_test": None if r.get("has_test") is None else bool(int(r["has_test"])),
            }
            for r in rows
        ]

    async def person_activity(
        self, org: str, ns: str, *, login: str, days: int = 7, limit: int = 20
    ) -> dict[str, Any]:
        """What one GitHub login merged, was asked to review, and opened in
        the window; escalations they authored when the namespace is
        restricted."""
        repos = self.repos()
        if not repos or not login:
            return {}
        lim = max(1, min(int(limit), 100))
        params = {"login": login, "days": self._window(days), "repos": repos, "limit": lim}
        merged, reviewing, opened = await asyncio.gather(
            self._rows(
                "person_merged",
                f"""
                SELECT source_id, pr_number, url, title, has_test, minor_version, closed_at
                FROM {self.db}.github_prs FINAL
                WHERE {self._privacy()}is_merged = 1 AND author = {{login:String}}
                  AND {ORIGINAL_PR_PRED}
                  AND closed_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
                  AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                ORDER BY closed_at DESC LIMIT {{limit:UInt32}}""",
                params,
                cache_key=["m", params],
            ),
            self._rows(
                "person_reviews",
                f"""
                SELECT source_id, pr_number, url, title, author, state, closed_at, updated_at
                FROM {self.db}.github_prs FINAL
                WHERE {self._privacy()}has(reviewers, {{login:String}}) AND {ORIGINAL_PR_PRED}
                  AND updated_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
                  AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                ORDER BY updated_at DESC LIMIT {{limit:UInt32}}""",
                params,
                cache_key=["r", params],
            ),
            self._rows(
                "person_issues",
                f"""
                SELECT source_id, url, title, severity_level, primary_component, created_at,
                       closed_at
                FROM {self.db}.github_issues FINAL
                WHERE {self._privacy()}author = {{login:String}}
                  AND created_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
                  AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
                ORDER BY created_at DESC LIMIT {{limit:UInt32}}""",
                params,
                cache_key=["i", params],
            ),
        )
        out: dict[str, Any] = {
            "login": login,
            "days": self._window(days),
            "merged": [
                {
                    "pr_number": int(r.get("pr_number") or 0),
                    "title": str(r.get("title") or "")[:120],
                    "url": str(r.get("url") or ""),
                    "first_release": str(r.get("minor_version") or ""),
                    "has_test": None if r.get("has_test") is None else bool(int(r["has_test"])),
                    "merged_at": upstream_dt(r.get("closed_at")),
                }
                for r in merged
            ],
            "reviewing": [
                {
                    "pr_number": int(r.get("pr_number") or 0),
                    "title": str(r.get("title") or "")[:120],
                    "author": str(r.get("author") or ""),
                    "state": str(r.get("state") or ""),
                    "url": str(r.get("url") or ""),
                }
                for r in reviewing
            ],
            "opened_issues": [doc_row(r, "github_issue") for r in opened],
        }
        assert self.cfg is not None
        if self.cfg.restricted:
            esc = await self._rows(
                "person_escalations",
                f"""
                SELECT source_id, url, title, severity_level, org_name, created_at, closed_at
                FROM {self.db}.support_cases FINAL
                WHERE author = {{login:String}}
                  AND created_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
                ORDER BY created_at DESC LIMIT {{limit:UInt32}}""",
                params,
                cache_key=["e", params],
            )
            out["escalated"] = [doc_row(r, "support_case") for r in esc]
        return out
