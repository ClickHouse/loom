"""MCP transport — one implicit run per token and `Mcp-Session-Id`.

Leaf module: an in-process registry of the run each MCP session is bound to.
`run.begin` / `run.complete` are not listed in `core`, so a default-tier MCP
client never names a run of its own. The transport mints the run itself:

- The first run-scoped tool call of a session begins a run (goal = that
  call's query or key, `environment="mcp"`, `consumer=<client agent>`,
  `metadata.implicit=true`), in the caller's org and default namespace, filed
  under the user and session the call acts as. Its id binds the token's
  subject (`session_run_id`).
- Writes get the run stamped into their `run_id` argument (provenance, and
  the cross-scope `duplicate` rule); reads are labelled through the
  ledger ContextVar (`server.observability.run_context`) and never filtered.
- The ledger holds the session's calls (`agent_events` by run id and token
  subject, in any org and namespace a call named); the closed run freezes no
  count, since a call's row lands a moment after it answers. A call on the
  session after its DELETE re-opens the run. It closes on `DELETE /mcp` or after
  `mcp_session_run_idle_seconds` without a call to any web process of the
  server (the ledger says which) —
  `status="completed"`, `outcome="none"` (the transport knows the session
  ended, not how it went, and never guesses). The sweep is request-driven
  (once a minute at most) so it needs no worker.
- Begins and closes run detached from the request, as the sweep does: they are
  the transport's own bookkeeping, the id already proves the run is the
  token's, and they run before the call's read scope is bound (a DELETE has
  none at all).

Bounded like the session→agent cache: at the cap the idlest run is closed
and evicted. A close the ledger did not answer is retried by the sweep of a
web process serving the run, unless the session goes on. Ledger failures never
fail a tool call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from server.core.background import TaskSet
from server.tasks.domain.session_runs import session_run_id

log = logging.getLogger(__name__)

# One run per live MCP session; sessions are short-lived, so a churning
# process still stays bounded (same cap as the session→agent cache).
SESSION_RUN_CAP = 4096
# The sweep runs at most this often, from the request path.
SWEEP_MIN_INTERVAL_S = 60.0
# Closes one sweep retries at most, oldest first.
CLOSE_RETRIES_PER_SWEEP = 64
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


@dataclass(frozen=True)
class Caller:
    """Whose session it is: the token's subject, which binds the run id, and the
    user and session the call acts as, which the run row is filed under; "-"
    for each with no auth."""

    subject: str = "-"
    user_id: str = "-"
    session_id: str = "-"

    @classmethod
    def of_call(cls, auth: Any, arguments: dict[str, Any]) -> Caller | None:
        """The caller as the call's read scope resolves it: the scope the admission
        gate binds next, from the same arguments, so the run row stays inside it.
        None when it does not resolve (the gate refuses the call, or admits it
        acting as no one)."""
        from server.core.errors import AuthInsufficientError, IdentityIncompleteError
        from server.identity.domain.read_scope import resolve_read_filters

        try:
            scope = resolve_read_filters(
                requested=arguments.get("read_scope"),
                user_id=arguments.get("user_id"),
                session_id=arguments.get("session_id"),
                auth=auth,
            )
        except (AuthInsufficientError, IdentityIncompleteError):
            return None
        return cls(
            subject=subject_of(auth),
            user_id=(scope.user_id if scope.narrowed else None) or "-",
            session_id=(scope.session_id if scope.scope == "own_session" else None) or "-",
        )


def subject_of(auth: Any) -> str:
    """The token's subject; "-" with no auth."""
    return str(getattr(auth, "subject", "") or "-")


@dataclass
class SessionRun:
    run_id: str
    org: str
    namespace: str
    agent: str
    caller: Caller
    # The session its row is filed under: the one the token pins, else the MCP session.
    row_session_id: str
    goal: str
    # Wall clock when this process registered it: a new row starts here, before
    # the first call's ledger row.
    started_at: datetime
    last_seen: float
    # When this process last asked the ledger whether the run is still open.
    checked_at: float
    begin_task: asyncio.Task[None] | None = None
    begin_failed: bool = False


