"""The operation mix of an org or namespace, read from `loom.agent_events`.

The mix of create / read / update / delete an agent chooses is a signal that
moves with task success: a well-trained policy reads less per step, creates
more, and leans on updates. An agent that only ever gets, searches and creates
(thousands of reads and creates against a handful of updates) has the
untrained shape.

This module classifies every wire operation into one of a few verb classes
and returns the counts, the two ratios that describe the shape, and the
top operations, so `ops.health` can report it per org or per namespace and
the Health page can draw it. It reads only; nothing here changes a write.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["ok", "warn", "fail"]
VerbClass = str  # create | read | update | annotate | delete | scratchpad | maintain | other

_CREATE = {
    "memory.set",
    "memory.import_evidence",
    "memory.set_from_messages",
    "memory.bulk_set",
    "memory.record.write",
    "memory.import",
    "memory.import_curated",
    "memory.copy_to",
    "memory.demonstrate",
    "context.offload",
}
_READ = {
    "memory.get",
    "memory.owner_report",
    "memory.recall",
    "memory.export_evidence",
    "memory.get_by_id",
    "memory.statuses",
    "memory.auto_retirements",
    "memory.bulk_get",
    "memory.search",
    "memory.ask",
    "memory.brief",
    "memory.keys",
    "memory.list",
    "memory.timeline",
    "memory.related",
    "memory.history",
    "memory.lineage",
    "memory.provenance",
    "memory.stack",
    "memory.aggregate",
    "memory.for_file",
    "memory.record.query",
    "memory.record.history",
    "memory.record.conflicts",
    "memory.exposure_summary",
    "memory.inspect",
    "context.pack",
    "context.fetch",
    "facts.list",
    "facts.traverse",
    "facts.path",
    "facts.related",
    # The structural reads an agent makes before it writes
    "memory.outline",
    "memory.backlinks",
    # The facts on trial and their graded counts
    "memory.proposals",
}
_UPDATE = {
    "memory.update",
    "memory.update_by_id",
    "memory.apply",
    "memory.restore",
    "memory.assign_category",
    # A rename revises where the fact lives; the old key
    # stays readable as a redirect, so it is not a delete.
    "memory.move",
    # Accepting or rejecting a proposal revises its lifecycle
    "memory.proposal_decide",
}
_ANNOTATE = {
    "memory.link",
    "memory.evidence",
    "memory.verify",
    "memory.pin",
    "memory.unpin",
    "memory.touch",
    "memory.promote",
    "memory.cross_promote",
    "memory.outcome",
    "memory.outcome_run",
    "memory.lock",
    "memory.unlock",
}
_DELETE = {
    "memory.delete",
    "memory.delete_version",
    "memory.clear",
    "memory.demote",
    "memory.redact",
    "memory.reject",
    "facts.expire_by_adapter",
}
_MAINTAIN = {
    "memory.consolidate",
    "memory.dedup_list",
    "memory.dedup_resolve",
    "memory.dedup_collapse",
    "memory.resolve_orphan_duplicates",
    "memory.reindex",
    "memory.sweep_expired",
    "memory.health",
    "memory.extraction_status",
    "memory.extraction_status_mine",
    "memory.extraction_retry_mine",
    "memory.search_trace",
    "memory.search_trace_log",
    "memory.agents_list",
    "memory.digests",
}

CLASSES: tuple[VerbClass, ...] = (
    "create",
    "read",
    "update",
    "annotate",
    "delete",
    "scratchpad",
    "maintain",
    "other",
)


def classify(operation: str) -> VerbClass:
    """Map one wire operation name to its verb class."""

    if operation in _CREATE:
        return "create"
    if operation in _READ:
        return "read"
    if operation in _UPDATE:
        return "update"
    if operation in _ANNOTATE:
        return "annotate"
    if operation in _DELETE:
        return "delete"
    if operation.startswith("working_memory."):
        return "scratchpad"
    if operation in _MAINTAIN:
        return "maintain"
    return "other"


@dataclass
class OpMix:
    window_days: int
    counts: dict[VerbClass, int] = field(default_factory=lambda: dict.fromkeys(CLASSES, 0))
    top: list[dict[str, Any]] = field(default_factory=list)
    agents: int = 0

    @property
    def writes(self) -> int:
        return self.counts["create"] + self.counts["update"]

    @property
    def reads_per_write(self) -> float | None:
        """Reads for every row-producing write; None when nothing was written."""

        return self.counts["read"] / self.writes if self.writes else None

    @property
    def update_share(self) -> float | None:
        """Fraction of row-producing writes that revised an existing key."""

        return self.counts["update"] / self.writes if self.writes else None

    def as_data(self) -> dict[str, Any]:
        rpw = self.reads_per_write
        share = self.update_share
        return {
            "window_days": self.window_days,
            "counts": dict(self.counts),
            "writes": self.writes,
            "reads_per_write": round(rpw, 2) if rpw is not None else None,
            "update_share": round(share, 4) if share is not None else None,
            "agents": self.agents,
            "top": self.top,
        }


def fold(rows: list[dict[str, Any]], *, window_days: int, top_n: int = 12) -> OpMix:
    """Fold `(operation, c, agents)` rows into an OpMix. Pure, for tests."""

    mix = OpMix(window_days=window_days)
    agents: set[str] = set()
    ranked: list[tuple[int, str]] = []
    for row in rows:
        op = str(row.get("operation") or "")
        n = int(row.get("c") or 0)
        if not op or n <= 0:
            continue
        mix.counts[classify(op)] += n
        ranked.append((n, op))
        for a in row.get("agents") or ():
            if a:
                agents.add(str(a))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    mix.top = [{"operation": op, "count": n, "class": classify(op)} for n, op in ranked[:top_n]]
    mix.agents = len(agents)
    return mix


async def op_mix(
    ch: Any,
    *,
    org: str,
    namespace: str | None = None,
    window_days: int = 7,
) -> OpMix:
    """Count successful wire operations per name over the window."""

    from server.insights.store import ops as ops_store

    rows = await ops_store.memory_op_counts(org, namespace, window_days, ch=ch)
    return fold(rows, window_days=window_days)


def assess(
    mix: OpMix,
    *,
    reads_per_write_warn: float,
    update_share_warn: float,
    min_writes: int,
) -> tuple[Status, str, str]:
    """Return (status, detail, repair) for the ops.health check.

    Warn on the two untrained shapes: a polling read
    load (reads per write above the bar) and a bank that is only ever
    appended to (update share below the bar once enough was written)."""

    rpw = mix.reads_per_write
    share = mix.update_share
    if mix.writes < min_writes:
        return (
            "ok",
            f"{mix.writes} writes in {mix.window_days} d (below {min_writes}; shape not judged)",
            "",
        )
    problems: list[str] = []
    repairs: list[str] = []
    if rpw is not None and rpw > reads_per_write_warn:
        problems.append(f"{rpw:.1f} reads per write (bar {reads_per_write_warn:g})")
        repairs.append(
            "agents poll: pass if_version on memory.get, read the scratchpad first, lower top_k"
        )
    if share is not None and share < update_share_warn:
        problems.append(f"update share {share:.2%} (bar {update_share_warn:.0%})")
        repairs.append(
            "agents only append: expose memory.update (by key) in the tool profile "
            "so a changed fact revises its row instead of adding a version blind"
        )
    if problems:
        return ("warn", "; ".join(problems), "; ".join(repairs))
    return (
        "ok",
        f"{rpw:.1f} reads per write, update share {share:.2%}, "
        f"{mix.counts['delete']} deletes in {mix.window_days} d",
        "",
    )
