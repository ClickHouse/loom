"""The traffic class a request runs under.

Loom serves production consumers, interactive sessions and evaluation
harnesses from one wire. The harnesses can be most of the LLM spend, most
`RATE_LIMITED` rows and the concurrent load that stretches production
`memory.search` latency, so every lever — rate buckets, ClickHouse per-query
settings, LLM tier eligibility, the cost ledger — needs to tell them apart.

`traffic_class ∈ {production, interactive, bench}` is resolved once per
request, in this order, and published on a request-scoped ContextVar:

    1. the verified token's `traffic_class` claim (the operator's override);
    2. the namespace's registry row (every live namespace has one): a
       `traffic_class:<value>` tag, else `kind = 'bench'` → bench, any other registered kind
       → production;
    3. the request's own hint — a `traffic_class` field, a
       `traffic_class:<value>` tag, a bench-prefixed namespace (the class the
       first-write hook would register it as), or a `consumer` that names a
       bench;
    4. `interactive`.

The class never changes what a request returns: the levers keyed on it
(server/core/ratelimit.py, server/core/ch.py::statement_settings, the ask handler's
tier policy) bound how much of the shared machine a class may claim, not
which rows it reads. Precision first: served results are identical across
classes by construction and the measurement in the record checks it.
"""

from __future__ import annotations

from contextvars import Token
from typing import Any

from server.core.traffic_context import traffic_class_var as _current

PRODUCTION = "production"
INTERACTIVE = "interactive"
BENCH = "bench"
# The class a worker pass or an operator CLI runs
# under. Not a request class — nothing resolves to it; a process binds it.
BACKGROUND = "background"
TRAFFIC_CLASSES: tuple[str, ...] = (PRODUCTION, INTERACTIVE, BENCH, BACKGROUND)
DEFAULT_CLASS = INTERACTIVE

TAG_PREFIX = "traffic_class:"

# Sources, for the response header and the ledger's `args`: which layer named
# the class.
SOURCE_CLAIM = "claim"
SOURCE_NAMESPACE = "namespace"
SOURCE_HINT = "hint"
SOURCE_DEFAULT = "default"


def normalize_class(raw: Any) -> str:
    """A member of TRAFFIC_CLASSES, lower-cased and stripped; '' otherwise."""
    if raw is None or isinstance(raw, bool) or not isinstance(raw, str):
        return ""
    value = raw.strip().lower()
    return value if value in TRAFFIC_CLASSES else ""


def class_from_tags(tags: Any) -> str:
    """The class a `traffic_class:<value>` tag names; '' when none does.
    The last valid tag wins, so a re-registration that appends one supersedes."""
    if not isinstance(tags, (list, tuple)):
        return ""
    found = ""
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(TAG_PREFIX):
            value = normalize_class(tag[len(TAG_PREFIX) :])
            if value:
                found = value
    return found


def class_from_registry_row(row: dict[str, Any] | None) -> str:
    """The class a `loom.namespaces` row implies: its `traffic_class:` tag,
    else `kind = 'bench'` → bench, any other kind → production (a registered
    owner row is somebody's production namespace unless it says otherwise).
    '' for no row."""
    if not row:
        return ""
    tagged = class_from_tags(row.get("tags"))
    if tagged:
        return tagged
    kind = str(row.get("kind") or "").strip().lower()
    if kind == BENCH:
        return BENCH
    return PRODUCTION


def request_hint(raw_args: dict[str, Any] | None) -> str:
    """What the request itself says: a `traffic_class` field, a
    `traffic_class:<value>` tag, or a `consumer` that names a bench
    (`bench` as a word or prefix — `bench`, `membench`, `bench-shunt`,
    `kcurve-bench`). '' when nothing does."""
    if not raw_args:
        return ""
    explicit = normalize_class(raw_args.get("traffic_class"))
    if explicit:
        return explicit
    tagged = class_from_tags(raw_args.get("tags"))
    if tagged:
        return tagged
    consumer = raw_args.get("consumer")
    if isinstance(consumer, str) and "bench" in consumer.strip().lower():
        return BENCH
    return ""


def resolve_traffic_class(
    *,
    claim: str | None = None,
    registry_class: str | None = None,
    hint: str | None = None,
) -> tuple[str, str]:
    """(class, source): the claim, else the registry row's class, else the
    hint, else `interactive`."""
    c = normalize_class(claim)
    if c:
        return c, SOURCE_CLAIM
    r = normalize_class(registry_class)
    if r:
        return r, SOURCE_NAMESPACE
    h = normalize_class(hint)
    if h:
        return h, SOURCE_HINT
    return DEFAULT_CLASS, SOURCE_DEFAULT


def set_current_traffic_class(value: str, source: str = "") -> None:
    _current.set((normalize_class(value), source or ""))


def bind_background(source: str) -> Token[tuple[str, str]]:
    """Run the rest of this task as `background` —
    every read statement it issues carries the class's ClickHouse settings
    (lower `priority`, a `max_threads` cap, cache-bypass reads), so a heal or
    a backfill never crowds an interactive search off the shared replicas.
    `source` names who bound it (`worker:<name>`, `cli:<command>`). Returns
    the token `reset_traffic_class` takes back — a caller that is not its own
    task (a CLI, a test) undoes the binding when its work is done."""
    return _current.set((BACKGROUND, source or ""))


def reset_traffic_class(token: Token[tuple[str, str]]) -> None:
    _current.reset(token)


def current_traffic_class() -> str:
    """The request's class, or '' outside a request (a worker pass, a
    process-lifetime task). Callers that need a class treat '' as
    unclassified — the base bucket, no per-class settings, no tier policy."""
    return _current.get()[0]


def current_traffic_class_source() -> str:
    return _current.get()[1]


def header_value() -> str:
    """`<class>;source=<source>` for the `X-Loom-Traffic-Class` response header."""
    cls, source = _current.get()
    if not cls:
        return ""
    return f"{cls};source={source}" if source else cls


__all__ = [
    "BACKGROUND",
    "BENCH",
    "DEFAULT_CLASS",
    "INTERACTIVE",
    "PRODUCTION",
    "SOURCE_CLAIM",
    "SOURCE_DEFAULT",
    "SOURCE_HINT",
    "SOURCE_NAMESPACE",
    "TAG_PREFIX",
    "TRAFFIC_CLASSES",
    "class_from_registry_row",
    "class_from_tags",
    "current_traffic_class",
    "current_traffic_class_source",
    "header_value",
    "normalize_class",
    "request_hint",
    "resolve_traffic_class",
    "bind_background",
    "reset_traffic_class",
    "set_current_traffic_class",
]