# Keyed by run id: two callers sending one session id hold two runs.
_RUNS: dict[str, SessionRun] = {}
_BEGIN_FAILED_AT: dict[str, float] = {}
# Closes the ledger did not answer for runs this process served (a DELETE, an
# eviction): run id -> (org, subject, reason, when it was asked), oldest first.
# Each sweep retries a few and stops at the first the ledger still refuses. A
# close is dropped when the session goes on, and given up with a warning once
# it is an idle window old or past the cap; the run then stays open in the
# ledger for `run.complete`.
_CLOSE_LATER: dict[str, tuple[str, str, str, datetime]] = {}
_PENDING = TaskSet()  # in-flight run.begin, and evicted runs' closes
_last_sweep: float = 0.0


def reset() -> None:
    """Test hook: forget every session run without closing it."""
    _RUNS.clear()
    _BEGIN_FAILED_AT.clear()
    _CLOSE_LATER.clear()
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
    if not _RUNS and not _BEGIN_FAILED_AT and not _CLOSE_LATER:
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


def current(run_id: str) -> SessionRun | None:
    return _RUNS.get(run_id)


async def ensure(
    session_id: str | None,
    defaults: dict[str, str] | None,
    caller: Caller,
    *,
    goal: str,
    now: float | None = None,
) -> SessionRun | None:
    """The session's run, begun on first use, in the org and namespace `defaults`
    name. None when the transport has no session or caller identity, or
    `run.begin` failed (retried after the idle window, not on every call)."""
    if not session_id or not defaults:
        return None
    now = time.monotonic() if now is None else now
    org = str(defaults.get("org") or "")
    namespace = str(defaults.get("namespace") or "")
    agent = str(defaults.get("agent") or "")
    if any(v in ("", "-") for v in (org, namespace, agent)):
        return None
    run_id = session_run_id(org, caller.subject, session_id)
    run = _RUNS.get(run_id)
    if run is not None:
        run.last_seen = now
        if now - run.checked_at >= SWEEP_MIN_INTERVAL_S:
            # A sibling web process may have closed it since (its idle sweep, a
            # DELETE, a cap eviction): ask the ledger, off the call's path, and
            # re-open it if so.
            run.checked_at = now
            run.begin_task = _PENDING.spawn(
                _begin(run, now=now, first=False), name=f"mcp-run-check:{run_id}", detached=True
            )
        return run
    failed_at = _BEGIN_FAILED_AT.get(run_id)
    if failed_at is not None and now - failed_at < idle_seconds():
        return None
    if len(_RUNS) >= SESSION_RUN_CAP:
        # A cap, not a sign the session ended: the close does not wait for the
        # session to go idle, and a sibling still serving it re-opens the run.
        idlest = min(_RUNS.values(), key=lambda r: r.last_seen)
        _RUNS.pop(idlest.run_id, None)
        _PENDING.spawn(
            _end(idlest, reason="evicted"), name=f"mcp-run-evict:{idlest.run_id}", detached=True
        )
    from server.core.datetimes import naive_utc_now

    # The run is registered now, under a deterministic id, and its begin lands
    # in the background: an awaited agent_runs insert would hold every session's
    # first tool call for ~3 s on ClickHouse Cloud, and the id is all the call
    # needs.
    run = SessionRun(
        run_id=run_id,
        org=org,
        namespace=namespace,
        agent=agent,
        caller=caller,
        row_session_id=caller.session_id if caller.session_id != "-" else session_id,
        goal=goal,
        started_at=naive_utc_now(),
        last_seen=now,
        checked_at=now,
    )
    _RUNS[run_id] = run
    _BEGIN_FAILED_AT.pop(run_id, None)
    _CLOSE_LATER.pop(run_id, None)  # the session goes on: its close is void
    run.begin_task = _PENDING.spawn(
        _begin(run, now=now, first=True), name=f"mcp-run-begin:{run_id}", detached=True
    )
    return run


