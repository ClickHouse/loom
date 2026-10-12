"""The runs manifest: one line per recent session, for the agent that reads its own
history.

A coding agent that answers from stored trajectories does better with a
manifest — a concise summary of every trajectory (goal, start, action sequence,
outcome): it shortlists from the manifest, then inspects, instead of exploring
raw files. The gain is latency more than accuracy.

Loom's coding agent reads its history through `context.pack`,
`memory.evidence` and `memory.digests`; it has no index of the sessions
and runs behind them to shortlist from. This module renders that index
from what the planes already hold — the run ledger (`agent_runs`: goal,
status, duration), the episodic plane (one observation per tool call:
counts by kind, errors, files touched) and the session digest — as a
bounded list of one-line rows, newest first, that `context.pack` serves
as its `runs` section. Three indexed reads, no model call, fail-open.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.memory.domain.context_layout import label_part, one_item

log = logging.getLogger(__name__)

_MAX_FILES = 4
_GOAL_CHARS = 90
_DIGEST_CHARS = 140


@dataclass
class SessionRow:
    session_id: str
    first_at: datetime | None = None
    last_at: datetime | None = None
    calls: int = 0
    edits: int = 0
    commands: int = 0
    errors: int = 0
    files: list[str] = field(default_factory=list)
    run_id: str = ""
    goal: str = ""
    status: str = ""
    duration_ms: int = 0
    digest: str = ""
    # Who wrote the session's observations, and how many memories its digest compressed.
    actors: list[str] = field(default_factory=list)
    digest_sources: int = 0

    @property
    def when(self) -> datetime | None:
        return self.first_at or self.last_at


def merge_rows(
    observations: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    digests: list[dict[str, Any]],
    *,
    limit: int,
) -> list[SessionRow]:
    """Join the three planes on session_id; newest first; at most `limit`.
    A run without observations and a session without a run both appear —
    the manifest is about what happened, from whichever plane saw it."""
    by_sid: dict[str, SessionRow] = {}

    def row(sid: str) -> SessionRow:
        if sid not in by_sid:
            by_sid[sid] = SessionRow(session_id=sid)
        return by_sid[sid]

    for o in observations:
        sid = str(o.get("session_id") or "")
        if not sid or sid == "-":
            continue
        r = row(sid)
        r.first_at = o.get("first_at") or r.first_at
        r.last_at = o.get("last_at") or r.last_at
        r.calls = int(o.get("n") or 0)
        r.edits = int(o.get("edits") or 0)
        r.commands = int(o.get("commands") or 0)
        r.errors = int(o.get("errors") or 0)
        r.files = [str(f) for f in (o.get("files") or []) if str(f).strip()][:_MAX_FILES]
        r.actors = sorted(str(a) for a in (o.get("actors") or []) if a)
    for run in runs:
        sid = str(run.get("session_id") or "")
        if not sid or sid == "-":
            sid = str(run.get("run_id") or "")
        if not sid:
            continue
        r = row(sid)
        r.run_id = str(run.get("run_id") or "")
        r.goal = " ".join(str(run.get("goal") or "").split())[:_GOAL_CHARS]
        r.status = str(run.get("status") or "")
        r.duration_ms = int(run.get("duration_ms") or 0)
        started = run.get("started_at")
        if started is not None and r.first_at is None:
            r.first_at = started
    for d in digests:
        sid = str(d.get("session_id") or "")
        if sid in by_sid:
            by_sid[sid].digest = " ".join(str(d.get("summary_text") or "").split())[:_DIGEST_CHARS]
            by_sid[sid].digest_sources = int(d.get("derived_n") or 0)
    rows = sorted(
        by_sid.values(), key=lambda r: (r.when is None, r.when or datetime.min), reverse=True
    )
    return rows[: max(0, int(limit))]


def render_manifest(rows: list[SessionRow]) -> str:
    """One line per session: date, session, run status and goal, tool
    counts with errors, files, digest. Terse on purpose — a shortlist, not
    the evidence; the agent opens a session with memory.evidence or
    memory.digests once the line says it is the one."""
    out: list[str] = []
    for r in rows:
        when = r.when.strftime("%Y-%m-%d %H:%M") if r.when else "undated"
        parts = [f"- {when} session={label_part(r.session_id[:24])}"]
        if r.actors:
            parts.append("by=" + ",".join(label_part(a) for a in r.actors))
        if r.run_id or r.status:
            dur = f" {r.duration_ms / 1000:.0f}s" if r.duration_ms else ""
            parts.append(f"run={r.status or 'active'}{dur}")
        if r.goal:
            parts.append(f"goal: {r.goal}")
        if r.calls:
            detail = ", ".join(
                s
                for s in (
                    f"{r.edits} edits" if r.edits else "",
                    f"{r.commands} cmds" if r.commands else "",
                    f"{r.errors} errors" if r.errors else "",
                )
                if s
            )
            parts.append(f"{r.calls} calls" + (f" ({detail})" if detail else ""))
        if r.files:
            parts.append("files: " + ", ".join(r.files))
        if r.digest:
            sources = f" (from {r.digest_sources} memories)" if r.digest_sources else ""
            parts.append(f"digest{sources}: {r.digest}")
        out.append(one_item(" · ".join(parts)))
    return "\n".join(out)


async def fetch_manifest_rows(
    ch: Any,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    limit: int,
) -> list[SessionRow]:
    """Recent sessions of the identity from the episodic plane, the run
    ledger and the digest table. Agent `*`/`-` drops the agent predicate;
    user_id `-`/empty drops the user predicate."""
    where = ["org = {org:String}", "namespace = {ns:String}"]
    params: dict[str, Any] = {"org": org, "ns": namespace, "lim": int(limit)}
    if agent and agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id and user_id != "-":
        where.append("user_id = {uid:String}")
        params["uid"] = user_id
    scope = " AND ".join(where)
    observations = await ch.query_all(
        f"""
        SELECT session_id,
               min(created_at) AS first_at, max(created_at) AS last_at,
               count() AS n,
               countIf(has(tags, 'file_edit')) AS edits,
               countIf(has(tags, 'command_run')) AS commands,
               countIf(has(tags, 'error')) AS errors,
               arraySlice(arrayDistinct(arrayFlatten(groupArray(entities))), 1,
                          {_MAX_FILES}) AS files,
               arraySlice(groupUniqArrayIf(actor, actor != ''), 1, 3) AS actors
        FROM loom.memory FINAL
        WHERE {scope}
          AND memory_type = 'episodic'
          AND has(tags, 'observation')
          AND tombstoned_at IS NULL
        GROUP BY session_id
        ORDER BY last_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    from server.tasks.app.run import recent_run_heads

    runs = await recent_run_heads(
        ch, org=org, namespace=namespace, agent=agent, user_id=user_id, limit=limit
    )
    sids = sorted(
        {str(o.get("session_id") or "") for o in observations}
        | {str(r.get("session_id") or "") for r in runs}
    )
    sids = [s for s in sids if s and s != "-"]
    digests: list[dict[str, Any]] = []
    if sids:
        digests = await ch.query_all(
            f"""
            SELECT session_id, substring(summary_text, 1, {_DIGEST_CHARS * 2}) AS summary_text,
                   length(derived_from) AS derived_n
            FROM loom.session_summaries FINAL
            WHERE {scope}
              AND session_id IN {{sids:Array(String)}}
              AND tombstoned_at IS NULL
            LIMIT 1 BY session_id
            """,
            params={**params, "sids": sids},
        )
    return merge_rows(list(observations or []), list(runs or []), list(digests or []), limit=limit)


async def runs_manifest(
    *, org: str, namespace: str, agent: str, user_id: str, limit: int = 12, ch: Any = None
) -> tuple[str, int]:
    """(rendered manifest, session count) — '' and 0 when there is nothing
    or on any error (the section is additive)."""
    try:
        if ch is None:
            from server.core.ch import get_client

            ch = get_client()
        rows = await fetch_manifest_rows(
            ch, org=org, namespace=namespace, agent=agent, user_id=user_id, limit=limit
        )
    except Exception as e:  # noqa: BLE001 — additive section, never fails the pack
        log.warning("context.pack runs manifest skipped: %s", e)
        return "", 0
    if not rows:
        return "", 0
    return render_manifest(rows), len(rows)
