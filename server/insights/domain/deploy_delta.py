"""The deploy delta: what a colour swap changed, read from the two records the
deployment already keeps.

`loom.agent_events` (the ledger, stamped with `instance_label`/`build_commit`)
says what the deployment's callers saw — volume, latency,
error codes, upstream spend per operation. `system.query_log` says what the
deployment asked ClickHouse — one row per statement shape, by
`normalized_query_hash`, with bytes and seconds. Both are read for a symmetric
window around the swap and the two halves are set side by side; a change past
a threshold is a flag, everything else is a table.

The report is a reading, not a verdict: a p95 that doubled on an operation
with a hundred calls an hour is a lead to open, not a rollback trigger. The
thresholds are deliberately coarse so a quiet swap prints no flags at all.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass
class OpRow:
    operation: str
    n_before: int
    n_after: int
    err_before: int
    err_after: int
    p50_before: float
    p50_after: float
    p95_before: float
    p95_after: float
    usd_before: float
    usd_after: float


@dataclass
class ErrorRow:
    operation: str
    error_code: str
    n_before: int
    n_after: int


@dataclass
class ShapeRow:
    shape: int
    n_before: int
    n_after: int
    bytes_before: int
    bytes_after: int
    ms_before: int
    ms_after: int
    sample: str = ""
    log_comment: str = ""


@dataclass
class VitalsRow:
    """One dashboard route's paint, before → after."""

    route: str
    n_before: int
    n_after: int
    lcp_before: float
    lcp_after: float
    lt_before: float
    lt_after: float
    calls_before: float
    calls_after: float
    nodes_before: float
    nodes_after: float
    # The view's longest interaction, p50 of views.
    inp_before: float = 0.0
    inp_after: float = 0.0


@dataclass
class PlaneRow:
    """One namespace's returned keys by plane, before → after."""

    namespace: str
    n_before: int
    n_after: int
    keys_before: int
    keys_after: int
    chunk_before: int
    chunk_after: int
    summary_before: int
    summary_after: int
    other_before: int
    other_after: int

    @staticmethod
    def _share(part: int, whole: int) -> float:
        return part / whole if whole else 0.0

    @property
    def chunk_share_before(self) -> float:
        return self._share(self.chunk_before, self.keys_before)

    @property
    def chunk_share_after(self) -> float:
        return self._share(self.chunk_after, self.keys_after)


@dataclass
class ReadRow:
    """One read operation's answered/missed keys, before → after."""

    operation: str
    n_before: int
    n_after: int
    asked_before: int
    asked_after: int
    miss_before: int
    miss_after: int

    @property
    def miss_rate_before(self) -> float:
        return self.miss_before / self.asked_before if self.asked_before else 0.0

    @property
    def miss_rate_after(self) -> float:
        return self.miss_after / self.asked_after if self.asked_after else 0.0


@dataclass
class DeployDelta:
    swap_at: datetime
    window_min: int
    builds_before: list[str] = field(default_factory=list)
    builds_after: list[str] = field(default_factory=list)
    calls_before: int = 0
    calls_after: int = 0
    ops: list[OpRow] = field(default_factory=list)
    errors: list[ErrorRow] = field(default_factory=list)
    shapes: list[ShapeRow] = field(default_factory=list)
    shapes_error: str = ""
    vitals: list[VitalsRow] = field(default_factory=list)
    planes: list[PlaneRow] = field(default_factory=list)
    reads: list[ReadRow] = field(default_factory=list)

    @property
    def shapes_runs_before(self) -> int:
        return sum(s.n_before for s in self.shapes)

    @property
    def shapes_runs_after(self) -> int:
        return sum(s.n_after for s in self.shapes)

    @property
    def shapes_before_sparse(self) -> bool:
        """The query_log before-window is missing, not quiet: at least half
        the kept shapes have no before-run (see SHAPES_SPARSE_NEW_SHARE)."""
        if len(self.shapes) < SHAPES_SPARSE_MIN_SHAPES:
            return False
        unseen = sum(1 for s in self.shapes if s.n_before == 0)
        return unseen >= SHAPES_SPARSE_NEW_SHARE * len(self.shapes)

    @property
    def empty(self) -> bool:
        """No deployment rows on either side: the connection or the stamps,
        not a quiet hour — a fail-open client answers an unreachable server
        with no rows."""
        return self.calls_before + self.calls_after == 0

    def to_json(self) -> str:
        d = asdict(self)
        d["swap_at"] = self.swap_at.isoformat()
        d["flags"] = flags(self)
        return json.dumps(d, indent=2, default=str)


