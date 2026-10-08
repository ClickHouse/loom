"""The procedure plane is a monitored signal.

Two numbers say whether the procedure plane works: how often a run reused a
stored procedure, and whether a success reproduces (success retention,
P(ok_t+1 | ok_t)). A dark plane — packs flowing, none showing a procedure, no
ledger rows — is invisible without them. `ops.health` carries a
`procedure_plane` check: packs, packs that showed a procedure, packs that
withheld one (the exploration arm), ledger rows and the procedures they touched, the
current quarantines, and success retention over the window. It warns when
packs flow but no procedure is ever shown, and when retention over enough
pairs falls under the bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["ok", "warn", "fail"]


@dataclass
class ProcedurePlane:
    window_days: int
    packs: int = 0
    packs_showing: int = 0
    packs_withholding: int = 0
    ledger_rows: int = 0
    ledger_ok: int = 0
    procedures_run: int = 0
    retention_pairs: int = 0
    retention_ok_after_ok: int = 0
    quarantined: int = 0
    active_procedures: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def show_rate(self) -> float | None:
        return self.packs_showing / self.packs if self.packs else None

    @property
    def success_rate(self) -> float | None:
        return self.ledger_ok / self.ledger_rows if self.ledger_rows else None

    @property
    def retention(self) -> float | None:
        if not self.retention_pairs:
            return None
        return self.retention_ok_after_ok / self.retention_pairs

    def as_data(self) -> dict[str, Any]:
        rate = self.show_rate
        ret = self.retention
        succ = self.success_rate
        return {
            "window_days": self.window_days,
            "packs": self.packs,
            "packs_showing": self.packs_showing,
            "packs_withholding": self.packs_withholding,
            "show_rate": round(rate, 4) if rate is not None else None,
            "ledger_rows": self.ledger_rows,
            "ledger_ok": self.ledger_ok,
            "success_rate": round(succ, 4) if succ is not None else None,
            "procedures_run": self.procedures_run,
            "retention_pairs": self.retention_pairs,
            "retention": round(ret, 4) if ret is not None else None,
            "quarantined": self.quarantined,
            "active_procedures": self.active_procedures,
        }


def fold(row: dict[str, Any] | None, *, window_days: int) -> ProcedurePlane:
    """Fold the one aggregate row into a ProcedurePlane. Pure, for tests."""
    p = ProcedurePlane(window_days=window_days)
    if not row:
        return p
    for k in (
        "packs",
        "packs_showing",
        "packs_withholding",
        "ledger_rows",
        "ledger_ok",
        "procedures_run",
        "retention_pairs",
        "retention_ok_after_ok",
        "quarantined",
        "active_procedures",
    ):
        setattr(p, k, int(row.get(k) or 0))
    return p


async def procedure_plane(
    ch: Any,
    *,
    org: str,
    namespace: str | None = None,
    window_days: int = 7,
) -> ProcedurePlane:
    """One read over agent_events, procedure_runs and the procedural rows."""
    params: dict[str, Any] = {"org": org, "days": int(window_days), "ns": namespace or "*"}
    row = await ch.query_one(
        """
        WITH pk AS (
            SELECT
                count() AS packs,
                countIf(args['out_procedure_keys'] != '') AS packs_showing,
                countIf(args['out_procedure_keys_withheld'] != '') AS packs_withholding
            FROM loom.agent_events
            WHERE org = {org:String} AND ({ns:String} = '*' OR namespace = {ns:String})
              AND ts > now() - INTERVAL {days:UInt16} DAY
              AND operation = 'context.pack' AND success
        ),
        runs AS (
            SELECT
                count() AS ledger_rows, countIf(run_ok) AS ledger_ok,
                uniq(memory_key) AS procedures_run,
                countIf(prev_ok = 1) AS retention_pairs,
                countIf(prev_ok = 1 AND run_ok) AS retention_ok_after_ok
            FROM (
                SELECT memory_key, ok AS run_ok,
                       lagInFrame(toUInt8(ok)) OVER
                           (PARTITION BY namespace, memory_key ORDER BY ts) AS prev_ok,
                       row_number() OVER
                           (PARTITION BY namespace, memory_key ORDER BY ts) AS rn
                FROM loom.procedure_runs
                WHERE org = {org:String} AND ({ns:String} = '*' OR namespace = {ns:String})
                  AND ts > now() - INTERVAL {days:UInt16} DAY
            )
        ),
        pr AS (
            SELECT
                count() AS active_procedures,
                countIf(JSONHas(value, 'quarantined')) AS quarantined
            FROM loom.memory FINAL
            WHERE org = {org:String} AND ({ns:String} = '*' OR namespace = {ns:String})
              AND memory_type = 'procedural' AND status = 'active' AND tombstoned_at IS NULL
        )
        SELECT pk.packs, pk.packs_showing, pk.packs_withholding,
               runs.ledger_rows, runs.ledger_ok, runs.procedures_run,
               runs.retention_pairs, runs.retention_ok_after_ok,
               pr.active_procedures, pr.quarantined
        FROM pk, runs, pr
        """,
        params,
    )
    return fold(row, window_days=window_days)


def assess(
    plane: ProcedurePlane,
    *,
    min_packs: int,
    retention_warn: float,
    min_pairs: int,
) -> tuple[Status, str, str]:
    """(status, detail, repair). Warn when packs flow but never show a
    procedure while procedures exist, and when success retention over
    enough pairs falls under the bar. Quiet below the traffic floors."""
    problems: list[str] = []
    repairs: list[str] = []
    if plane.packs >= min_packs and plane.packs_showing == 0 and plane.active_procedures > 0:
        problems.append(
            f"{plane.packs} packs in {plane.window_days} d showed no procedure "
            f"({plane.active_procedures} active)"
        )
        repairs.append(
            "the pack matches by situation: callers pass `situation` (and `query` as the goal); "
            "check procedure.match returns rows for the namespace's real situations"
        )
    ret = plane.retention
    if ret is not None and plane.retention_pairs >= min_pairs and ret < retention_warn:
        problems.append(
            f"success retention {ret:.0%} over {plane.retention_pairs} pairs "
            f"(bar {retention_warn:.0%})"
        )
        repairs.append(
            "successes are not reproducing: read procedure.evolve fail_prone_steps and "
            "failure notes; consider procedure.revise"
        )
    if problems:
        return ("warn", "; ".join(problems), "; ".join(repairs))
    if plane.packs < min_packs and plane.ledger_rows == 0:
        return (
            "ok",
            f"{plane.packs} packs, 0 ledger rows in {plane.window_days} d "
            f"(below {min_packs} packs; shape not judged)",
            "",
        )
    rate = plane.show_rate
    parts = [
        f"{plane.packs} packs, {plane.packs_showing} showed a procedure"
        + (f" ({rate:.0%})" if rate is not None else ""),
        f"{plane.ledger_rows} ledger rows over {plane.procedures_run} procedures",
    ]
    if ret is not None:
        parts.append(f"retention {ret:.0%} over {plane.retention_pairs} pairs")
    if plane.quarantined:
        parts.append(f"{plane.quarantined} quarantined")
    return ("ok", "; ".join(parts), "")
