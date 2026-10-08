"""loom.code_backport_gaps — one row per (fix, release branch) the nightly
`code_backport_gaps` pass computed, read by `code.backport_gaps`
as the set-wide report: which labelled fixes a branch
still owes. Keyed (org, code_namespace, branch_namespace, pr_number),
ReplacingMergeTree by version: a pass re-emits every row of a fix and FINAL
serves the newest; a fix that left the 90-day window keeps its last row and
the readers filter on `pr_merged_at`. Plain WHERE; FINAL because the keys are
re-inserted every pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict, cast

from server.core.ch import CHClient, get_client
from server.core.datetimes import naive_utc_now

TABLE = "loom.code_backport_gaps"

GAP_COLS: dict[str, Any] = {
    "org": "",
    "code_namespace": "",
    "branch_namespace": "",
    "pr_number": 0,
    "branch": "",
    "verdict": "unknown",
    "epistemic": "",
    "basis": "",
    "reason": "",
    "cherry_pick_pr": 0,
    "cherry_pick_sha": "",
    "backport_pr": 0,
    "label_wanted": 0,
    "wanted_branch": 0,
    "wanted_source": "",
    "settled": 0,
    "pr_title": "",
    "pr_merged_at": None,
    "pr_minor_version": "",
    "computed_at": None,
    "version": 0,
}


class GapRow(TypedDict):
    """One loom.code_backport_gaps row: a fix's verdict on one release branch."""

    org: str
    code_namespace: str
    branch_namespace: str
    pr_number: int
    branch: str
    verdict: str
    epistemic: str
    basis: str
    reason: str
    cherry_pick_pr: int
    cherry_pick_sha: str
    backport_pr: int
    label_wanted: int
    wanted_branch: int
    wanted_source: str
    settled: int
    pr_title: str
    pr_merged_at: datetime
    pr_minor_version: str
    computed_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class FixStamp:
    """A fix's rows at a glance: when it was last computed, whether every
    branch row is settled, and how many branch rows it has."""

    latest: datetime | None
    settled: bool
    rows: int


@dataclass(frozen=True, slots=True)
class GapStats:
    """What the table holds for a trunk namespace and when it was written."""

    fixes: int
    rows: int
    last_computed: datetime | None
    first_computed: datetime | None


_EPOCH = datetime(1970, 1, 1)
_COLS = ", ".join(GAP_COLS)
MAX_ROWS = 5000


def build_gap_row(**overrides: Any) -> GapRow:
    """A full row with every persisted column present; an unknown key is a
    loud failure (builder / schema drift)."""
    unknown = set(overrides) - set(GAP_COLS)
    if unknown:
        raise ValueError(f"code_backport_gaps: unknown columns {sorted(unknown)}")
    row = dict(GAP_COLS)
    row.update(overrides)
    now = naive_utc_now()
    if row.get("pr_merged_at") is None:
        row["pr_merged_at"] = _EPOCH
    if row.get("computed_at") is None:
        row["computed_at"] = now
    if not row.get("version"):
        row["version"] = int(now.timestamp() * 1e9)
    return cast(GapRow, row)


