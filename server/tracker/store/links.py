"""loom.qa_link_candidates — the issue–PR link candidates the daily pass
writes and `tracker.links` / `code.backport_gap` / `code.bisect_candidates`
read. Keyed (org, tracker_namespace, issue_number, pr_number),
ReplacingMergeTree by version: a pass re-emits every pair of an issue's
list; the readers keep only the issue's latest `computed_at`
(`server/tracker/domain/links.py`, `latest_only`) so a pair that fell out of the list is
not served beside the new one. Plain WHERE; FINAL because the keys are
re-inserted every pass.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, TypedDict, cast

from server.core.ch import CHClient, get_client
from server.tracker.domain.links import latest_only

TABLE = "loom.qa_link_candidates"

TRACKER_LINK_COLS: dict[str, Any] = {
    "org": "",
    "qa_namespace": "",  # the stored column name
    "issue_number": 0,
    "pr_number": 0,
    "rank": 0,
    "score": 0.0,
    "bm25_title": 0.0,
    "bm25_body": 0.0,
    "bm25_norm": 0.0,
    "window_hours": 0,
    "component_match": 0,
    "frames_symbols": 0,
    "frames_files": 0,
    "frames_total": 0,
    "closes_mention": 0,
    "terms": [],
    "pr_title": "",
    "pr_url": "",
    "pr_merged_at": None,
    "pr_minor_version": "",
    "issue_title": "",
    "issue_created_at": None,
    "computed_at": None,
    "version": 0,
}

_EPOCH = datetime(1970, 1, 1)
_COLS = ", ".join(TRACKER_LINK_COLS)


class LinkCandidate(TypedDict):
    """One stored (issue, PR) pair of a pass's list: every persisted column."""

    org: str
    qa_namespace: str
    issue_number: int
    pr_number: int
    rank: int
    score: float
    bm25_title: float
    bm25_body: float
    bm25_norm: float
    window_hours: int
    component_match: int
    frames_symbols: int
    frames_files: int
    frames_total: int
    closes_mention: int
    terms: list[str]
    pr_title: str
    pr_url: str
    pr_merged_at: datetime
    pr_minor_version: str
    issue_title: str
    issue_created_at: datetime
    computed_at: datetime
    version: int


class LinkStats(TypedDict):
    """How much of the table a namespace holds and when it was last written."""

    issues: int
    pairs: int
    last_computed: datetime | None


def build_link_row(**overrides: Any) -> dict[str, Any]:
    """A full row with every persisted column present; an unknown key is a
    loud failure (builder / schema drift)."""
    unknown = set(overrides) - set(TRACKER_LINK_COLS)
    if unknown:
        raise ValueError(f"qa_link_candidates: unknown columns {sorted(unknown)}")
    row = {k: (list(v) if isinstance(v, list) else v) for k, v in TRACKER_LINK_COLS.items()}
    row.update(overrides)
    now = datetime.now(UTC).replace(tzinfo=None)
    for k in ("pr_merged_at", "issue_created_at"):
        if row.get(k) is None:
            row[k] = _EPOCH
    if row.get("computed_at") is None:
        row["computed_at"] = now
    if not row.get("version"):
        row["version"] = time.time_ns()
    return row


class TrackerLinksRepo:
    def __init__(self, ch: CHClient | None = None) -> None:
        self._ch = ch or get_client()

    async def insert_candidates(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        for r in rows:
            missing = set(TRACKER_LINK_COLS) - set(r)
            if missing:
                raise ValueError(
                    f"qa_link_candidates: row missing {sorted(missing)} — use build_link_row()"
                )
        await self._ch.insert(TABLE, rows)

    async def candidates_for_issue(
        self, org: str, tracker_namespace: str, issue_number: int, *, limit: int = 20
    ) -> list[LinkCandidate]:
        """Point read on the key: one issue's list, best rank first, the
        latest pass only (a wider read than `limit`, so the filter sees the
        whole latest list before the cut)."""
        rows = await self._ch.query_all(
            f"""
            SELECT {_COLS}
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND qa_namespace = {{ns:String}}
              AND issue_number = {{n:UInt32}}
            ORDER BY computed_at DESC, rank, pr_number
            LIMIT {{lim:UInt32}}
            """,
            {
                "org": org,
                "ns": tracker_namespace,
                "n": int(issue_number),
                "lim": max(1, min(int(limit), 50)) * 4,
            },
        )

        kept = sorted(
            latest_only(rows), key=lambda r: (int(r.get("rank") or 0), int(r.get("pr_number") or 0))
        )
        return cast(list[LinkCandidate], kept[: max(1, min(int(limit), 50))])

    async def candidates_for_pr(
        self, org: str, tracker_namespace: str, pr_number: int, *, limit: int = 20
    ) -> list[LinkCandidate]:
        """The issues whose lists carry this PR: a scan of the namespace's
        rows (~100 bytes a pair) on the last key column; best score first
        after the per-issue latest-pass filter."""
        rows = await self._ch.query_all(
            f"""
            SELECT {_COLS}
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND qa_namespace = {{ns:String}}
              AND pr_number = {{n:UInt32}}
            ORDER BY score DESC, issue_number DESC
            LIMIT {{lim:UInt32}}
            """,
            {
                "org": org,
                "ns": tracker_namespace,
                "n": int(pr_number),
                "lim": max(1, min(int(limit), 50)) * 4,
            },
        )

        # a stale pair's issue may have a newer list without this PR: read the
        # issues' latest stamps, then keep the pairs that belong to them
        stamps = await self.latest_computed(
            org, tracker_namespace, [int(r["issue_number"]) for r in rows]
        )
        kept = [
            r
            for r in latest_only(rows)
            if r.get("computed_at")
            == stamps.get(int(r.get("issue_number") or 0), r.get("computed_at"))
        ]
        kept.sort(key=lambda r: (-float(r.get("score") or 0.0), -int(r.get("issue_number") or 0)))
        return cast(list[LinkCandidate], kept[: max(1, min(int(limit), 50))])

    async def latest_computed(
        self, org: str, tracker_namespace: str, issue_numbers: list[int]
    ) -> dict[int, datetime]:
        nums = sorted({int(n) for n in issue_numbers if int(n) > 0})[:256]
        if not nums:
            return {}
        rows = await self._ch.query_all(
            f"""
            SELECT issue_number, max(computed_at) AS latest
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND qa_namespace = {{ns:String}}
              AND issue_number IN {{nums:Array(UInt32)}}
            GROUP BY issue_number
            """,
            {"org": org, "ns": tracker_namespace, "nums": nums},
        )
        return {
            int(r["issue_number"]): r["latest"]
            for r in rows
            if isinstance(r.get("latest"), datetime)
        }

    async def namespace_stats(self, org: str, tracker_namespace: str) -> LinkStats:
        """How much of the table a namespace holds and when it was last
        written — the pass outcome and the `computed_at` a reader shows
        when it finds nothing for a number."""
        row = await self._ch.query_one(
            f"""
            SELECT uniqExact(issue_number) AS issues, count() AS pairs,
                   max(computed_at) AS last_computed
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND qa_namespace = {{ns:String}}
            """,
            {"org": org, "ns": tracker_namespace},
        )
        row = row or {}
        last = row.get("last_computed")
        return LinkStats(
            issues=int(row.get("issues") or 0),
            pairs=int(row.get("pairs") or 0),
            last_computed=last if isinstance(last, datetime) else None,
        )


__all__ = [
    "TRACKER_LINK_COLS",
    "TABLE",
    "LinkCandidate",
    "LinkStats",
    "TrackerLinksRepo",
    "build_link_row",
]
