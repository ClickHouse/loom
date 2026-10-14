"""MCP transport — one implicit run per token and `Mcp-Session-Id`.

The ledger owns the run; this module is each web process's cache of it.

- A session's first run-scoped call begins it in the caller's org and default
  namespace (`metadata.implicit`, the token's subject), under an id that binds
  the token (`session_run_id`). Writes are stamped with it; reads are labelled.
- `DELETE /mcp` closes it from any web process, the `mcp_session_run_sweep`
  worker once it goes idle; a later call re-opens it.
- Begins and closes run detached from the request, as the transport's own
  bookkeeping. Ledger failures never fail a tool call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.background import TaskSet
from server.core.ch import quota_key_var, set_query_tag, set_quota_key
from server.tasks.domain.session_runs import ledger_subject, session_run_id

log = logging.getLogger(__name__)

# Cached runs per process (the session→agent cache's cap); past it the least
# recently used is forgotten.
SESSION_RUN_CAP = 4096
# A cached run is checked with the ledger at most this often, and a failed first
# begin tried again.
CHECK_INTERVAL_S = 60.0
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
    # When this process last asked the ledger whether the run is still open.
    checked_at: float
    # The caller's ClickHouse quota key, which the detached begins and checks carry.
    quota_key: str = ""
    begin_task: asyncio.Task[None] | None = None


# Keyed by run id (two callers sending one session id hold two runs), least
# recently used first.
_RUNS: OrderedDict[str, SessionRun] = OrderedDict()
# Run id -> when its first begin failed, oldest first; capped like _RUNS.
_BEGIN_FAILED_AT: dict[str, float] = {}
_PENDING = TaskSet()  # in-flight begins and checks


def reset() -> None:
    """Test hook: forget every session run without closing it."""
    _RUNS.clear()
    _BEGIN_FAILED_AT.clear()


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


def goal_of(arguments: dict[str, Any], defaults: dict[str, str]) -> str:
    """The run's goal: the first call's query, else its key, else a label. The
    label too when the call works outside the run's org and namespace, whose
    readers may not read where it went: another namespace, or a search across
    every namespace the caller reads (`namespace_scope="all_mine"`)."""
    label = f"mcp session ({defaults.get('agent', '')})"[:GOAL_MAX_CHARS]
    elsewhere = (
        str(arguments.get("org") or defaults.get("org")) != defaults.get("org")
        or str(arguments.get("namespace") or "") != defaults.get("namespace")
        or arguments.get("namespace_scope") == "all_mine"
        or arguments.get("namespaces")
    )
    if elsewhere:
        return label
    for k in ("query", "goal", "memory_key", "text"):
        v = arguments.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:GOAL_MAX_CHARS]
    return label


def current(run_id: str) -> SessionRun | None:
    return _RUNS.get(run_id)


async def ensure(
    session_id: str | None,
    defaults: dict[str, str] | None,
    caller: Caller | None,
    *,
    goal: str,
    now: float | None = None,
) -> SessionRun | None:
    """The session's run, begun on first use, in the org and namespace `defaults`
    name, and filed under the first call's user and session: a later call on the
    session joins it whatever user it acts as. None when the transport has no
    session or caller identity, or `run.begin` failed (retried after
    CHECK_INTERVAL_S, not on every call)."""
    if not session_id or not defaults or caller is None:
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
        _RUNS.move_to_end(run_id)
        if now - run.checked_at >= CHECK_INTERVAL_S:
            # The idle sweep or a DELETE on another web process may have closed
            # it since: ask the ledger, off the call's path, and re-open it if so.
            # A check that races such a DELETE re-opens the run; the sweep closes it.
            run.checked_at = now
            run.begin_task = _PENDING.spawn(
                _begin(run, now=now, first=False), name=f"mcp-run-check:{run_id}", detached=True
            )
        return run
    failed_at = _BEGIN_FAILED_AT.get(run_id)
    if failed_at is not None and now - failed_at < CHECK_INTERVAL_S:
        return None
    if len(_RUNS) >= SESSION_RUN_CAP:
        # A cap, not a sign the session ended: its next call here joins the run.
        _RUNS.popitem(last=False)
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
        checked_at=now,
        quota_key=quota_key_var.get(),
    )
    _RUNS[run_id] = run
    _BEGIN_FAILED_AT.pop(run_id, None)
    run.begin_task = _PENDING.spawn(
        _begin(run, now=now, first=True), name=f"mcp-run-begin:{run_id}", detached=True
    )
    return run


async def _begin(run: SessionRun, *, now: float, first: bool) -> None:
    """`run.begin` for a registered run, unless another web process of this server
    began it already. A re-opened run keeps the session's first goal (of two
    first begins racing on two processes, the later one's). A
    `first` begin that fails forgets the run and the session waits
    CHECK_INTERVAL_S before the next attempt; a failed check of a run already
    begun keeps it, and the next check tries again."""
    _attribute(run.quota_key)
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
                # The idle sweep counts only this token's calls as the session's.
                metadata={
                    "implicit": True,
                    "transport": "mcp",
                    "subject": ledger_subject(run.caller.subject),
                },
            ),
            started_at=run.started_at,
        )
    except Exception as e:  # noqa: BLE001 — the ledger never fails a tool call
        log.warning("mcp session run: run.begin failed for %s (non-fatal): %s", run.run_id, e)
        if first:
            if _RUNS.get(run.run_id) is run:
                _RUNS.pop(run.run_id, None)
            if len(_BEGIN_FAILED_AT) >= SESSION_RUN_CAP:
                _BEGIN_FAILED_AT.pop(next(iter(_BEGIN_FAILED_AT)))
            _BEGIN_FAILED_AT[run.run_id] = now


async def drain_ledger_writes() -> None:
    """Await every in-flight begin and check (a test's sync point)."""
    if len(_PENDING):
        await asyncio.gather(*_PENDING, return_exceptions=True)


async def close_session(session_id: str | None, org: str, subject: str) -> bool:
    """`DELETE /mcp`: the client ended the session; close its run, the one its id
    names for this token in its org (`session_run_id`), whichever web process
    served it. A close the ledger did not answer is left to the idle sweep."""
    if not session_id or not org:
        return False
    from server.core.background import detached_task

    run_id = session_run_id(org, subject, session_id)
    run = _RUNS.pop(run_id, None)
    begin = run.begin_task if run is not None else None
    closing = _close(org, run_id, begin, quota_key_var.get())
    return await detached_task(closing, name=f"mcp-run-end:{run_id}")


def _attribute(quota_key: str) -> None:
    """A detached task starts with no request context: name its statements and
    count them against the caller's quota, as the request's would be."""
    set_query_tag("mcp.session_run")
    set_quota_key(quota_key)


async def _close(org: str, run_id: str, begin: asyncio.Task[None] | None, quota_key: str) -> bool:
    _attribute(quota_key)
    if begin is not None and not begin.done():
        await asyncio.gather(begin, return_exceptions=True)  # complete after begin
    try:
        from server.tasks.app.run import close_session_run

        return await close_session_run(org, run_id, reason="session_end")
    except Exception as e:  # noqa: BLE001 — the idle sweep closes it later
        log.warning("mcp session run: close failed for %s (non-fatal): %s", run_id, e)
        return False


__all__ = [
    "CHECK_INTERVAL_S",
    "GOAL_MAX_CHARS",
    "SESSION_RUN_CAP",
    "Caller",
    "SessionRun",
    "close_session",
    "current",
    "drain_ledger_writes",
    "ensure",
    "goal_of",
    "reset",
    "stamps_run",
    "subject_of",
    "takes_run",
]