MIN_CALLS = 100
MIN_RUNS = 20
P95_RATIO = 1.3
RATE_RATIO = 2.0
COST_RATIO = 1.5
SHAPE_RATIO = 2.0
# query_log is a log, not a ledger: a before-window that lost its rows (a
# replica restart, a flush that has not landed) makes every shape look NEW.
# When at least this share of the kept shapes has no before-run at all — one
# new heavy statement is a finding, eighteen at once is a missing window —
# the report says so once and suppresses the NEW SHAPE flags.
SHAPES_SPARSE_NEW_SHARE = 0.5
SHAPES_SPARSE_MIN_SHAPES = 4
# Result planes: a namespace's chunk share of returned keys that
# moved by this many percentage points, with this many searches on both sides.
PLANE_MIN_SEARCHES = 20
PLANE_SHARE_PP = 0.15
# Read outcomes: a miss rate that doubled and grew by this many points, with
# this many keys asked on both sides.
READ_MIN_ASKED = 100
READ_MISS_PP = 0.05


def _per_k(n: int, total: int) -> float:
    return 1000.0 * n / total if total else 0.0


def flags(d: DeployDelta) -> list[str]:
    out: list[str] = []
    for e in d.errors:
        rb, ra = _per_k(e.n_before, d.calls_before), _per_k(e.n_after, d.calls_after)
        if e.n_after >= 5 and e.n_before == 0:
            out.append(
                f"NEW ERROR {e.operation} {e.error_code}: 0 → {e.n_after} ({ra:.2f}/1k calls)"
            )
        elif e.n_after >= 5 and ra >= RATE_RATIO * rb > 0:
            out.append(
                f"ERROR RATE {e.operation} {e.error_code}: {rb:.2f} → {ra:.2f}/1k calls"
                f" ({e.n_before} → {e.n_after})"
            )
    for o in d.ops:
        if o.n_before >= MIN_CALLS and o.n_after >= MIN_CALLS:
            if o.p95_after >= P95_RATIO * o.p95_before and o.p95_after - o.p95_before >= 50:
                out.append(
                    f"P95 {o.operation}: {o.p95_before:.0f} → {o.p95_after:.0f} ms"
                    f" (n {o.n_before} → {o.n_after})"
                )
            cb, ca = o.usd_before / o.n_before, o.usd_after / o.n_after
            if o.usd_after >= 1.0 and ca >= COST_RATIO * cb:
                out.append(
                    f"COST/CALL {o.operation}: ${cb:.4f} → ${ca:.4f}"
                    f" (${o.usd_before:.2f} → ${o.usd_after:.2f})"
                )
        if o.n_before >= MIN_CALLS and o.n_after == 0:
            out.append(f"STOPPED {o.operation}: {o.n_before} calls before, none after")
    if d.shapes_before_sparse:
        out.append(
            f"QUERY_LOG SPARSE: {d.shapes_runs_before} statements before vs"
            f" {d.shapes_runs_after} after — NEW SHAPE flags suppressed, per-shape deltas kept"
        )
    for s in d.shapes:
        if s.n_before == 0 and s.n_after >= MIN_RUNS and not d.shapes_before_sparse:
            out.append(
                f"NEW SHAPE {s.shape}: {s.n_after} runs, {s.bytes_after / 1e9:.1f} GB,"
                f" {s.ms_after / 1000:.0f} CH-s — {s.log_comment or s.sample[:80]}"
            )
        elif s.n_before >= MIN_RUNS and s.n_after >= MIN_RUNS:
            mb_b, mb_a = s.bytes_before / s.n_before / 1e6, s.bytes_after / s.n_after / 1e6
            ms_b, ms_a = s.ms_before / s.n_before, s.ms_after / s.n_after
            if (mb_a >= SHAPE_RATIO * mb_b and mb_a - mb_b >= 10) or (
                ms_a >= SHAPE_RATIO * ms_b and ms_a - ms_b >= 100
            ):
                out.append(
                    f"SHAPE {s.shape}: {mb_b:.0f} → {mb_a:.0f} MB/run,"
                    f" {ms_b:.0f} → {ms_a:.0f} ms/run — {s.log_comment or s.sample[:80]}"
                )
    for p in d.planes:
        if p.n_before >= PLANE_MIN_SEARCHES and p.n_after >= PLANE_MIN_SEARCHES:
            b, a = p.chunk_share_before, p.chunk_share_after
            if abs(a - b) >= PLANE_SHARE_PP:
                out.append(
                    f"PLANE MIX {p.namespace}: chunk keys {b:.0%} → {a:.0%} of returned"
                    f" ({p.chunk_before}/{p.keys_before} → {p.chunk_after}/{p.keys_after}"
                    f" over {p.n_before} → {p.n_after} searches)"
                )
    for r in d.reads:
        if r.asked_before >= READ_MIN_ASKED and r.asked_after >= READ_MIN_ASKED:
            b, a = r.miss_rate_before, r.miss_rate_after
            if a >= RATE_RATIO * b and a - b >= READ_MISS_PP:
                out.append(
                    f"READ MISSES {r.operation}: {b:.0%} → {a:.0%} of keys asked not found"
                    f" ({r.miss_before}/{r.asked_before} → {r.miss_after}/{r.asked_after})"
                )
    for v in d.vitals:
        if v.n_before >= 3 and v.n_after >= 3:
            if v.lcp_before > 0 and v.lcp_after > v.lcp_before * 1.34:
                out.append(
                    f"PAGE {v.route}: p50 LCP {v.lcp_before:.0f} → {v.lcp_after:.0f} ms"
                    f" over {v.n_before} → {v.n_after} views"
                )
            if v.lt_before > 0 and v.lt_after > v.lt_before * 1.34:
                out.append(
                    f"PAGE {v.route}: p50 long tasks {v.lt_before:.0f} → {v.lt_after:.0f} ms"
                    f" over {v.n_before} → {v.n_after} views"
                )
            # An interaction that grew by a third.
            if v.inp_before > 0 and v.inp_after > v.inp_before * 1.34:
                out.append(
                    f"PAGE {v.route}: p50 interaction {v.inp_before:.0f} → {v.inp_after:.0f} ms"
                    f" over {v.n_before} → {v.n_after} views"
                )
    return out