async def _begin(run: SessionRun, *, now: float, first: bool) -> None:
    """`run.begin` for a registered run, unless another web process of this server
    began it already. A re-opened run keeps the session's first goal. A
    `first` begin that fails forgets the run and the session waits out the idle
    window before the next attempt; a failed check of a run already begun keeps
    it, and the next check tries again."""
    try:
        from server.tasks.api.models_run import RunBeginInput
        from server.tasks.app.run import active_session_runs, begin_session_run

        if await active_session_runs(run.org, run.run_id):
            return
        await begin_session_run(
            RunBeginInput(
                org=run.org,
                namespace=run.namespace,
                agent=run.agent,
                run_id=run.run_id,
                user_id=run.caller.user_id,
                session_id=run.row_session_id,
                goal=run.goal or "mcp session",
                environment="mcp",
                consumer=run.agent[:64],
                metadata={"implicit": True, "transport": "mcp"},
            ),
            started_at=run.started_at,
        )
    except Exception as e:  # noqa: BLE001 — the ledger never fails a tool call
        log.warning("mcp session run: run.begin failed for %s (non-fatal): %s", run.run_id, e)
        if first:
            run.begin_failed = True
            if _RUNS.get(run.run_id) is run:
                _RUNS.pop(run.run_id, None)
            _BEGIN_FAILED_AT[run.run_id] = now


async def drain_ledger_writes() -> None:
    """Await every in-flight `run.begin` and evicted run's close (a test's sync point)."""
    if len(_PENDING):
        await asyncio.gather(*_PENDING, return_exceptions=True)


# What a close did: completed the run; found it gone (closed elsewhere, or its
# begin never landed); or left it for later (served lately, or the ledger did
# not answer).
_Closed = Literal["closed", "gone", "later"]


async def _close(
    org: str, run_id: str, subject: str, *, reason: str, asked_at: datetime | None = None
) -> _Closed:
    """Complete the run if the ledger says the session is over. An idle close
    waits until no call reached the ledger within the idle window; a DELETE or an
    eviction does not wait. A retried close (`asked_at`) is void when the session
    made a call after it was asked: it went on, on some web process."""
    try:
        from server.core.datetimes import naive_utc_now
        from server.tasks.api.models_run import RunCompleteInput
        from server.tasks.app.run import handle_run_complete, session_run_activity

        found = await session_run_activity(org, run_id, "" if subject == "-" else subject)
        if found is None:
            return "gone"
        namespaces, last_call = found
        if asked_at is not None and last_call is not None and last_call > asked_at:
            return "gone"
        if reason == "idle" and last_call is not None:
            if (naive_utc_now() - last_call).total_seconds() < idle_seconds():
                return "later"  # a sibling web process served it lately
        for namespace in namespaces:
            await handle_run_complete(
                RunCompleteInput(
                    org=org,
                    namespace=namespace,
                    run_id=run_id,
                    status="completed",
                    # The transport saw the session end; it cannot say how the
                    # work went, and `none` writes no evidence or harm.
                    outcome="none",
                    # 0: from the run's start, which every web process shares.
                    duration_ms=0,
                    metadata={"closed_by": f"mcp_{reason}"},
                )
            )
        return "closed"
    except Exception as e:  # noqa: BLE001 — best effort; a sweep tries again
        log.warning("mcp session run: run.complete failed for %s (non-fatal): %s", run_id, e)
        return "later"


async def _close_run(run: SessionRun, *, reason: str) -> _Closed:
    """`_close` for a run this process registered, once its begin has landed."""
    if run.begin_task is not None and not run.begin_task.done():
        await asyncio.gather(run.begin_task, return_exceptions=True)  # complete after begin
    if run.begin_failed:
        return "gone"  # this process's begin never landed
    return await _close(run.org, run.run_id, run.caller.subject, reason=reason)


