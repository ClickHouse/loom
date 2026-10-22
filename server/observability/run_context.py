"""The ledger run of the current request.

Two things are both called `run_id` on the wire and they are not the same:

- The identity field `run_id` on a memory op is a SCOPE. On a write it is the
  row's provenance (a cross-scope restatement is a `duplicate` row instead of
  a `merge`); on a read it is a FILTER — a search that carries `run_id` reads
  only that run's rows.
- The ledger wants to know which run a read *served*, without narrowing it.

This module carries the second meaning as a request-scoped ContextVar: the transport sets it
(MCP from the session's implicit run, HTTP from the `X-Loom-Run-Id` header the SDK's ambient
run sends) and the observability paths that read the input's `run_id` fall back to it —
`agent_events.run_id`, the exposure ledger and the search trace log. Nothing on the
retrieval path reads it, so a threaded read is exactly the unthreaded read plus a label.

An explicit `run_id` on the request still wins everywhere: a caller inside a
run scope keeps the scope semantics and the `next` nudge; the implicit run
never produces that nudge (the op it names is not on the default surface).
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from server.core.naming import named

# 128 chars: the bound RunBeginInput applies too.
RUN_ID_MAX_CHARS = 128

ledger_run_var: ContextVar[str] = ContextVar("loom_ledger_run", default="")


def clean_run_id(value: object) -> str:
    """A run id as the ledger stores it: stripped, bounded, sentinel → ''."""
    raw = named(str(value or "").strip())
    return raw[:RUN_ID_MAX_CHARS] if raw else ""


def current_ledger_run() -> str:
    """The run the transport bound this request to ('' outside one)."""
    return ledger_run_var.get()


def set_ledger_run(run_id: object) -> Token[str]:
    """Bind the request's ledger run; reset with `reset_ledger_run(token)`."""
    return ledger_run_var.set(clean_run_id(run_id))


def reset_ledger_run(token: Token[str]) -> None:
    ledger_run_var.reset(token)


def ledger_run_for(explicit: object) -> str:
    """The run a ledger row should name: the request's own `run_id` when it
    carried one, else the transport-bound run, else ''."""
    return clean_run_id(explicit) or current_ledger_run()


__all__ = [
    "RUN_ID_MAX_CHARS",
    "clean_run_id",
    "current_ledger_run",
    "ledger_run_for",
    "ledger_run_var",
    "reset_ledger_run",
    "set_ledger_run",
]
