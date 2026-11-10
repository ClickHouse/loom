"""An erasure's `failure_reason` as a requester reads it.

The saga writes `<step>: <Kind>`, `<step>: <CODE>: <message>`, a bare `<Kind>` or
`<CODE>: <message>` outside a step, or a residue failure with its counts; recovery
writes `max_attempts (N) exceeded`, then `; last: <reason>`; `loom reconcile` writes
REPLAY_PENDING_REASON while it replays an erasure after a restore. A build from before the
error kind rule stored the driver's text after the kind, and that text can carry a
statement or a server address, so a reason is cut to these shapes wherever it is read.

Pure and I/O-free.
"""

from __future__ import annotations

import json

from server.core.errors import stored_caller_text

# The failure_reason prefix of the recovery worker's terminal row: a saga at
# its attempts budget whose row does not start with it was never abandoned.
ABANDONED_REASON_PREFIX = "max_attempts ("
# The failure_reason of a ClickHouse row `loom reconcile` writes before it replays a
# completed erasure (`server.erasure.app.replay`): the recovery worker never resumes it,
# since the reconcile owns it and a resumed saga's census would count the user's writes
# made after the erasure. A completed run replaces it; one that failed leaves it.
REPLAY_PENDING_REASON = "replay after a restore pending: run loom reconcile again"
# What separates an abandoned saga's head from the last reason it failed with.
LAST_REASON_SEP = "; last: "

_RESIDUE_KIND = "ErasureResidueError"
# What a reader gets for a stored reason in none of the shapes: its text is not shown.
UNSHOWN_REASON = "the saga failed; the error is in the server log"


def caller_reason(reason: str) -> str:
    """`reason` cut to the shapes the module docstring names; a reason in none of
    them reads as empty, and an abandoned saga's head is kept whole."""
    if reason == REPLAY_PENDING_REASON:
        return reason
    if reason.startswith(ABANDONED_REASON_PREFIX):
        head, _, last = reason.partition(LAST_REASON_SEP)
        last = _one_reason(last)
        return f"{head}{LAST_REASON_SEP}{last}" if last else head
    return _one_reason(reason)


def shown_reason(reason: str) -> str:
    """`caller_reason` for a reader: a reason it cuts to nothing reads as
    UNSHOWN_REASON, so a failed saga never shows an empty reason."""
    return caller_reason(reason) or (UNSHOWN_REASON if reason else "")


# The subject field of each audit event that carries an erasure's failure reason. A
# build before the error-kind rule wrote the driver's text there.
_REASON_FIELDS = {"erasure.failed": "error", "erasure.abandoned": "last_error"}


def caller_audit_subject(event_type: str, subject: str) -> str:
    """An audit row's subject as `audit.list` and `erasure.export` return it: an
    erasure's failure reason cut by `shown_reason`, any other subject as stored."""
    field = _REASON_FIELDS.get(event_type)
    if field is None:
        return subject
    try:
        parsed = json.loads(subject)
    except ValueError:
        return subject
    if not isinstance(parsed, dict) or not isinstance(parsed.get(field), str):
        return subject
    cut = shown_reason(parsed[field])
    if cut == parsed[field]:
        return subject
    return json.dumps({**parsed, field: cut}, sort_keys=True)


def _one_reason(reason: str) -> str:
    step, _, rest = reason.partition(": ")
    if not ("." in step and all(part.isidentifier() for part in step.split("."))):
        step, rest = "", reason
    if step and rest.startswith(f"{_RESIDUE_KIND}: "):
        return reason
    error = stored_caller_text(rest)
    return f"{step}: {error}" if step and error else step or error


__all__ = [
    "ABANDONED_REASON_PREFIX",
    "LAST_REASON_SEP",
    "REPLAY_PENDING_REASON",
    "UNSHOWN_REASON",
    "caller_audit_subject",
    "caller_reason",
    "shown_reason",
]