async def _end(run: SessionRun, *, reason: str) -> _Closed:
    """Close a run this process has let go of (a DELETE, an eviction); a close
    the ledger did not answer is kept for the next sweep."""
    from server.core.datetimes import naive_utc_now

    asked_at = naive_utc_now()
    outcome = await _close_run(run, reason=reason)
    if outcome == "later":
        if len(_CLOSE_LATER) >= SESSION_RUN_CAP:
            _give_up(next(iter(_CLOSE_LATER)), "too many closes waiting")
        _CLOSE_LATER[run.run_id] = (run.org, run.caller.subject, reason, asked_at)
    return outcome


def _give_up(run_id: str, why: str) -> None:
    _CLOSE_LATER.pop(run_id, None)
    log.warning("mcp session run: gave up closing %s (%s); it stays open", run_id, why)


async def _retry_closes() -> int:
    """Retry the oldest closes the ledger did not answer; returns how many closed."""
    from server.core.datetimes import naive_utc_now

    closed = 0
    for run_id in list(_CLOSE_LATER)[:CLOSE_RETRIES_PER_SWEEP]:
        org, subject, reason, asked_at = _CLOSE_LATER[run_id]
        outcome = await _close(org, run_id, subject, reason=reason, asked_at=asked_at)
        if outcome == "later":
            if (naive_utc_now() - asked_at).total_seconds() >= idle_seconds():
                _give_up(run_id, "the ledger did not answer for an idle window")
                continue
            break  # the ledger is still down; the next sweep tries again
        _CLOSE_LATER.pop(run_id, None)
        closed += outcome == "closed"
    return closed


async def close_session(session_id: str | None, org: str, subject: str) -> bool:
    """`DELETE /mcp`: the client ended the session; close its run, the one its id
    names for this token in its org (`session_run_id`), whichever web process
    served it. A close the ledger did not answer is retried by this process's
    sweep when it was serving the run, else left to the idle sweep of one that is."""
    if not session_id or not org:
        return False
    from server.core.background import detached_task

    run_id = session_run_id(org, subject, session_id)
    run = _RUNS.pop(run_id, None)
    ending = (
        _end(run, reason="session_end")
        if run is not None
        else _close(org, run_id, subject, reason="session_end")
    )
    return await detached_task(ending, name=f"mcp-run-end:{run_id}") == "closed"


async def sweep_idle(*, now: float | None = None, force: bool = False) -> int:
    """Close runs idle for longer than the idle window, and retry the closes the
    ledger did not answer. Throttled to once per SWEEP_MIN_INTERVAL_S unless
    forced. A run the ledger shows another process served lately stays, for the
    next sweep. Returns how many closed."""
    global _last_sweep
    now = time.monotonic() if now is None else now
    if not force and now - _last_sweep < SWEEP_MIN_INTERVAL_S:
        return 0
    _last_sweep = now
    idle = idle_seconds()
    closed = 0
    for run in [r for r in _RUNS.values() if now - r.last_seen >= idle]:
        outcome = await _close_run(run, reason="idle")
        if outcome == "later":
            continue
        if _RUNS.get(run.run_id) is run:
            _RUNS.pop(run.run_id)
        closed += outcome == "closed"
    closed += await _retry_closes()
    for run_id, failed_at in list(_BEGIN_FAILED_AT.items()):
        if now - failed_at >= idle:
            _BEGIN_FAILED_AT.pop(run_id, None)
    return closed


def live_count() -> int:
    return len(_RUNS)


__all__ = [
    "GOAL_MAX_CHARS",
    "SESSION_RUN_CAP",
    "SWEEP_MIN_INTERVAL_S",
    "Caller",
    "SessionRun",
    "close_session",
    "current",
    "drain_ledger_writes",
    "ensure",
    "goal_of",
    "idle_seconds",
    "live_count",
    "reset",
    "stamps_run",
    "subject_of",
    "sweep_due",
    "sweep_idle",
    "takes_run",
]
