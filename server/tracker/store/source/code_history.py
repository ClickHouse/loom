"""Readers that join the code index to the tracker record: the merged PRs
of a window that touched given paths, the bisect scan, a component's
directories and the risk calibration's outcomes.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from server.settings import settings
from server.tracker.store.source.base import SourceBase
from server.tracker.store.source.conventions import code_path_sql, code_prefixes_for
from server.tracker.store.source.rows import ch_datetime, split_source, str_list, upstream_dt
from server.tracker.store.source.sql import ORIGINAL_PR_PRED


def _scan_scope(dirs: bool, scope_pats: bool, code_prefixes: list[str] | None = None) -> str:
    """The bisect scan's admission predicate: the frames' files, their
    directories, or the diff carrying a rare pattern
    — each leg a parameter, OR-ed."""
    legs = ["hasAny(affected_file_paths, {paths:Array(String)})"]
    if dirs:
        legs.append(
            "arrayExists(f -> arrayExists(d -> startsWith(f, d), {dirs:Array(String)}), "
            "affected_file_paths)"
        )
    if scope_pats:
        # a culprit is a code change: a changelog or version-bump PR carries
        # every identifier in its diff and never introduced a bug; a PR that
        # touches no `src/` file cannot have either
        legs.append(
            "(multiSearchAny(diff, {scope_pats:Array(String)})"
            f" AND arrayExists(f -> {code_path_sql('f', code_prefixes or [])}, affected_file_paths)"
            " AND NOT match(title, {meta_title_re:String}))"
        )
    return legs[0] if len(legs) == 1 else "(" + " OR ".join(legs) + ")"


# the titles the pattern leg never admits
_SCAN_META_TITLE_RE = r"(?i)changelog|version_date|\bcontributors\b|\bbump\b"


class CodeHistoryReads(SourceBase):
    """Window scans over the merged PRs, keyed by the code they touched."""

    def _code_prefixes(self) -> list[str]:
        """This tracker's source-code path prefixes, or []."""
        cfg = getattr(self, "cfg", None)
        return code_prefixes_for(cfg.credential) if cfg is not None else []

    async def prs_touching_paths(
        self,
        org: str,
        ns: str,
        paths: list[str],
        *,
        before: datetime | None = None,
        days: int = 365,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """The pull requests — open or merged, opened in
        the last `days` and before the wall — whose diff touched any of the
        draft's `paths`, open ones first, newest first; each with the paths
        it shares with the draft (`hit_paths`). One array-membership scan of
        the PR table (121 ms live on 65k rows); the fix a PR names is read
        from the graph by the caller."""
        repos = self.repos()
        wanted = [str(x).strip() for x in paths if str(x).strip()][:40]
        if not repos or not wanted:
            return []
        lim = max(1, min(int(limit), 100))
        wall = "AND created_at < {before:DateTime64(3)}" if before else ""
        params: dict[str, Any] = {
            "paths": wanted,
            "repos": repos,
            "lim": lim,
            "days": max(1, int(days)),
        }
        if before:
            params["before"] = before
        rows = await self._rows(
            "prs_touching_paths",
            f"""
            SELECT pr_number, source_id, state, is_merged, merged_at, created_at, title,
                   arrayFilter(x -> has({{paths:Array(String)}}, x), affected_file_paths)
                       AS hit_paths
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}hasAny(affected_file_paths, {{paths:Array(String)}})
              AND created_at > now() - INTERVAL {{days:UInt32}} DAY {wall}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            ORDER BY is_merged ASC, created_at DESC
            LIMIT {{lim:UInt32}}
            """,
            params,
            cache_key=["prs_touching_paths", wanted, repos, before, days, lim],
            budget_ms=self._text_budget(),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                n = int(r.get("pr_number") or 0)
            except (TypeError, ValueError):
                continue
            if n <= 0:
                continue
            out.append(
                {
                    "pr_number": n,
                    "source_id": str(r.get("source_id") or ""),
                    "state": "merged" if r.get("is_merged") else str(r.get("state") or "open"),
                    "merged_at": upstream_dt(r.get("merged_at")),
                    "created_at": upstream_dt(r.get("created_at")),
                    "title": str(r.get("title") or ""),
                    "hit_paths": [str(x) for x in (r.get("hit_paths") or [])][:40],
                }
            )
        return out

    async def prs_touching_prefix(
        self, org: str, ns: str, *, path_prefix: str, days: int = 30, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Merged PRs of the window whose file list has a path under
        `path_prefix`, newest first — "what changed here lately"."""
        repos = self.repos()
        if not repos or not path_prefix:
            return []
        sql = f"""
            SELECT source_id, pr_number, url, title, author, primary_component, has_test,
                   is_backport_pr, minor_version, closed_at,
                   arrayFilter(p -> startsWith(p, {{prefix:String}}), affected_file_paths)
                       AS touched
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}is_merged = 1 AND {ORIGINAL_PR_PRED}
              AND closed_at >= now64(3) - INTERVAL {{days:UInt32}} DAY
              AND arrayExists(p -> startsWith(p, {{prefix:String}}), affected_file_paths)
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            ORDER BY closed_at DESC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), 100))
        rows = await self._rows(
            "prs_prefix",
            sql,
            {"prefix": path_prefix, "days": self._window(days), "repos": repos, "limit": lim},
            cache_key=[path_prefix, days, repos, lim],
            budget_ms=self._text_budget(),
        )
        return [
            {
                "pr_number": int(r.get("pr_number") or 0),
                "url": str(r.get("url") or ""),
                "title": str(r.get("title") or "")[:140],
                "author": str(r.get("author") or ""),
                "component": str(r.get("primary_component") or ""),
                "has_test": None if r.get("has_test") is None else bool(int(r["has_test"])),
                "is_backport": bool(int(r.get("is_backport_pr") or 0)),
                "first_release": str(r.get("minor_version") or ""),
                "merged_at": upstream_dt(r.get("closed_at")),
                "touched": str_list(r.get("touched"))[:8],
            }
            for r in rows
        ]

    async def merged_prs_touching(
        self,
        org: str,
        ns: str,
        paths: list[str],
        versions: list[str],
        *,
        symbols: list[str] | None = None,
        patterns: list[str] | None = None,
        dirs: list[str] | None = None,
        scope_patterns: list[str] | None = None,
        pattern_weights: dict[str, float] | None = None,
        limit: int = 20,
        before: datetime | None = None,
        after: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Merged, non-derivative PRs whose FIRST release is one of `versions`
        — or, with no versions, merged at or before `before` (and after `after` when given):
        the window a reviewer has when a bug surfaced while reading a PR and no release has
        carried it yet — and whose file list touches one of `paths` — or, when `dirs` is
        given, any file under one of those directories (the widened candidate set) — or,
        when `scope_patterns` is given, whose DIFF carries one of those literals (the
        frameless scan: the window's diffs are read once, ~1.3 s live for four minors, and
        admit the rows a rare identifier names). Per row, computed upstream on the filtered
        rows only: `matched_symbols` (frame names the diff carries), `matched_patterns`
        (issue patterns anywhere in the diff), `added_patterns` (patterns on a `+` line —
        the change that brought the text in) and `hunk_symbols` (frame names in a hunk
        header: the change lands inside that function). Ranked coarsely upstream — frames,
        then the IDF weight of the patterns added (`pattern_weights`, 1.0 each when absent)
        — and the caller re-ranks."""
        repos = self.repos()
        want_paths = sorted({str(p) for p in paths if p})[:64]
        want_versions = sorted({str(v) for v in versions if v})[:24]
        names = sorted({str(x) for x in (symbols or []) if x})[:32]
        pats = sorted({str(x) for x in (patterns or []) if x})[:12]
        want_dirs = sorted({str(d) for d in (dirs or []) if d})[:16]
        scope_pats = sorted({str(x) for x in (scope_patterns or []) if x})[:12]
        bounded = bool(want_versions or before)
        if not repos or not bounded or not (want_paths or want_dirs or scope_pats):
            return []
        # Releases bound the scan when the caller has them;
        # otherwise merge time does.
        if want_versions:
            window_sql = "minor_version IN {versions:Array(String)}"
        else:
            window_sql = "closed_at <= {before:DateTime}" + (
                " AND closed_at > {after:DateTime}" if after else ""
            )
        # RE2 for "an added line carrying the pattern"; the pattern itself is
        # escaped in Python, never interpolated into the statement
        added_res = [r"(?m)^\+[^\n]*" + re.escape(p) for p in pats]
        # A hunk header (`@@ … @@ <enclosing declaration>`) that
        # names the frame — the change lands INSIDE the function, where a
        # match anywhere in the diff may be a call site
        hunk_res = [r"(?m)^@@[^\n]*@@[^\n]*\b" + re.escape(s) + r"\b" for s in names]
        # the upstream order weighs an added pattern by its rarity, aligned
        # with `pats`; unit weights are a plain count
        weights = [float((pattern_weights or {}).get(p, 1.0)) for p in pats]
        # multiSearchAllPositions wants a non-empty needle list: a needle that
        # cannot occur keeps the statement shape when nothing was asked
        names = names or ["\x00loom-no-needle"]
        pats_q = pats or ["\x00loom-no-needle"]
        weights = weights or [0.0]
        scope = _scan_scope(bool(want_dirs), bool(scope_pats), self._code_prefixes())
        sql = f"""
            SELECT source_id, pr_number, title, author, closed_at, minor_version, has_test,
                   is_backport_pr, url, additions, deletions,
                   arrayFilter(p -> has({{paths:Array(String)}}, p), affected_file_paths)
                       AS matched_paths,
                   -- one pass over the diff per row for every needle (Volnitsky),
                   -- not one position() scan per needle
                   arrayFilter((s, pos) -> pos > 0, {{names:Array(String)}},
                               multiSearchAllPositions(diff, {{names:Array(String)}}))
                       AS matched_symbols,
                   arrayFilter((s, pos) -> pos > 0, {{pats:Array(String)}},
                               multiSearchAllPositions(diff, {{pats:Array(String)}}))
                       AS matched_patterns,
                   -- "on an added line": every regex in one Vectorscan pass; the
                   -- indices come back 1-based
                   if(length({{added_res:Array(String)}}) = 0, emptyArrayString(),
                      arrayMap(i -> {{pats:Array(String)}}[i],
                               multiMatchAllIndices(diff, {{added_res:Array(String)}})))
                       AS added_patterns,
                   if(length({{added_res:Array(String)}}) = 0, 0.0,
                      arraySum(arrayMap(i -> {{weights:Array(Float64)}}[i],
                                        multiMatchAllIndices(diff, {{added_res:Array(String)}}))))
                       AS added_weight,
                   if(length({{hunk_res:Array(String)}}) = 0, emptyArrayString(),
                      arrayMap(i -> {{names:Array(String)}}[i],
                               multiMatchAllIndices(diff, {{hunk_res:Array(String)}})))
                       AS hunk_symbols
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}is_merged = 1 AND is_backport_pr = 0 AND is_cherrypick_pr = 0
              AND is_sync_pr = 0 AND NOT startsWith(title, 'Backport')
              AND {window_sql}
              AND {scope}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            ORDER BY length(matched_symbols) DESC, added_weight DESC,
                     length(matched_patterns) DESC, length(matched_paths) DESC, closed_at DESC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), 60))
        params = {
            "paths": want_paths,
            "versions": want_versions,
            "names": names,
            "pats": pats_q,
            "weights": weights,
            "added_res": added_res,
            "hunk_res": hunk_res,
            "dirs": want_dirs,
            "scope_pats": scope_pats or ["\x00loom-no-needle"],
            "meta_title_re": _SCAN_META_TITLE_RE,
            "repos": repos,
            "limit": lim,
            "before": ch_datetime(before) if before else "1970-01-01 00:00:00",
            "after": ch_datetime(after) if after else "1970-01-01 00:00:00",
        }
        # the diff scan over a release range is a triage step, not an interactive
        # read: 1.6–5.4 s live on four minors of MergeTree PRs
        rows = await self._rows(
            "prs_touching",
            sql,
            params,
            cache_key=[
                want_paths,
                want_versions,
                names,
                pats,
                want_dirs,
                scope_pats,
                weights,
                repos,
                lim,
                params["before"],
                params["after"],
            ],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            repo, number = split_source(str(r.get("source_id") or ""))
            has_test = r.get("has_test")
            out.append(
                {
                    "repo": repo,
                    "pr_number": int(r.get("pr_number") or number),
                    "title": str(r.get("title") or ""),
                    "author": str(r.get("author") or ""),
                    "merged_at": upstream_dt(r.get("closed_at")),
                    "minor_version": str(r.get("minor_version") or ""),
                    "has_test": -1 if has_test is None else int(has_test),
                    "matched_paths": str_list(r.get("matched_paths")),
                    "matched_symbols": str_list(r.get("matched_symbols")),
                    "matched_patterns": str_list(r.get("matched_patterns")),
                    "added_patterns": str_list(r.get("added_patterns")),
                    "hunk_symbols": str_list(r.get("hunk_symbols")),
                    "additions": int(r.get("additions") or 0),
                    "deletions": int(r.get("deletions") or 0),
                    "url": str(r.get("url") or ""),
                }
            )
        return out

    async def window_prs(
        self,
        org: str,
        ns: str,
        versions: list[str],
        *,
        before: datetime | None = None,
        after: datetime | None = None,
        limit: int = 12000,
    ) -> list[dict[str, Any]]:
        """Every merged trunk PR of the bisect window — the same filters as
        `merged_prs_touching`, no diff: number, title, first release, merge
        time, files, the changelog line and the body head (
        the fusion's corpus, 6,878 rows / 0.2–0.3 s live for six minors).
        Releases bound it when given, else the merge-time window."""
        repos = self.repos()
        want_versions = sorted({str(v) for v in versions if v})[:24]
        if not repos or not (want_versions or before):
            return []
        conds = [
            "is_merged = 1",
            "is_backport_pr = 0",
            "is_cherrypick_pr = 0",
            "is_sync_pr = 0",
            "NOT startsWith(title, 'Backport')",
            "splitByChar('#', source_id)[1] IN {repos:Array(String)}",
        ]
        if want_versions:
            conds.append("minor_version IN {versions:Array(String)}")
        if before is not None:
            conds.append("closed_at < {before:DateTime}")
        if after is not None:
            conds.append("closed_at > {after:DateTime}")
        sql = f"""
            SELECT pr_number, title, author, closed_at, minor_version, url,
                   affected_file_paths, left(changelog_entry, 300) AS cl, left(body, 500) AS body
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}{" AND ".join(conds)}
            LIMIT {{limit:UInt32}}
        """
        wall = ch_datetime(before) if before else "1970-01-01 00:00:00"
        floor = ch_datetime(after) if after else "1970-01-01 00:00:00"
        params = {
            "repos": repos,
            "versions": want_versions,
            "before": wall,
            "after": floor,
            "limit": max(1, min(int(limit), 20000)),
        }
        rows = await self._rows(
            "window_prs",
            sql,
            params,
            # the hour is the cache's grain: reports minutes apart share one read
            cache_key=[want_versions, wall[:13], floor[:13], repos],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
        )
        return [
            {
                "pr_number": int(r.get("pr_number") or 0),
                "title": str(r.get("title") or ""),
                "author": str(r.get("author") or ""),
                "closed_at": upstream_dt(r.get("closed_at")),
                "minor_version": str(r.get("minor_version") or ""),
                "url": str(r.get("url") or ""),
                "affected_file_paths": str_list(r.get("affected_file_paths")),
                "cl": str(r.get("cl") or ""),
                "body": str(r.get("body") or ""),
            }
            for r in rows
        ]

    async def component_dirs(
        self,
        org: str,
        ns: str,
        component: str,
        *,
        limit: int = 8,
        min_lift: float = 1.5,
        days: int = 365,
    ) -> list[dict[str, Any]]:
        """The source directories a component's PRs touch:
        over the merged trunk PRs of the last `days` whose `primary_component`
        or `src_components` names `component`, the three-segment `src/`
        directories (`src/Storages/MergeTree/`) by how many of those PRs
        touched them, kept when at least three did and the share inside the
        component is `min_lift` × the share over all trunk PRs (a hub
        directory every PR touches says nothing). `[{dir, prs, lift}]`, most
        touched first, at most `limit`; one aggregate per component, kept
        `LOOM_TRACKER_BISECT_COMPONENT_TTL_S`."""
        repos = self.repos()
        comp = str(component or "").strip()
        if not repos or not comp:
            return []
        pred = self._trunk_pr_pred() + " AND closed_at >= now() - INTERVAL {days:UInt32} DAY"
        hit = "(primary_component = {c:String} OR has(src_components, {c:String}))"
        dir3 = "concat(arrayStringConcat(arraySlice(splitByChar('/', f), 1, 3), '/'), '/')"
        sql = f"""
            WITH (SELECT count() FROM {self.db}.github_prs FINAL WHERE {pred}) AS total,
                 (SELECT count() FROM {self.db}.github_prs FINAL WHERE {pred} AND {hit}) AS in_comp
            SELECT d, countIf(hit) AS n_comp,
                   (countIf(hit) / greatest(in_comp, 1)) / (count() / greatest(total, 1)) AS lift
            FROM (
                SELECT {hit} AS hit,
                       arrayJoin(arrayDistinct(arrayMap(f -> {dir3},
                           arrayFilter(f -> {code_path_sql("f", self._code_prefixes())}
                                            AND length(splitByChar('/', f)) >= 3,
                                       affected_file_paths)))) AS d
                FROM {self.db}.github_prs FINAL
                WHERE {pred}
            )
            GROUP BY d
            HAVING n_comp >= 3 AND lift >= {{min_lift:Float64}}
            ORDER BY n_comp DESC, d ASC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), 32))
        params = {
            "c": comp,
            "repos": repos,
            "days": max(30, min(int(days), 730)),
            "min_lift": float(min_lift),
            "limit": lim,
        }
        rows = await self._rows(
            "component_dirs",
            sql,
            params,
            cache_key=[comp, repos, params["days"], min_lift, lim],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
            ttl_s=float(settings.tracker_bisect_component_ttl_s),
        )
        return [
            {
                "dir": str(r.get("d") or ""),
                "prs": int(r.get("n_comp") or 0),
                "lift": round(float(r.get("lift") or 0.0), 2),
            }
            for r in rows
            if r.get("d")
        ]

    async def merged_pr_outcomes(
        self,
        org: str,
        ns: str,
        *,
        months: int = 12,
        limit: int = 20000,
    ) -> list[dict[str, Any]]:
        """Every merged original PR of the window with the three facts the
        risk calibration fits on and its label: author,
        lines added, files changed, merge time, and whether the record says
        it was reverted (`is_reverted`, or the target of a `reverts` edge)
        or an issue was traced to it (`caused_by`). One statement, oldest
        first; the label subqueries are keyed reads on `edges`' first key
        column. A pass-time read for the weekly refit, budgeted as a scan
        (12,821 rows in 160–180 ms live)."""
        repos = self.repos()
        if not repos:
            return []
        span = max(1, min(int(months), 36))
        lim = max(100, min(int(limit), 50000))
        sql = f"""
            SELECT pr_number, author, additions, changed_files_count, closed_at, is_reverted,
                   id IN (SELECT to_id FROM {self.db}.edges WHERE rel_type = 'reverts')
                       AS revert_edge,
                   id IN (SELECT to_id FROM {self.db}.edges WHERE rel_type = 'caused_by')
                       AS caused_edge
            FROM {self.db}.github_prs FINAL
            WHERE {self._privacy()}is_merged = 1 AND {ORIGINAL_PR_PRED}
              AND closed_at >= now() - INTERVAL {{months:UInt32}} MONTH
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            ORDER BY closed_at
            LIMIT {{limit:UInt32}}
        """
        rows = await self._rows(
            "pr_outcomes",
            sql,
            {"months": span, "repos": repos, "limit": lim},
            cache_key=[span, repos, lim],
            budget_ms=int(settings.tracker_live_bisect_budget_ms),
            ttl_s=3600.0,
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            label = int(
                bool(r.get("is_reverted"))
                or bool(r.get("revert_edge"))
                or bool(r.get("caused_edge"))
            )
            out.append(
                {
                    "pr_number": int(r.get("pr_number") or 0),
                    "author": str(r.get("author") or ""),
                    "lines_added": int(r.get("additions") or 0),
                    "files_changed": int(r.get("changed_files_count") or 0),
                    "merged_at": upstream_dt(r.get("closed_at")),
                    "label": label,
                    "reverted": int(bool(r.get("is_reverted")) or bool(r.get("revert_edge"))),
                    "caused": int(bool(r.get("caused_edge"))),
                }
            )
        return out