class CodeBackportGapsRepo:
    def __init__(self, ch: CHClient | None = None) -> None:
        self._ch = ch or get_client()

    async def insert_rows(self, rows: list[GapRow]) -> None:
        if not rows:
            return
        for r in rows:
            missing = set(GAP_COLS) - set(r)
            if missing:
                raise ValueError(
                    f"code_backport_gaps: row missing {sorted(missing)} — use build_gap_row()"
                )
        await self._ch.insert(TABLE, cast(list[dict[str, Any]], rows))

    async def rows_for_branch(
        self,
        org: str,
        code_ns: str,
        branch_ns: str,
        *,
        since: datetime,
        wanted_only: bool = True,
        limit: int = MAX_ROWS,
    ) -> list[GapRow]:
        """One branch's rows for the fixes merged since `since` — a range read
        on the key prefix; newest fix first. `wanted_only` keeps the fixes
        the maintainers labelled for backport (the report's default: the
        others are candidates)."""
        wanted = "AND label_wanted = 1" if wanted_only else ""
        rows = await self._ch.query_all(
            f"""
            SELECT {_COLS}
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND code_namespace = {{ns:String}}
              AND branch_namespace = {{branch:String}}
              AND pr_merged_at >= {{since:DateTime}} {wanted}
            ORDER BY pr_number DESC
            LIMIT {{lim:UInt32}}
            """,
            {
                "org": org,
                "ns": code_ns,
                "branch": branch_ns,
                "since": since,
                "lim": max(1, min(int(limit), MAX_ROWS)),
            },
        )
        return cast(list[GapRow], rows)

    async def rows_for_namespace(
        self,
        org: str,
        code_ns: str,
        *,
        since: datetime,
        wanted_only: bool = True,
        limit: int = MAX_ROWS,
        branches: list[str] | None = None,
    ) -> list[GapRow]:
        """The rows for the fixes merged since `since` — the (org, code_namespace)
        prefix; newest fix first. `branches` (branch namespaces) narrows it before the
        LIMIT, so rows of other branches do not use up the page; None = every branch."""
        wanted = "AND label_wanted = 1" if wanted_only else ""
        params: dict[str, Any] = {
            "org": org,
            "ns": code_ns,
            "since": since,
            "lim": max(1, min(int(limit), MAX_ROWS)),
        }
        only = ""
        if branches is not None:
            only = "AND branch_namespace IN {branches:Array(String)}"
            params["branches"] = list(branches)
        rows = await self._ch.query_all(
            f"""
            SELECT {_COLS}
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND code_namespace = {{ns:String}}
              AND pr_merged_at >= {{since:DateTime}} {wanted} {only}
            ORDER BY pr_number DESC, branch_namespace
            LIMIT {{lim:UInt32}}
            """,
            params,
        )
        return cast(list[GapRow], rows)

    async def fix_stamps(self, org: str, code_ns: str) -> dict[int, FixStamp]:
        """pr_number → its `FixStamp`: when each fix was last computed
        and whether every branch row is settled — the pass's order (never
        computed first, then stalest) and its skip list, one statement over
        the namespace prefix."""
        rows = await self._ch.query_all(
            f"""
            SELECT pr_number, max(computed_at) AS latest, min(settled) AS all_settled,
                   count() AS branch_rows
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND code_namespace = {{ns:String}}
            GROUP BY pr_number
            """,
            {"org": org, "ns": code_ns},
        )
        out: dict[int, FixStamp] = {}
        for r in rows:
            latest = r.get("latest")
            out[int(r["pr_number"])] = FixStamp(
                latest=latest if isinstance(latest, datetime) else None,
                settled=bool(int(r.get("all_settled") or 0)),
                rows=int(r.get("branch_rows") or 0),
            )
        return out

    async def namespace_stats(self, org: str, code_ns: str) -> GapStats:
        """How many fixes and rows the table holds for a trunk namespace and
        when it was last written — the report's `pass` block."""
        row = await self._ch.query_one(
            f"""
            SELECT uniqExact(pr_number) AS fixes, count() AS branch_rows,
                   max(computed_at) AS last_computed, min(computed_at) AS first_computed
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND code_namespace = {{ns:String}}
            """,
            {"org": org, "ns": code_ns},
        )
        row = row or {}
        last, first = row.get("last_computed"), row.get("first_computed")
        return GapStats(
            fixes=int(row.get("fixes") or 0),
            rows=int(row.get("branch_rows") or 0),
            last_computed=last if isinstance(last, datetime) else None,
            first_computed=first if isinstance(first, datetime) else None,
        )


__all__ = [
    "GAP_COLS",
    "MAX_ROWS",
    "TABLE",
    "CodeBackportGapsRepo",
    "FixStamp",
    "GapRow",
    "GapStats",
    "build_gap_row",
]
