"""Issue and PR records by number, their graph edges, the bug board's
counts and cells, release blockers and the nightly snapshots.
"""

from __future__ import annotations

import asyncio
from typing import Any

from server.tracker.store.source.base import SourceBase
from server.tracker.store.source.rows import (
    ISSUE_COLS,
    PR_COLS,
    doc_row,
    ids_of,
    issue_row,
    pr_row,
    str_list,
    upstream_dt,
)
from server.tracker.store.source.sql import all_tokens, any_tokens, text_match, text_tokens

# A cell's read lists every issue the board counted. The
# largest live cell is 236 open issues (Functions x low); 300
# covers every cell and the statement stays a point read (65-132 ms,
# 117 KB for MergeTree x low, 208 rows).
CELL_LIMIT = 300
# the severity order every ranked read shares
SEVERITY_NAMES = ("critical", "major", "medium", "minor", "low", "unrated")


def normalise_severities(values: list[str] | None) -> list[str]:
    """The severity names a caller spoke, as the column stores them:
    lower-case, trimmed, known, de-duplicated, in rank order. `unrated`
    is the empty level."""
    if not values:
        return []
    seen: set[str] = set()
    for v in values:
        name = str(v or "").strip().lower()
        if name in SEVERITY_NAMES:
            seen.add("" if name == "unrated" else name)
    return sorted(seen, key=lambda x: SEVERITY_NAMES.index(x or "unrated"))


def _severity_index(name: str) -> int:
    """A stored severity's place in `SEVERITY_NAMES`; empty or unknown ranks as unrated."""
    name = (name or "").strip().lower()
    return SEVERITY_NAMES.index(name if name in SEVERITY_NAMES else "unrated")


_SEVERITY_RANK = (
    "multiIf(severity_level = 'critical', 0, severity_level = 'major', 1,"
    " severity_level = 'medium', 2, severity_level = 'low', 3, 4)"
)


