"""The consumer a request speaks for, as one ledger column.

Every operation accepts `consumer` — the client module or product calling
loom (a QA agent, a coding-agent plugin, a memory application, a bench
harness). It is a column of its own on `loom.agent_events` and the audit
stream, so a per-consumer error rate, latency or spend is not a Map lookup over
every row and a `budget.violated` row can say who was slow.

This module resolves the value once per request and publishes it on a
request-scoped ContextVar, so the event emitter, the audit writer and the
budget gate all read the same string without per-call plumbing:

    the verified token's `consumer` CLAIM (bound at mint time,
        the way the traffic_class claim binds the class)
    else explicit `consumer` in the request (stripped, bounded to 64 chars)
    else the token's SUBJECT CLASS: 'service-account' | 'user' | 'anonymous'

`consumer_source` says which layer named it — `claimed`, `self-reported` or
`class` — and rides the event row's `args` and `admin/consumers`, so a spend
or error-rate figure can say whether its tag was bound to a credential or
was the client's own word (any client can send any tag).

The class fallback keeps the column non-empty on every row —
a GROUP BY consumer then separates "a client that named
itself" from "a human token" from "no token" instead of lumping all three
into ''. The transports set the ContextVar right after they know the
arguments (and again once the token is verified, so the class is exact);
process-lifetime tasks run in an empty context and never inherit it.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

CONSUMER_MAX_CHARS = 64

# The three subject classes a request can fall back to.
CLASS_SERVICE_ACCOUNT = "service-account"
CLASS_USER = "user"
CLASS_ANONYMOUS = "anonymous"

# The consumers that inspect a namespace on a person's behalf rather than use it: the
# dashboard and the assistant toolset (`?toolset=assistant`). Usage reports leave their
# calls out of the counts they grade, so looking at a namespace does not change its grade.
UI_CONSUMER = "loom-ui"
ASSISTANT_CONSUMER = "loom-assistant"
INSPECTOR_CONSUMERS: tuple[str, ...] = (UI_CONSUMER, ASSISTANT_CONSUMER)

# Where the consumer value came from.
SOURCE_CLAIMED = "claimed"
SOURCE_SELF_REPORTED = "self-reported"
SOURCE_CLASS = "class"

_current: ContextVar[str] = ContextVar("loom_consumer", default="")
_current_source: ContextVar[str] = ContextVar("loom_consumer_source", default="")


def explicit_consumer(raw_args: dict[str, Any] | None) -> str:
    """The `consumer` the request named, normalised; '' when absent/unusable.
    Only a string or int is accepted (a bool or a nested object is not a
    consumer), whitespace is stripped, control characters are dropped and
    the value is bounded to CONSUMER_MAX_CHARS so a LowCardinality column
    never receives an unbounded or multi-line dictionary entry."""
    if not raw_args:
        return ""
    raw = raw_args.get("consumer")
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return ""
    text = "".join(ch for ch in str(raw) if ch.isprintable()).strip()
    if text in ("", "-"):
        return ""
    return text[:CONSUMER_MAX_CHARS]


def subject_class(auth: Any) -> str:
    """The token's subject class: 'anonymous' without a verified token,
    'service-account' for an `sa:` subject or a token minted with `sa=true`,
    'user' otherwise."""
    if auth is None:
        return CLASS_ANONYMOUS
    subject = str(getattr(auth, "subject", "") or "")
    if subject.startswith("sa:") or bool(getattr(auth, "is_service_account", False)):
        return CLASS_SERVICE_ACCOUNT
    return CLASS_USER


def claim_consumer(raw: Any) -> str:
    """A `consumer` token claim normalised the way the request field is:
    printable, stripped, bounded; '' when unusable."""
    return explicit_consumer({"consumer": raw}) if raw is not None else ""


def claimed_consumer(auth: Any) -> str:
    """The verified token's `consumer` claim, '' when the token has none."""
    if auth is None:
        return ""
    return claim_consumer(getattr(auth, "consumer", "") or "")


def resolve_consumer_and_source(raw_args: dict[str, Any] | None, auth: Any) -> tuple[str, str]:
    """(consumer, source): the token's claim → `claimed`; else the request's
    own field → `self-reported`; else the subject class → `class`."""
    claimed = claimed_consumer(auth)
    if claimed:
        return claimed, SOURCE_CLAIMED
    explicit = explicit_consumer(raw_args)
    if explicit:
        return explicit, SOURCE_SELF_REPORTED
    return subject_class(auth), SOURCE_CLASS


def bind_current_consumer(raw_args: dict[str, Any] | None, auth: Any) -> None:
    """Resolve and publish (consumer, source) for this request — one call for
    both transports."""
    set_current_consumer(*resolve_consumer_and_source(raw_args, auth))


def set_current_consumer(value: str, source: str = "") -> None:
    _current.set(value or "")
    _current_source.set(source or "")


def current_consumer() -> str:
    """The request's consumer, or '' outside a request (a worker pass, a
    process-lifetime task)."""
    return _current.get()


def current_consumer_source() -> str:
    """`claimed` | `self-reported` | `class`, or '' outside a request."""
    return _current_source.get()


__all__ = [
    "ASSISTANT_CONSUMER",
    "CLASS_ANONYMOUS",
    "CLASS_SERVICE_ACCOUNT",
    "CLASS_USER",
    "CONSUMER_MAX_CHARS",
    "INSPECTOR_CONSUMERS",
    "SOURCE_CLAIMED",
    "SOURCE_CLASS",
    "SOURCE_SELF_REPORTED",
    "UI_CONSUMER",
    "bind_current_consumer",
    "claim_consumer",
    "claimed_consumer",
    "current_consumer",
    "current_consumer_source",
    "explicit_consumer",
    "resolve_consumer_and_source",
    "set_current_consumer",
    "subject_class",
]
