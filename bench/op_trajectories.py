#!/usr/bin/env python3
"""Operation trajectories: how an agent's create / read / update / delete mix
moves across a task.

A trajectory plot of an agent's operation mix against task success needs one
row per task: the ordered operations, their verb classes, and
the task's outcome. loom records the operations (loom.agent_events), the runs
(loom.agent_runs) and what happened to the memories a run wrote
(loom.memory_usage_events: used / evidence / harm), and the operation row
names its run — so a training dataset of operation
trajectories can be exported from real traffic, with per-entry credit.

    python bench/op_trajectories.py --org <org> --days 7 --out /tmp/traj.json
    python bench/op_trajectories.py --org <org> --namespace <namespace> --by session

Grouping: `--by run` uses agent_events.run_id (rows with none are dropped); `--by
session` groups every memory-plane call by (agent, session_id); `--by gap` splits one
caller's calls into bursts at pauses over `--gap-seconds` (for callers that send no
identity, e.g. `-` on every call); `--by auto` (default) takes runs when any exist,
sessions when most rows carry one, else gap bursts. Outcome joins only exist for runs;
session trajectories report `unknown`.

The folds are pure (fold_trajectories / decile_profile / summarize) and unit
tested; only `load_*` touch ClickHouse.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.insights.app.op_mix import CLASSES, classify

MEMORY_PLANE_PREFIXES = ("memory.", "working_memory.", "context.", "facts.")

# One letter per class for the compact sequence string ("R R C U R D").
LETTER: dict[str, str] = {
    "create": "C",
    "read": "R",
    "update": "U",
    "annotate": "A",
    "delete": "D",
    "scratchpad": "S",
    "maintain": "M",
    "other": "?",
}


@dataclass
class Step:
    ts: datetime
    operation: str
    success: bool
    latency_ms: int
    top_k: int | None = None
    memory_key: str = ""

    @property
    def verb(self) -> str:
        return classify(self.operation)


@dataclass
class Trajectory:
    key: str  # run_id, or "<agent>/<session_id>" for sessions
    grouping: str  # "run" | "session"
    agent: str
    steps: list[Step] = field(default_factory=list)
    # Joined from agent_runs when grouping == "run".
    outcome: str = "unknown"  # completed | failed | cancelled | active | unknown
    memories_used: int = 0
    memories_written: int = 0
    credit: dict[str, int] = field(default_factory=dict)  # used / evidence / harm, keys written
    credit_used: dict[str, int] = field(default_factory=dict)  # same, keys read

    @property
    def counts(self) -> dict[str, int]:
        c = dict.fromkeys(CLASSES, 0)
        for s in self.steps:
            c[s.verb] += 1
        return c

    @property
    def writes(self) -> int:
        c = self.counts
        return c["create"] + c["update"]

    @property
    def reads_per_write(self) -> float | None:
        return self.counts["read"] / self.writes if self.writes else None

    @property
    def update_share(self) -> float | None:
        return self.counts["update"] / self.writes if self.writes else None

    @property
    def sequence(self) -> str:
        return " ".join(LETTER[s.verb] for s in self.steps)

    @property
    def duration_s(self) -> float:
        if len(self.steps) < 2:
            return 0.0
        return (self.steps[-1].ts - self.steps[0].ts).total_seconds()

    @property
    def reads_before_first_write(self) -> int:
        n = 0
        for s in self.steps:
            if s.verb in ("create", "update"):
                break
            if s.verb == "read":
                n += 1
        return n

    @property
    def modal_top_k(self) -> int | None:
        ks = [s.top_k for s in self.steps if s.top_k]
        return Counter(ks).most_common(1)[0][0] if ks else None

    def row(self) -> dict[str, Any]:
        rpw = self.reads_per_write
        share = self.update_share
        return {
            "key": self.key,
            "grouping": self.grouping,
            "agent": self.agent,
            "n_ops": len(self.steps),
            "sequence": self.sequence,
            "counts": {k: v for k, v in self.counts.items() if v},
            "reads_per_write": round(rpw, 2) if rpw is not None else None,
            "update_share": round(share, 3) if share is not None else None,
            "reads_before_first_write": self.reads_before_first_write,
            "modal_top_k": self.modal_top_k,
            "duration_s": round(self.duration_s, 1),
            "errors": sum(1 for s in self.steps if not s.success),
            "outcome": self.outcome,
            "memories_used": self.memories_used,
            "memories_written": self.memories_written,
            "credit": dict(self.credit),
            "credit_used": dict(self.credit_used),
        }


def _to_int(v: Any) -> int | None:
    try:
        n = int(str(v))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


DEFAULT_GAP_SECONDS = 60
SENTINELS = ("", "-")


def fold_trajectories(
    rows: list[dict[str, Any]],
    *,
    by: str,
    min_ops: int = 1,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> list[Trajectory]:
    """Group agent_events rows (ordered by ts) into trajectories.

    `by` is "run" (agent_events.run_id; run-less rows dropped), "session"
    ((agent, session_id); rows with the `-` sentinel dropped) or "gap": the
    rows of one (agent, user_id, session_id) axis split into bursts wherever
    the pause between consecutive calls exceeds `gap_seconds`. Gap grouping
    is the fallback for callers that send no identity at all (`-` for session
    and user on every call); with an inter-call gap of p90 ~21 s / p99 ~366 s,
    a 60 s cut recovers task-shaped bursts. Rows must
    arrive ordered by ts for "gap". Pure."""

    if by not in ("run", "session", "gap"):
        raise ValueError(f"by must be run|session|gap, got {by!r}")
    groups: dict[str, Trajectory] = {}
    last_ts: dict[str, datetime] = {}
    burst_no: dict[str, int] = {}
    for r in rows:
        op = str(r.get("operation") or "")
        if not op.startswith(MEMORY_PLANE_PREFIXES):
            continue
        run_id = str(r.get("run_id") or "")
        agent = str(r.get("agent") or "")
        ts = r.get("ts")
        if not isinstance(ts, datetime):
            ts = datetime.fromisoformat(str(ts))
        if by == "run":
            if not run_id:
                continue
            key = run_id
        elif by == "session":
            sid = str(r.get("session_id") or "")
            if sid in SENTINELS:
                continue
            key = f"{agent}/{sid}"
        else:
            axis = f"{agent}/{r.get('user_id') or '-'}/{r.get('session_id') or '-'}"
            prev = last_ts.get(axis)
            if prev is None or (ts - prev).total_seconds() > gap_seconds:
                burst_no[axis] = burst_no.get(axis, 0) + 1
            last_ts[axis] = ts
            key = f"{axis}#{burst_no[axis]}"
        t = groups.get(key)
        if t is None:
            t = groups[key] = Trajectory(key=key, grouping=by, agent=agent)
        t.steps.append(
            Step(
                ts=ts,
                operation=op,
                success=bool(r.get("success", True)),
                latency_ms=int(r.get("latency_ms") or 0),
                top_k=_to_int(r.get("top_k")),
                memory_key=str(r.get("memory_key") or ""),
            )
        )
    out = [t for t in groups.values() if len(t.steps) >= min_ops]
    for t in out:
        t.steps.sort(key=lambda s: s.ts)
    out.sort(key=lambda t: (t.steps[0].ts, t.key))
    fold_trajectories.dropped = len(groups) - len(out)  # type: ignore[attr-defined]
    return out


def choose_grouping(rows: list[dict[str, Any]], *, min_identity_share: float = 0.5) -> str:
    """`--by auto`: runs when any row names one; sessions when at least
    `min_identity_share` of memory-plane rows carry a session id; else gap
    bursts. Pure."""

    plane = [r for r in rows if str(r.get("operation") or "").startswith(MEMORY_PLANE_PREFIXES)]
    if any(str(r.get("run_id") or "") for r in plane):
        return "run"
    if not plane:
        return "session"
    with_sid = sum(1 for r in plane if str(r.get("session_id") or "") not in SENTINELS)
    return "session" if with_sid / len(plane) >= min_identity_share else "gap"


def attach_runs(
    trajectories: list[Trajectory],
    runs: list[dict[str, Any]],
    usage: list[dict[str, Any]],
) -> None:
    """Join run outcome and per-entry credit onto run trajectories. Pure.

    `runs`: agent_runs rows (run_id, status, memories_used, memories_written —
    both lists of memory KEYS). `usage`: (memory_key, event_type, n) rows.
    `credit` sums used / evidence / harm over the keys the run wrote;
    `credit_used` over the keys it read (a failed run that read a harmful
    row shows up here)."""

    by_run = {str(r.get("run_id") or ""): r for r in runs}
    credit_by_key: dict[str, Counter[str]] = {}
    for u in usage:
        key = str(u.get("memory_key") or u.get("memory_id") or "")
        credit_by_key.setdefault(key, Counter())[str(u.get("event_type") or "")] += int(
            u.get("n") or 0
        )
    for t in trajectories:
        if t.grouping != "run":
            continue
        r = by_run.get(t.key)
        if r is None:
            continue
        t.outcome = str(r.get("status") or "unknown") or "unknown"
        used = [str(k) for k in (r.get("memories_used") or [])]
        written = [str(k) for k in (r.get("memories_written") or [])]
        t.memories_used = len(used)
        t.memories_written = len(written)
        c: Counter[str] = Counter()
        for k in written:
            c.update(credit_by_key.get(k, Counter()))
        t.credit = dict(c)
        cu: Counter[str] = Counter()
        for k in used:
            cu.update(credit_by_key.get(k, Counter()))
        t.credit_used = dict(cu)


def decile_profile(trajectories: list[Trajectory]) -> list[dict[str, float]]:
    """Class share by position decile across trajectories: the reads-per-step curve read
    along one task. Returns ten dicts (class → share) for deciles 0..9. Pure."""

    buckets: list[Counter[str]] = [Counter() for _ in range(10)]
    for t in trajectories:
        n = len(t.steps)
        if n == 0:
            continue
        for i, s in enumerate(t.steps):
            d = min(9, (i * 10) // n)
            buckets[d][s.verb] += 1
    out: list[dict[str, float]] = []
    for b in buckets:
        total = sum(b.values())
        out.append({k: round(v / total, 3) for k, v in sorted(b.items())} if total else {})
    return out


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    idx = min(len(vs) - 1, max(0, round(q * (len(vs) - 1))))
    return vs[idx]


def summarize(trajectories: list[Trajectory], *, dropped: int = 0) -> dict[str, Any]:
    """Aggregate over trajectories. `dropped` is how many groups fell under
    --min-ops: under run grouping that is the count of run ids with a single
    op, the shape a caller produces when it sends a per-request id as the
    run id (N run ids for N memory.set calls). Pure."""

    n = len(trajectories)
    if n == 0:
        return {"trajectories": 0, "dropped_under_min_ops": dropped}
    ops = [len(t.steps) for t in trajectories]
    rpw = [t.reads_per_write for t in trajectories if t.reads_per_write is not None]
    totals: Counter[str] = Counter()
    for t in trajectories:
        totals.update(t.counts)
    total_ops = sum(totals.values())
    outcomes = Counter(t.outcome for t in trajectories)
    by_outcome: dict[str, dict[str, Any]] = {}
    for oc in outcomes:
        group = [t for t in trajectories if t.outcome == oc]
        g_rpw = [t.reads_per_write for t in group if t.reads_per_write is not None]
        by_outcome[oc] = {
            "n": len(group),
            "median_ops": statistics.median(len(t.steps) for t in group),
            "median_reads_per_write": statistics.median(g_rpw) if g_rpw else None,
            "share_with_update": round(sum(1 for t in group if t.counts["update"]) / len(group), 3),
            "share_with_scratchpad": round(
                sum(1 for t in group if t.counts["scratchpad"]) / len(group), 3
            ),
        }
    return {
        "trajectories": n,
        "grouping": trajectories[0].grouping,
        "ops_total": total_ops,
        "ops_median": statistics.median(ops),
        "ops_p90": _pct([float(o) for o in ops], 0.9),
        "class_share": {k: round(v / total_ops, 3) for k, v in totals.items() if v},
        "reads_per_write_p50": _pct(rpw, 0.5),
        "reads_per_write_p90": _pct(rpw, 0.9),
        "share_with_update": round(sum(1 for t in trajectories if t.counts["update"]) / n, 3),
        "share_with_delete": round(sum(1 for t in trajectories if t.counts["delete"]) / n, 3),
        "share_with_scratchpad": round(
            sum(1 for t in trajectories if t.counts["scratchpad"]) / n, 3
        ),
        "share_read_only": round(sum(1 for t in trajectories if t.writes == 0) / n, 3),
        "dropped_under_min_ops": dropped,
        "outcomes": dict(outcomes),
        "by_outcome": by_outcome,
        "decile_profile": decile_profile(trajectories),
    }


# --------------------------------------------------------------------------
# ClickHouse loads
# --------------------------------------------------------------------------


async def load_events(
    ch: Any, *, org: str, namespace: str | None, days: int, limit: int
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"org": org, "days": int(days), "lim": int(limit)}
    ns = ""
    if namespace:
        ns = "AND namespace = {ns:String}"
        params["ns"] = namespace
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT ts, run_id, session_id, user_id, agent, operation, success, latency_ms,
               args['top_k'] AS top_k, memory_key
        FROM loom.agent_events
        WHERE org = {{org:String}} {ns}
          AND ts > now() - INTERVAL {{days:UInt16}} DAY
          AND kind IN ('request', '')
          AND (operation LIKE 'memory.%' OR operation LIKE 'working_memory.%'
               OR operation LIKE 'context.%' OR operation LIKE 'facts.%')
        ORDER BY ts
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return rows


async def load_runs(ch: Any, *, org: str, namespace: str | None, days: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"org": org, "days": int(days)}
    ns = ""
    if namespace:
        ns = "AND namespace = {ns:String}"
        params["ns"] = namespace
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT run_id, status, memories_used, memories_written
        FROM loom.agent_runs FINAL
        WHERE org = {{org:String}} {ns}
          AND started_at > now() - INTERVAL {{days:UInt16}} DAY
        LIMIT 100000
        """,
        params=params,
    )
    return rows