class ItemReads(SourceBase):
    """Readers of the item tables, the edge graph and the nightly snapshots."""

    async def item_counts(self, org: str, ns: str) -> dict[str, Any]:
        """How much the source holds and how fresh it is, under the
        namespace's predicate (the dashboard's source card):
        `{issues, prs, last_refresh_at}`. Two bounded count reads, cached.
        """
        priv = self._privacy()

        async def one(table: str) -> dict[str, Any]:
            rows = await self._rows(
                f"count:{table}",
                f"SELECT count() AS n, max(updated_at) AS newest FROM {self.db}.{table} FINAL"
                f" WHERE {priv}1",
                {},
                cache_key=[table],
            )
            return rows[0] if rows else {}

        issues, prs = await asyncio.gather(one("github_issues"), one("github_prs"))
        newest = max(
            (str(r.get("newest") or "") for r in (issues, prs) if r.get("newest")), default=""
        )
        return {
            "issues": int(issues.get("n") or 0),
            "prs": int(prs.get("n") or 0),
            "last_refresh_at": newest,
        }

    async def items_by_number(
        self, org: str, ns: str, numbers: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Issue and PR rows keyed by number (≤ 64), across the namespace's
        repositories. A number is repository-global for one repository; with
        several repositories the first match wins."""
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:64]
        repos = self.repos()
        if not nums or not repos:
            return {}
        refs = [f"{repo}#{n}" for repo in repos for n in nums]
        priv = self._privacy()
        # two key reads side by side: the issue and PR column lists differ in
        # width, so one UNION ALL is refused (verified live)
        issues, prs = await asyncio.gather(
            self._rows(
                "items_issues",
                f"""SELECT 'issue' AS kind, {ISSUE_COLS}
                    FROM {self.db}.github_issues FINAL
                    WHERE {priv}source_id IN {{refs:Array(String)}}""",
                {"refs": refs},
                cache_key=refs,
            ),
            self._rows(
                "items_prs",
                f"""SELECT 'pr' AS kind, {PR_COLS}
                    FROM {self.db}.github_prs FINAL
                    WHERE {priv}source_id IN {{refs:Array(String)}}""",
                {"refs": refs},
                cache_key=refs,
            ),
        )
        out: dict[int, dict[str, Any]] = {}
        for r in [*issues, *prs]:
            row = issue_row(r) if r.get("kind") == "issue" else pr_row(r)
            if row["number"] > 0 and row["number"] not in out:
                out[row["number"]] = row
        return out

    async def issue_text(self, org: str, ns: str, number: int, *, chars: int = 6000) -> str:
        """Title and the head of the body of one issue — the text the bisect
        derives its greppable patterns from. One keyed read."""
        repos = self.repos()
        if not repos or int(number) <= 0:
            return ""
        refs = [f"{repo}#{int(number)}" for repo in repos]
        sql = f"""
            SELECT title, substring(body, 1, {{chars:UInt32}}) AS body
            FROM {self.db}.github_issues FINAL
            WHERE {self._privacy()}source_id IN {{refs:Array(String)}}
            LIMIT 1
        """
        rows = await self._rows(
            "issue_text",
            sql,
            {"refs": refs, "chars": max(200, min(int(chars), 20000))},
            cache_key=[refs, chars],
        )
        if not rows:
            return ""
        return f"{rows[0].get('title') or ''}\n{rows[0].get('body') or ''}"

    async def edges_from(
        self,
        org: str,
        ns: str,
        refs: list[str],
        rel_types: list[str] | None = None,
        *,
        budget_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """The graph around `refs` (`repo#number`): edges asserted FROM them
        (`direction = out`) and TO them (`direction = in`), the other end
        resolved to its `repo#number`. An end the credential cannot see is
        dropped with its edge."""
        wanted = sorted({str(r) for r in refs if r})[:64]
        if not wanted:
            return []
        rels = sorted({str(r) for r in (rel_types or []) if r})
        rel_pred = " AND rel_type IN {rels:Array(String)}" if rels else ""
        assert self.cfg is not None
        ours = ids_of(wanted, bool(self.cfg.is_private))
        ids = sorted(ours)
        # our ends are derived, not looked up: one keyed read of
        # the edges table on both sides, then the other ends resolved
        sql = f"""
            SELECT rel_type, from_type, to_type, from_id, to_id, created_at
            FROM {self.db}.edges
            WHERE (from_id IN {{ids:Array(UUID)}} OR to_id IN {{ids:Array(UUID)}}){rel_pred}
            LIMIT 2000
        """
        # `edges` is keyed (rel_type, from_id, to_id) but a read by our ids is a
        # range scan whatever its shape (live, typed parameters: OR of
        # the two sides 223k rows / 249 ms warm; the two keyed legs unioned 413k
        # rows / 387 ms; cold 600–2,600 ms) — the point-read budget lost the
        # whole QA half of four ops to it, so the graph reads under the scan
        # budget
        raw = await self._rows(
            "edges",
            sql,
            {"ids": ids, "rels": rels},
            cache_key=[ids, rels],
            budget_ms=budget_ms or self._text_budget(),
        )
        edges: list[dict[str, Any]] = []
        for e in raw:
            fid, tid = str(e.get("from_id") or ""), str(e.get("to_id") or "")
            if fid in ours:
                edges.append(
                    {
                        **e,
                        "our_ref": ours[fid][0],
                        "other_id": tid,
                        "direction": "out",
                        "other_type": str(e.get("to_type") or ""),
                    }
                )
            elif tid in ours:
                edges.append(
                    {
                        **e,
                        "our_ref": ours[tid][0],
                        "other_id": fid,
                        "direction": "in",
                        "other_type": str(e.get("from_type") or ""),
                    }
                )
        if not edges:
            return []
        other_ids = sorted({str(e["other_id"]) for e in edges if e["other_id"]})[:512]
        kinds = {str(e["other_type"]) for e in edges}
        resolved = await self._resolve_ids(
            other_ids, kinds, tag="edge_refs", budget_ms=budget_ms or self._text_budget()
        )
        by_id = {str(r["id"]): str(r["source_id"]) for r in resolved}
        out: list[dict[str, Any]] = []
        for e in edges:
            other = by_id.get(str(e.get("other_id")))
            if not other:
                continue
            outward = e.get("direction") == "out"
            out.append(
                {
                    "src_kind": str(e.get("from_type" if outward else "to_type") or ""),
                    "src_ref": str(e.get("our_ref") or ""),
                    "rel_type": str(e.get("rel_type") or ""),
                    "dst_kind": str(e.get("to_type" if outward else "from_type") or ""),
                    "dst_ref": other,
                    "direction": "out" if outward else "in",
                    "observed_at": upstream_dt(e.get("created_at")),
                }
            )
        out.sort(key=lambda x: (x["rel_type"], x["dst_ref"], x["direction"]))
        return out

    async def duplicates_of(
        self, org: str, ns: str, *, number: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Issues the graph marks as duplicates of, duplicated by, or related
        to `number` (either direction), with their state — the "already
        reported" answer a triager wants first."""
        repos = self.repos()
        if not repos or int(number) <= 0:
            return []
        refs = [f"{repo}#{int(number)}" for repo in repos]
        priv = self._privacy()
        assert self.cfg is not None
        ids = sorted(ids_of(refs, bool(self.cfg.is_private)))
        # `me` is derived, not looked up; `reproduces` edges do
        # not exist upstream (verified live) and cost nothing to keep
        sql = f"""
            WITH linked AS (
                SELECT rel_type, if(from_id IN {{ids:Array(UUID)}}, 'out', 'in') AS direction,
                       if(from_id IN {{ids:Array(UUID)}}, to_id, from_id) AS other_id
                FROM {self.db}.edges
                WHERE rel_type IN ('duplicate_of', 'related_to', 'reproduces')
                  AND (from_id IN {{ids:Array(UUID)}} OR to_id IN {{ids:Array(UUID)}})
            )
            SELECT l.rel_type AS rel_type, l.direction AS direction, i.source_id AS source_id,
                   i.url AS url, i.title AS title, i.severity_level AS severity_level,
                   i.primary_component AS primary_component, i.created_at AS created_at,
                   i.closed_at AS closed_at, i.state_reason AS state_reason
            FROM linked AS l
            INNER JOIN (SELECT id, source_id, url, title, severity_level, primary_component,
                               created_at, closed_at, state_reason
                        FROM {self.db}.github_issues FINAL
                        WHERE {priv}id IN (SELECT other_id FROM linked)) AS i ON i.id = l.other_id
            ORDER BY rel_type = 'duplicate_of' DESC, created_at DESC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), 50))
        rows = await self._rows(
            "duplicates", sql, {"ids": ids, "limit": lim}, cache_key=["dups", refs, lim]
        )
        return [
            {
                **doc_row(r, "github_issue"),
                "rel_type": str(r.get("rel_type") or ""),
                "direction": str(r.get("direction") or ""),
                "state_reason": str(r.get("state_reason") or ""),
            }
            for r in rows
        ]

    async def component_risk_latest(
        self, org: str, ns: str, components: list[str]
    ) -> dict[str, dict[str, Any]]:
        """The newest nightly snapshot row per component (≤ 64). Snapshots
        are repository-agnostic aggregates (counts, ratios); any tracker namespace
        serves them."""
        comps = sorted({str(c) for c in components if c})[:64]
        if not comps:
            return {}
        sql = f"""
            SELECT component, toDate(computed_at) AS snapshot_date, computed_at, total_bugs,
                   critical_bugs, open_bugs, bugs_with_test, bugs_without_test, gap_ratio,
                   risk_score, trend_delta, core_incident_count, support_case_count,
                   reviewer_no_test_count, avg_time_to_fix_days
            FROM {self.db}.component_risk_snapshot
            WHERE component IN {{comps:Array(String)}}
            ORDER BY component, computed_at DESC
            LIMIT 1 BY component
        """
        rows = await self._rows("risk", sql, {"comps": comps}, cache_key=comps)
        return {str(r["component"]): r for r in rows}

    async def test_gap_components(
        self, org: str, ns: str, *, limit: int = 10, min_bugs: int = 20, days: int = 365
    ) -> list[dict[str, Any]]:
        """Components by untested-bug ratio from the latest nightly snapshot
        (the QA team's own gap formula), at least `min_bugs` bugs."""
        sql = f"""
            SELECT component, total_bugs, bugs_without_test, gap_ratio, risk_score, trend_delta,
                   open_bugs, critical_bugs, core_incident_count, support_case_count,
                   reviewer_no_test_count, toDate(computed_at) AS snapshot_date
            FROM {self.db}.component_risk_snapshot
            WHERE computed_at = (SELECT max(computed_at) FROM {self.db}.component_risk_snapshot)
              AND total_bugs >= {{min_bugs:UInt32}}
            ORDER BY gap_ratio DESC, total_bugs DESC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), 50))
        rows = await self._rows(
            "test_gaps",
            sql,
            {"min_bugs": max(1, int(min_bugs)), "limit": lim},
            cache_key=[min_bugs, lim],
        )
        return [
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
            for r in rows
        ]

    async def release_items(self, org: str, ns: str, version: str) -> list[dict[str, Any]]:
        """Blocker rows of one release version for the namespace's
        repositories, plus the latest health snapshot as an `item_kind =
        health` row."""
        repos = self.repos()
        if not repos:
            return []
        # The health row reads an optional table; a source
        # without it answers the blockers alone instead of failing the read
        # (an UNKNOWN_TABLE here tripped the shared breaker for every op).
        health = (
            f"""
            UNION ALL
            SELECT 'health', 'health', '', '', '', computed_at,
                   open_blockers, critical_untested, total_items, toDate(computed_at)
            FROM (
                SELECT computed_at, open_blockers, critical_untested, total_items
                FROM {self.db}.release_health_snapshot
                WHERE release_version = {{v:String}}
                ORDER BY computed_at DESC
                LIMIT 1
            )"""
            if self._has_optional("release_health_snapshot")
            else ""
        )
        sql = f"""
            SELECT 'blocker' AS item_kind,
                   concat(issue_repo, '#', toString(issue_number)) AS item_ref,
                   status, issue_state, fix_version, updated_at,
                   toUInt32(0) AS open_blockers, toUInt32(0) AS critical_untested,
                   toUInt32(0) AS total_items, toDate(updated_at) AS snapshot_date
            FROM {self.db}.release_items FINAL
            WHERE release_version = {{v:String}} AND issue_repo IN {{repos:Array(String)}}{health}
        """
        rows = await self._rows(
            "release", sql, {"v": version, "repos": repos}, cache_key=[version, repos]
        )
        return sorted(rows, key=lambda r: (str(r.get("item_kind")), str(r.get("item_ref"))))

    async def release_blockers_with_fixes(
        self, org: str, ns: str, *, version: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Open blockers of a release with the PRs that fix them (graph
        `fixes` edges) and whether those merged and in which release —
        the "blocked but already fixed" list, with assignees."""
        from server.tracker.domain.namespaces import doc_id as _doc_id

        repos = self.repos()
        if not repos:
            return []
        assert self.cfg is not None
        lim = max(1, min(int(limit), 100))
        blockers = await self._rows(
            "release_blockers",
            f"""SELECT concat(issue_repo, '#', toString(issue_number)) AS sid
                FROM {self.db}.release_items FINAL
                WHERE release_version = {{v:String}} AND issue_repo IN {{repos:Array(String)}}
                  AND status = 'Blockers' AND issue_state = 'OPEN'
                LIMIT {{limit:UInt32}}""",
            {"v": version, "repos": repos, "limit": lim},
            cache_key=[version, repos, lim],
        )
        sids = sorted({str(b.get("sid") or "") for b in blockers if b.get("sid")})
        if not sids:
            return []
        # the blockers' ids are derived: the issue rows by key
        # and their `fixes` edges read side by side; the fixing PRs by id
        issue_kind = "github_issue_private" if self.cfg.is_private else "github_issue"
        id_of = {_doc_id(issue_kind, sid): sid for sid in sids}
        issues, fixes = await asyncio.gather(
            self._rows(
                "blocker_issues",
                f"""SELECT source_id, url, title, severity_level, primary_component, assignees
                    FROM {self.db}.github_issues FINAL
                    WHERE {self._privacy()}source_id IN {{refs:Array(String)}}""",
                {"refs": sids},
                cache_key=["blocker_issues", sids],
            ),
            self._rows(
                "blocker_fixes",
                f"""SELECT to_id, from_id FROM {self.db}.edges
                    WHERE rel_type = 'fixes' AND to_id IN {{ids:Array(UUID)}} LIMIT 1000""",
                {"ids": sorted(id_of)},
                cache_key=["blocker_fixes", sorted(id_of)],
            ),
        )
        pr_ids = sorted({str(f.get("from_id") or "") for f in fixes if f.get("from_id")})
        prs = (
            await self._rows(
                "blocker_prs",
                f"""SELECT id, source_id, is_merged, minor_version FROM {self.db}.github_prs FINAL
                WHERE {self._privacy()}id IN {{ids:Array(UUID)}}""",
                {"ids": pr_ids},
                cache_key=["blocker_prs", pr_ids],
            )
            if pr_ids
            else []
        )
        pr_of = {str(p.get("id")): p for p in prs}
        merged: dict[str, list[str]] = {}
        open_: dict[str, list[str]] = {}
        releases: dict[str, list[str]] = {}
        for f in fixes:
            sid = id_of.get(str(f.get("to_id") or ""))
            pr = pr_of.get(str(f.get("from_id") or ""))
            if not sid or not pr:
                continue
            ref = str(pr.get("source_id") or "")
            if int(pr.get("is_merged") or 0) == 1:
                merged.setdefault(sid, []).append(ref)
                if pr.get("minor_version"):
                    releases.setdefault(sid, []).append(str(pr["minor_version"]))
            else:
                open_.setdefault(sid, []).append(ref)
        out = [
            {
                "source_id": str(r.get("source_id") or ""),
                "url": str(r.get("url") or ""),
                "title": str(r.get("title") or "")[:140],
                "severity": str(r.get("severity_level") or ""),
                "component": str(r.get("primary_component") or ""),
                "assignees": str_list(r.get("assignees")),
                "merged_fixes": sorted(set(merged.get(str(r.get("source_id") or ""), []))),
                "open_fixes": sorted(set(open_.get(str(r.get("source_id") or ""), []))),
                "fix_releases": sorted(set(releases.get(str(r.get("source_id") or ""), []))),
            }
            for r in issues
        ]
        out.sort(
            key=lambda x: (
                -len(x["merged_fixes"]),
                _severity_index(str(x["severity"])),
                x["source_id"],
            )
        )
        return out[:lim]

    async def open_issues_by_component(
        self,
        org: str,
        ns: str,
        component: str,
        *,
        limit: int = 10,
        severities: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Open issues of one component, most severe first, then newest.
        `severities` keeps the cell the board counted —
        `severity_level IN (...)`, the stored values being lower-case
        (`critical | medium | low`, verified live), so the
        names are normalised here and the column stays index-served — and
        lifts the ceiling from 50 to `CELL_LIMIT`, so a cell lists every
        issue it counts (the largest live cell is 236)."""
        repos = self.repos()
        if not repos:
            return []
        sevs = normalise_severities(severities)
        sev_pred = "AND severity_level IN {sev:Array(String)}" if sevs else ""
        sql = f"""
            SELECT {ISSUE_COLS}
            FROM {self.db}.github_issues FINAL
            WHERE {self._privacy()}primary_component = {{component:String}}
              AND closed_at <= toDateTime64(0, 3)
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
              {sev_pred}
            ORDER BY multiIf(severity_level = 'critical', 0, severity_level = 'major', 1,
                             severity_level = 'medium', 2, 3) ASC,
                     created_at DESC
            LIMIT {{limit:UInt32}}
        """
        lim = max(1, min(int(limit), CELL_LIMIT if sevs else 50))
        params: dict[str, Any] = {"component": component, "repos": repos, "limit": lim}
        if sevs:
            params["sev"] = sevs
        rows = await self._rows(
            "open_issues",
            sql,
            params,
            cache_key=[component, repos, lim, sevs],
        )
        return [issue_row(r) for r in rows]

    async def open_issue_counts_by_severity(
        self,
        org: str,
        ns: str,
        *,
        components: list[str] | None = None,
        days: int = 0,
    ) -> list[dict[str, Any]]:
        """The bug board: open issues of the namespace's
        repositories counted per `(primary_component, severity_level)` cell,
        every component or the ones named (≤ 64), opened in the last `days`
        days (0 = every open issue). One aggregating statement over
        `github_issues FINAL`, ≤ 2000 cells, components alphabetical and
        the severities most severe first. `untested` counts the cells' issues
        with no linked test (`has_test` is Nullable upstream and NULL on
        most open issues — no test is known, so none is claimed); `flaky`
        the flaky-test issues; `bisected` the ones the triage bot bisected."""
        repos = self.repos()
        if not repos:
            return []
        comps = sorted({str(c) for c in (components or []) if c})[:64]
        window = int(days or 0)
        conds = [
            f"{self._privacy()}closed_at <= toDateTime64(0, 3)",
            "splitByChar('#', source_id)[1] IN {repos:Array(String)}",
        ]
        params: dict[str, Any] = {"repos": repos}
        if comps:
            conds.append("primary_component IN {comps:Array(String)}")
            params["comps"] = comps
        if window > 0:
            conds.append("created_at >= now64(3) - INTERVAL {days:UInt32} DAY")
            params["days"] = self._window(window)
        # `severity_level` is the contract's name; upstream has no level
        # column, so the contract aliases the name to `severity`
        # (the label class: '', bug, crash, memory, major),
        # so the alias is given in SELECT only
        sql = f"""
            SELECT primary_component AS component, severity_level AS severity,
                   count() AS open,
                   countIf(coalesce(has_test, 0) = 0) AS untested,
                   countIf(is_flaky_test = 1) AS flaky,
                   countIf(triage_bisect_outcome != '') AS bisected
            FROM {self.db}.github_issues FINAL
            WHERE {" AND ".join(conds)}
            GROUP BY primary_component, severity_level
            HAVING count() > 0
            ORDER BY primary_component ASC, {_SEVERITY_RANK} ASC
            LIMIT 2000
        """
        rows = await self._rows(
            "severity_board", sql, params, cache_key=[comps, params.get("days", 0)]
        )
        return [
            {
                "component": str(r.get("component") or ""),
                "severity": str(r.get("severity") or "") or "unrated",
                "open": int(r.get("open") or 0),
                "untested": int(r.get("untested") or 0),
                "flaky": int(r.get("flaky") or 0),
                "bisected": int(r.get("bisected") or 0),
            }
            for r in rows
        ]

    async def flaky_issues_matching(
        self, org: str, ns: str, test_name: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Flaky-test issues whose title carries every token of `test_name`
        (the upstream title text index), newest first."""
        repos = self.repos()
        toks = text_tokens(test_name)
        if not repos or not toks:
            return []
        lim = max(1, min(int(limit), 50))
        params: dict[str, Any] = {"repos": repos, "limit": lim}
        sql = f"""
            SELECT {ISSUE_COLS}
            FROM {self.db}.github_issues FINAL
            WHERE {self._privacy()}is_flaky_test = 1
              AND {all_tokens("title", toks, params)}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            ORDER BY created_at DESC
            LIMIT {{limit:UInt32}}
        """
        rows = await self._rows(
            "flaky", sql, params, cache_key=[toks, repos, lim], budget_ms=self._text_budget()
        )
        return [issue_row(r) for r in rows]

    async def open_issues_naming(
        self, org: str, ns: str, tokens: list[str], *, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Open issues whose text carries any of `tokens` (function names the
        upstream body index tokenises), most severe first, then newest — "a
        bug is already reported in the function you are changing"."""
        repos = self.repos()
        toks = sorted({t for t in tokens if t})
        if not repos or not toks:
            return []
        lim = max(1, min(int(limit), 20))
        params: dict[str, Any] = {"repos": repos, "limit": lim}
        sql = f"""
            SELECT {ISSUE_COLS}
            FROM {self.db}.github_issues FINAL
            WHERE {self._privacy()}closed_at <= toDateTime64(0, 3)
              AND {any_tokens("body", toks, params)}
              AND splitByChar('#', source_id)[1] IN {{repos:Array(String)}}
            ORDER BY multiIf(severity_level = 'critical', 0, severity_level = 'major', 1,
                             severity_level = 'medium', 2, 3) ASC,
                     created_at DESC
            LIMIT {{limit:UInt32}}
        """
        rows = await self._rows(
            "issues_naming",
            sql,
            params,
            cache_key=[toks, repos, lim],
            budget_ms=self._text_budget(),
        )
        return [issue_row(r) for r in rows]

    async def similar_bugs(
        self,
        org: str,
        ns: str,
        *,
        text: str,
        component: str = "",
        version: str = "",
        days: int = 730,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Bugs whose title or body carries every token of `text`, across the
        sources the namespace may see: issues on a standard namespace; issues,
        incidents and escalations on a restricted one. Component and version
        narrow it; newest first, then most reacted."""
        toks = text_tokens(text)
        if len(" ".join(toks)) < 4:
            return []
        params: dict[str, Any] = {
            "days": self._window(days),
            "limit": max(1, min(int(limit), 50)),
        }
        conds = [text_match(toks, params), "created_at >= now64(3) - INTERVAL {days:UInt32} DAY"]
        if component:
            conds.append("primary_component = {component:String}")
            params["component"] = component
        if version:
            conds.append(
                "(has(affected_versions, {version:String}) OR position(body, {version:String}) > 0)"
            )
            params["version"] = version
        assert self.cfg is not None
        sources = (
            ("github_issue", "github_issue_private", "core_incident", "support_case")
            if (self.cfg.restricted)
            else ("github_issue",)
        )
        params["sources"] = list(sources)
        sql = f"""
            SELECT source_id, url, title, source, severity_level, primary_component, labels,
                   affected_versions, has_test, reactions_total, created_at, closed_at,
                   error_type, top_function
            FROM {self.db}.qa_bugs
            WHERE source IN {{sources:Array(String)}} AND {" AND ".join(conds)}
            ORDER BY {all_tokens("title", toks, params)} DESC, created_at DESC, reactions_total DESC
            LIMIT {{limit:UInt32}}
        """
        # title hits first: body-only matches were the noise in the live read
        rows = await self._rows(
            "similar_bugs", sql, params, cache_key=params, budget_ms=self._text_budget()
        )
        if rows or len(toks) < 2:
            return [doc_row(r, str(r.get("source") or "")) for r in rows]
        # nothing carries every token: the rows carrying the most of them,
        # marked partial, so a triager sees the nearest reports rather than none
        score = " + ".join(
            f"(hasAnyTokens(title, {{t{i}:Array(String)}})"
            f" OR hasAnyTokens(body, {{t{i}:Array(String)}}))"
            for i in range(len(toks))
        )
        conds[0] = f"({any_tokens('title', toks, params)} OR {any_tokens('body', toks, params)})"
        sql = f"""
            SELECT source_id, url, title, source, severity_level, primary_component, labels,
                   affected_versions, has_test, reactions_total, created_at, closed_at,
                   error_type, top_function, {score} AS matched
            FROM {self.db}.qa_bugs
            WHERE source IN {{sources:Array(String)}} AND {" AND ".join(conds)}
              AND {score} >= {{need:UInt32}}
            ORDER BY matched DESC, created_at DESC, reactions_total DESC
            LIMIT {{limit:UInt32}}
        """
        params["need"] = max(1, len(toks) - 1)
        rows = await self._rows(
            "similar_bugs_partial",
            sql,
            params,
            cache_key=["partial", params],
            budget_ms=self._text_budget(),
        )
        return [
            {
                **doc_row(r, str(r.get("source") or "")),
                "partial": True,
                "matched_tokens": int(r.get("matched") or 0),
                "of_tokens": len(toks),
            }
            for r in rows
        ]
