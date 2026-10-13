"""MCP transport — one implicit run per `Mcp-Session-Id`.

Leaf module: an in-process registry of the run each MCP session is bound to.
`run.begin` / `run.complete` are not listed in `core`, so a default-tier MCP
client never names a run of its own. The transport mints the run itself:

- The first run-scoped tool call of a session begins a run (goal = that
  call's query or key, `environment="mcp"`, `consumer=<client agent>`,
  `session_id` = the MCP session, `metadata.implicit=true`).
- Writes get the run stamped into their `run_id` argument (provenance, and
  the cross-scope `duplicate` rule); reads are labelled through the
  ledger ContextVar (`server.observability.run_context`) and never filtered.
- The run closes on `DELETE /mcp` or after `mcp_session_run_idle_seconds`
  without a call to any web process of the server (the ledger says which) —
  `status="completed"`, `outcome="none"` (the transport knows the session
  ended, not how it went, and never guesses). The sweep is request-driven
  (once a minute at most) so it needs no worker.

Bounded like the session→agent cache: at the cap the idlest run is closed
and evicted. Ledger failures never fail a tool call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.core.background import TaskSet

log = logging.getLogger(__name__)

# One run per live MCP session; sessions are short-lived, so a churning
# process still stays bounded (same cap as the session→agent cache).
SESSION_RUN_CAP = 4096
# The sweep runs at most this often, from the request path.
SWEEP_MIN_INTERVAL_S = 60.0
GOAL_MAX_CHARS = 2000

# Ops that never take the session run: the ledger's own verbs (an explicit
# `run.begin` must mint its own id; a `run.complete` without an id must not
# close the session's run by accident) and the code plane (its identity is
# the deployment's, resolved in the handler).
_NEVER_STAMPED_PREFIXES = ("run.", "code.")
# Ops whose `run_id` is part of the row's SCOPE, not its
# provenance. A working-memory block written with the session's run lands at
# run scope, where no other connection, the primer, or a later `replace`
# addressed without it ever reads it. They stay labelled on the ledger.
_RUN_SCOPED_PREFIXES = ("working_memory.",)
# Reads the session run labels although their input carries no `run_id`:
# `search.query` (the `loom_search` tool) is the read agents call most.
_LABELLED_READS = frozenset({"search.query"})


@dataclass
class SessionRun:
    run_id: str
    org: str
    namespace: str
    agent: str
    session_id: str
    last_seen: float
    goal: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


_RUNS: dict[str, SessionRun] = {}
_BEGIN_FAILED_AT: dict[str, float] = {}
_PENDING_BEGINS = TaskSet()  # in-flight run.begin
_last_sweep: float = 0.0


def reset() -> None:
    """Test hook: forget every session run without closing it."""
    _RUNS.clear()
    _BEGIN_FAILED_AT.clear()
    global _last_sweep
    _last_sweep = 0.0


def idle_seconds() -> float:
    from server.settings import settings

    return float(settings.mcp_session_run_idle_seconds)


def takes_run(op: Any) -> bool:
    """Whether a registry op belongs to a session run at all: its input model
    carries the identity `run_id`, outside the ledger's own and the code
    plane's verbs, or it is one of `_LABELLED_READS`. Reads that take a run
    are labelled (ledger ContextVar); writes are stamped (see `stamps_run`)."""
    name = str(getattr(op, "name", "") or "")
    if name in _LABELLED_READS:
        return True
    if name.startswith(_NEVER_STAMPED_PREFIXES):
        return False
    fields = getattr(getattr(op, "input_model", None), "model_fields", {}) or {}
    return "run_id" in fields


def stamps_run(op: Any) -> bool:
    """Whether a registry op's `run_id` argument takes the session run: an op
    that is not `read_only` (a read's `run_id` is a filter) among those
    `takes_run` admits — the rule the MCP `readOnlyHint` reads, so an
    audited or `writes` op a viewer may call is stamped like any write."""
    if not takes_run(op) or str(getattr(op, "name", "")).startswith(_RUN_SCOPED_PREFIXES):
        return False
    return not op.read_only


def sweep_due(now: float | None = None) -> bool:
    """Cheap pre-check for the request path: is a sweep worth scheduling?
    Failed begins count too: with the ledger down no run is ever live, and
    only the sweep forgets them."""
    if not _RUNS and not _BEGIN_FAILED_AT:
        return False
    now = time.monotonic() if now is None else now
    return now - _last_sweep >= SWEEP_MIN_INTERVAL_S


def goal_of(arguments: dict[str, Any], fallback: str) -> str:
    """The run's goal: the first call's query, else its key, else a label."""
    for k in ("query", "goal", "memory_key", "text"):
        v = arguments.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:GOAL_MAX_CHARS]
    return fallback[:GOAL_MAX_CHARS]


def current(session_id: str | None) -> SessionRun | None:
    if not session_id:
        return None
    return _RUNS.get(session_id)


async def ensure(
    session_id: str | None,
    defaults: dict[str, str] | None,
    *,
    goal: str,
    now: float | None = None,
) -> SessionRun | None:
    """The session's run, begun on first use. None when the transport has no
    session or caller identity, the feature is off, or `run.begin` failed
    (retried after the idle window, not on every call)."""
    if not session_id or not defaults:
        return None
    now = time.monotonic() if now is None else now
    run = _RUNS.get(session_id)
    if run is not None:
        run.last_seen = now
        return run
    failed_at = _BEGIN_FAILED_AT.get(session_id)
    if failed_at is not None and now - failed_at < idle_seconds():
        return None
    if len(_RUNS) >= SESSION_RUN_CAP:
        idlest = min(_RUNS.values(), key=lambda r: r.last_seen)
        await _close(idlest, reason="evicted")
        _RUNS.pop(idlest.session_id, None)
    org = str(defaults.get("org") or "")
    namespace = str(defaults.get("namespace") or "")
    agent = str(defaults.get("agent") or "")
    if any(v in ("", "-") for v in (org, namespace, agent)):
        return None
    # The run is registered now, under a deterministic id, and `run.begin`
    # lands in the background: an awaited agent_runs insert would hold every
    # session's first tool call for ~3 s on ClickHouse Cloud, and the id is all
    # the call needs.
    run = SessionRun(
        run_id=run_id_for(session_id),
        org=org,
        namespace=namespace,
        agent=agent,
        session_id=session_id,
        last_seen=now,
        goal=goal,
    )
    _RUNS[session_id] = run
    _BEGIN_FAILED_AT.pop(session_id, None)
    run.extra["begin_task"] = _PENDING_BEGINS.spawn(
        _begin(run, goal=goal, now=now), name=f"mcp-run-begin:{session_id}"
    )
    return run


def run_id_for(session_id: str) -> str:
    """The implicit run's id: `mcp-<session_id>`, so a retry after eviction
    re-sends the same id and `run.begin` takes its replace path."""
    return f"mcp-{session_id}"[:128]


async def _begin(run: SessionRun, *, goal: str, now: float) -> None:
    """`run.begin` for a registered run. A run already active is kept: another
    web process of this server began it for the same session. On failure the run
    is forgotten and the session waits out the idle window before the next attempt."""
    try:
        from server.tasks.api.models_run import RunBeginInput
        from server.tasks.app.run import handle_run_begin

        await handle_run_begin(
            RunBeginInput(
                org=run.org,
                namespace=run.namespace,
                agent=run.agent,
                run_id=run.run_id,
                session_id=run.session_id,
                goal=goal or "mcp session",
                environment="mcp",
                consumer=run.agent[:64],
                metadata={"implicit": True, "transport": "mcp"},
            ),
            keep_active=True,
        )
    except Exception as e:  # noqa: BLE001 — the ledger never fails a tool call
        if _RUNS.get(run.session_id) is run:
            _RUNS.pop(run.session_id, None)
        _BEGIN_FAILED_AT[run.session_id] = now
        log.warning("mcp session run: run.begin failed for %s (non-fatal): %s", run.session_id, e)


async def drain_begins() -> None:
    """Await every in-flight `run.begin` (a test's sync point)."""
    if len(_PENDING_BEGINS):
        await asyncio.gather(*_PENDING_BEGINS, return_exceptions=True)


async def _close(run: SessionRun, *, reason: str) -> bool:
    begin_task = run.extra.get("begin_task")
    if isinstance(begin_task, asyncio.Task) and not begin_task.done():
        await asyncio.gather(begin_task, return_exceptions=True)  # complete after begin
    if _BEGIN_FAILED_AT.get(run.session_id) is not None and _RUNS.get(run.session_id) is not run:
        return False  # begin never landed; there is no run to complete
    try:
        from server.tasks.api.models_run import RunCompleteInput
        from server.tasks.app.run import handle_run_complete, idle_session_run

        # The ledger, not this process, says whether the session is over: a
        # sibling web process may still serve it, or have closed it already.
        calls = await idle_session_run(
            run.org,
            run.namespace,
            run.agent,
            run.run_id,
            idle_s=idle_seconds() if reason == "idle" else None,
        )
        if calls is None:
            return False
        await handle_run_complete(
            RunCompleteInput(
                org=run.org,
                namespace=run.namespace,
                run_id=run.run_id,
                status="completed",
                # The transport saw the session end; it cannot say how the
                # work went, and `none` writes no evidence or harm.
                outcome="none",
                # 0: from the run's start, which every web process shares.
                duration_ms=0,
                metadata={"closed_by": f"mcp_{reason}", "calls": calls},
            )
        )
        return True
    except Exception as e:  # noqa: BLE001 — best effort
        log.warning("mcp session run: run.complete failed for %s (non-fatal): %s", run.run_id, e)
        return False


async def close_session(session_id: str | None) -> bool:
    """`DELETE /mcp`: the client ended the session; close its run."""
    if not session_id:
        return False
    run = _RUNS.pop(session_id, None)
    if run is None:
        return False
    return await _close(run, reason="session_end")


async def sweep_idle(*, now: float | None = None, force: bool = False) -> int:
    """Close runs idle for longer than the idle window. Throttled to once per
    SWEEP_MIN_INTERVAL_S unless forced. Returns how many closed."""
    global _last_sweep
    now = time.monotonic() if now is None else now
    if not force and now - _last_sweep < SWEEP_MIN_INTERVAL_S:
        return 0
    _last_sweep = now
    idle = idle_seconds()
    stale = [r for r in _RUNS.values() if now - r.last_seen >= idle]
    closed = 0
    for run in stale:
        _RUNS.pop(run.session_id, None)
        if await _close(run, reason="idle"):
            closed += 1
    for sid, t in list(_BEGIN_FAILED_AT.items()):
        if now - t >= idle:
            _BEGIN_FAILED_AT.pop(sid, None)
    return closed


def live_count() -> int:
    return len(_RUNS)


__all__ = [
    "GOAL_MAX_CHARS",
    "SESSION_RUN_CAP",
    "SWEEP_MIN_INTERVAL_S",
    "SessionRun",
    "close_session",
    "current",
    "ensure",
    "goal_of",
    "idle_seconds",
    "live_count",
    "reset",
    "stamps_run",
    "sweep_due",
    "sweep_idle",
    "takes_run",
]