async def load_usage(
    ch: Any, *, org: str, memory_keys: list[str], namespace: str | None = None
) -> list[dict[str, Any]]:
    """Per-key used / evidence / harm counts for the keys a run reported.

    `agent_runs.memories_used` / `memories_written` hold memory KEYS (the
    SDK and the MemoryArena hook send the keys their searches returned), while
    `memory_usage_events` is keyed by memory id; the join goes through
    loom.memory. Returns rows (memory_key, event_type, n)."""

    if not memory_keys:
        return []
    params: dict[str, Any] = {"org": org, "keys": memory_keys[:50000]}
    ns = ""
    if namespace:
        ns = "AND namespace = {ns:String}"
        params["ns"] = namespace
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT m.memory_key AS memory_key, u.event_type AS event_type, sum(u.event_count) AS n
        FROM loom.memory_usage_events AS u
        INNER JOIN (
            SELECT memory_id, memory_key FROM loom.memory FINAL
            WHERE org = {{org:String}} {ns} AND memory_key IN {{keys:Array(String)}}
              AND tombstoned_at IS NULL
        ) AS m ON u.memory_id = m.memory_id
        WHERE u.org = {{org:String}}
        GROUP BY memory_key, event_type
        """,
        params=params,
    )
    return rows


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


def render_markdown(summary: dict[str, Any], trajectories: list[Trajectory], top: int) -> str:
    if not summary.get("trajectories"):
        return (
            f"no trajectories in window ({summary.get('dropped_under_min_ops', 0)} groups "
            "under --min-ops)\n"
        )
    lines = [
        f"# op trajectories: {summary['trajectories']} {summary['grouping']}s, "
        f"{summary['ops_total']} ops",
        "",
        f"- ops per trajectory: median {summary['ops_median']}, p90 {summary['ops_p90']}",
        f"- reads per write: p50 {_fmt(summary['reads_per_write_p50'])}, "
        f"p90 {_fmt(summary['reads_per_write_p90'])}",
        f"- groups under --min-ops (single-op run ids are a per-request id, not a run): "
        f"{summary.get('dropped_under_min_ops', 0)}",
        f"- share with an update {summary['share_with_update']}, a delete "
        f"{summary['share_with_delete']}, a scratchpad op {summary['share_with_scratchpad']}, "
        f"read-only {summary['share_read_only']}",
        f"- class share: {summary['class_share']}",
        f"- outcomes: {summary['outcomes']}",
        "",
        "| decile | " + " | ".join(CLASSES) + " |",
        "|---|" + "---|" * len(CLASSES),
    ]
    for i, d in enumerate(summary["decile_profile"]):
        lines.append(f"| {i} | " + " | ".join(str(d.get(c, 0)) for c in CLASSES) + " |")
    lines += [
        "",
        f"## longest {top}",
        "",
        "| key | agent | ops | r/w | outcome | sequence |",
        "|---|---|---|---|---|---|",
    ]
    for t in sorted(trajectories, key=lambda t: -len(t.steps))[:top]:
        seq = t.sequence
        if len(seq) > 60:
            seq = seq[:57] + "..."
        lines.append(
            f"| {t.key[:40]} | {t.agent} | {len(t.steps)} | {_fmt(t.reads_per_write)} "
            f"| {t.outcome} | `{seq}` |"
        )
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace) -> int:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    events = await load_events(
        ch, org=args.org, namespace=args.namespace, days=args.days, limit=args.limit
    )
    by = args.by
    if by == "auto":
        by = choose_grouping(events)
    trajectories = fold_trajectories(
        events, by=by, min_ops=args.min_ops, gap_seconds=args.gap_seconds
    )
    if by == "run":
        runs = await load_runs(ch, org=args.org, namespace=args.namespace, days=args.days)
        keys = sorted(
            {
                str(k)
                for r in runs
                for k in list(r.get("memories_written") or []) + list(r.get("memories_used") or [])
            }
        )
        usage = await load_usage(ch, org=args.org, memory_keys=keys, namespace=args.namespace)
        attach_runs(trajectories, runs, usage)
    summary = summarize(trajectories, dropped=getattr(fold_trajectories, "dropped", 0))
    print(render_markdown(summary, trajectories, args.top))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(
                {"summary": summary, "trajectories": [t.row() for t in trajectories]},
                fh,
                indent=1,
                default=str,
            )
        print(f"wrote {args.out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--org", required=True)
    p.add_argument("--namespace", default=None)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--by", choices=("auto", "run", "session", "gap"), default="auto")
    p.add_argument(
        "--gap-seconds",
        type=int,
        default=DEFAULT_GAP_SECONDS,
        help="for --by gap: a pause longer than this starts a new burst (default 60)",
    )
    p.add_argument("--min-ops", type=int, default=2)
    p.add_argument("--limit", type=int, default=500_000)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
