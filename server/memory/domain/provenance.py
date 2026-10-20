"""Who wrote a memory row, and what kind of source it came from.

Two columns on `loom.memory`, stamped by the server on every
write and never taken from the request body:

* `actor` — `human:<subject>` or `process:<subject>`, the kind decided by the
  token (`memory.domain.trust.actor_for`), exactly as memory.verify already does.
  Empty only when no auth context exists (dev mode, internal workers that
  pass no fallback).
* `source_class` — how the content entered memory. One of
  `asserted` (the caller wrote it directly), `extracted` (an LLM lifted it
  from conversation), `observed` (observe.record), `derived` (consolidation
  or reflection over other memories), `imported` (copied from another
  namespace, restored, or brought in through a bundle), `external` (content
  the caller got from a tool, a page, a document — not endorsed by anyone).

The caller may only *downgrade*: it can declare `external`, `imported` or
`derived` for a write; it cannot claim `asserted` for extracted content or
promote anything. `derived` is declarable so an OKF bundle re-importing loom's
own inferences keeps them marked as inferences. Both columns are stamped here
because identity that reaches the transport (`enduser.id` on the span) is
otherwise dropped before the row.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

from server.core.errors import ValidationError

SOURCE_CLASSES: Final[frozenset[str]] = frozenset(
    {"asserted", "extracted", "observed", "derived", "imported", "external"}
)
# What a request body is allowed to say about its own write: only a downgrade.
# `derived`: an inference over other memories is lower
# standing than an assertion, so declaring it is a downgrade too.
CALLER_DECLARABLE: Final[frozenset[str]] = frozenset({"external", "imported", "derived"})

# source_trace conventions already in the data → class.
_TRACE_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("pipeline:set_from_messages", "extracted"),
    ("conversation:", "extracted"),
    # A promoted learnings candidate (`learning:<candidate_id>`): the rule was
    # extracted from conversations before a reviewer approved it.
    ("learning:", "extracted"),
    ("observe.record", "observed"),
    # The observe-extract worker's step events and session
    # hints (`observe.extract:event:…`, `observe.extract:hint`) summarise tool
    # observations; written through memory.set by a worker they would otherwise
    # be stamped `asserted`.
    ("observe.extract:", "observed"),
    # A session-outcome contrast reflection (`contrast:<neg><<pos>`) is an
    # inference over two sessions, not a statement.
    ("contrast:", "derived"),
    ("consolidate.", "derived"),
    ("memory.consolidate", "derived"),
    ("profile.fold", "derived"),
    ("reflection:", "derived"),
    ("okf:", "imported"),
    ("curated_doc://", "imported"),
)


# The principal a pipeline stage acts FOR when no request is in flight. The extraction-queue
# worker replays a request the token enqueued hours earlier; the CLI imports on an
# operator's behalf. Neither has an auth context, so without this `writer_actor()` would
# read ''. The variable carries an already-formed `kind:subject` (the kind was decided by
# the token at enqueue time, never by the body) and is read only when `current_auth()` is
# None: a live request's own principal always wins.
pipeline_actor_var: ContextVar[str] = ContextVar("loom_pipeline_actor", default="")

# The worker's own principal when the queue row names nobody (a payload with
# no `enqueued_by`, or a row a dev-mode server enqueued).
EXTRACTION_WORKER_ACTOR: Final = "process:extraction-worker"


@contextmanager
def pipeline_actor(actor: str) -> Iterator[None]:
    """Run a block as `actor` (`kind:subject`) for every write that has no
    auth context of its own. Empty `actor` leaves the fallback chain as it
    was."""
    token = pipeline_actor_var.set(str(actor or ""))
    try:
        yield
    finally:
        pipeline_actor_var.reset(token)


def cli_actor(command: str) -> str:
    """The principal a `loom <command>` invocation writes as."""
    return "process:cli:" + (str(command or "").strip() or "unknown")


def class_from_trace(source_trace: str) -> str:
    """Class implied by a source_trace convention, or '' when it says nothing."""
    s = (source_trace or "").strip()
    for prefix, cls in _TRACE_PREFIXES:
        if s.startswith(prefix):
            return cls
    return ""


def source_class_for(
    *,
    default: str,
    requested: str | None = None,
    source_trace: str = "",
) -> str:
    """Decide the stored class for one write.

    Precedence: a caller-declared downgrade (`external` / `imported` / `derived`) wins;
    then the class the source_trace convention implies; then the operation's
    default. Any other requested value is a 422 — the body may not promote.
    """
    req = (requested or "").strip()
    if req:
        if req not in SOURCE_CLASSES:
            raise ValidationError(f"source_class {req!r} is not one of {sorted(SOURCE_CLASSES)}")
        if req not in CALLER_DECLARABLE:
            raise ValidationError(
                f"source_class {req!r} is decided by the server; a caller may only "
                f"declare {sorted(CALLER_DECLARABLE)}"
            )
        return req
    implied = class_from_trace(source_trace)
    if implied:
        return implied
    if default not in SOURCE_CLASSES:
        raise ValueError(f"bad default source_class {default!r}")
    return default