# --- rendering ---------------------------------------------------------------


def _num(v: float, fmt: str) -> str:
    # A side with no calls has no quantile: ClickHouse returns NaN.
    return "—" if v != v else fmt.format(v)


def _arrow(b: float, a: float, fmt: str = "{:.0f}") -> str:
    return f"{_num(b, fmt)} → {_num(a, fmt)}"


def render(d: DeployDelta, *, top_ops: int = 15) -> str:
    lines = [
        f"# deploy delta — swap at {d.swap_at.isoformat()} ±{d.window_min} min",
        "",
        f"builds before: {', '.join(d.builds_before) or '—'} ({d.calls_before} calls)"
        f" | after: {', '.join(d.builds_after) or '—'} ({d.calls_after} calls)",
        "",
    ]
    if d.empty:
        lines += [
            "WARNING: no deployment rows in the window — check the ClickHouse connection"
            " (`.env` in the working directory) and the LOOM_INSTANCE_LABEL /"
            " LOOM_BUILD_COMMIT stamps; a fail-open client answers an"
            " unreachable server with no rows.",
            "",
        ]
    fl = flags(d)
    lines.append("## flags")
    lines.extend([f"- {f}" for f in fl] or ["- none"])
    lines += [
        "",
        "## operations (deployment rows, before → after)",
        "",
        "| operation | calls | errors | p50 ms | p95 ms | upstream $ |",
        "|---|---|---|---|---|---|",
    ]
    for o in sorted(d.ops, key=lambda r: r.n_before + r.n_after, reverse=True)[:top_ops]:
        lines.append(
            f"| {o.operation} | {o.n_before} → {o.n_after} | {o.err_before} → {o.err_after}"
            f" | {_arrow(o.p50_before, o.p50_after)} | {_arrow(o.p95_before, o.p95_after)}"
            f" | {_arrow(o.usd_before, o.usd_after, '{:.2f}')} |"
        )
    lines += ["", "## error codes", ""]
    if d.errors:
        lines += ["| operation | code | before | after |", "|---|---|---|---|"]
        lines += [
            f"| {e.operation} | {e.error_code} | {e.n_before} | {e.n_after} |"
            for e in d.errors[:20]
        ]
    else:
        lines.append("none in the window")
    lines += ["", "## page vitals (ui.vitals rows, p50 before → after)", ""]
    if d.vitals:
        lines += [
            "| route | views | LCP ms | long tasks ms | INP ms | calls | DOM nodes |",
            "|---|---|---|---|---|---|---|",
        ]
        for v in d.vitals:
            lines.append(
                f"| {v.route} | {v.n_before} → {v.n_after}"
                f" | {_arrow(v.lcp_before, v.lcp_after)} | {_arrow(v.lt_before, v.lt_after)}"
                f" | {_arrow(v.inp_before, v.inp_after)}"
                f" | {_arrow(v.calls_before, v.calls_after)}"
                f" | {_arrow(v.nodes_before, v.nodes_after)} |"
            )
    else:
        lines.append("no dashboard views in the window")
    lines += ["", "## result planes (search_trace_log, returned keys before → after)", ""]
    if d.planes:
        lines += [
            "| namespace | searches | keys | chunk | summary | other | chunk share |",
            "|---|---|---|---|---|---|---|",
        ]
        for p in d.planes[:top_ops]:
            lines.append(
                f"| {p.namespace} | {p.n_before} → {p.n_after} | {p.keys_before} → {p.keys_after}"
                f" | {p.chunk_before} → {p.chunk_after} | {p.summary_before} → {p.summary_after}"
                f" | {p.other_before} → {p.other_after}"
                f" | {p.chunk_share_before:.0%} → {p.chunk_share_after:.0%} |"
            )
    else:
        lines.append("no recorded searches in the window (per-query recall telemetry off?)")
    lines += ["", "## read outcomes (keys asked → not found, before → after)", ""]
    if d.reads:
        lines += ["| operation | calls | asked | missed | miss rate |", "|---|---|---|---|---|"]
        for r in d.reads[:top_ops]:
            lines.append(
                f"| {r.operation} | {r.n_before} → {r.n_after} | {r.asked_before} → {r.asked_after}"
                f" | {r.miss_before} → {r.miss_after}"
                f" | {r.miss_rate_before:.0%} → {r.miss_rate_after:.0%} |"
            )
    else:
        lines.append("no read-outcome columns in the window (rows predate them?)")
    lines += ["", "## ClickHouse statement shapes (deployment sessions, by after-cost)", ""]
    if d.shapes_error:
        lines.append(f"query_log unavailable: {d.shapes_error}")
    elif d.shapes:
        lines += [
            "| shape | runs | GB read | CH-s | ms/run | comment / sample |",
            "|---|---|---|---|---|---|",
        ]
        for s in d.shapes:
            ms_b = s.ms_before / s.n_before if s.n_before else 0.0
            ms_a = s.ms_after / s.n_after if s.n_after else 0.0
            lines.append(
                f"| {s.shape} | {s.n_before} → {s.n_after}"
                f" | {_arrow(s.bytes_before / 1e9, s.bytes_after / 1e9, '{:.1f}')}"
                f" | {_arrow(s.ms_before / 1000, s.ms_after / 1000)} | {_arrow(ms_b, ms_a)}"
                f" | {(s.log_comment + ' ' if s.log_comment else '') + s.sample[:90]} |"
            )
    else:
        lines.append("no shapes met the run floor")
    return "\n".join(lines) + "\n"
