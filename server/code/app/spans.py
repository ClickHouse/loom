"""The code plane's span helpers: `code.<name>` child spans, bounded
`loom.code.*` attributes on the current span, and the per-request stage wall
that charges each span's exclusive time to a stage. The code search services
and the code.* handlers both emit these spans; they live here so a service
never imports a handler module for them."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

# ---------------------------------------------------------------------------
# Spans. A single root span per op cannot say which leg of a slow call is slow, so
# code_span() wraps the shared traced() helper with a `code.` prefix and the waterfall reads
# code.callers.dispatch / code.search.vector_leg; set_code_span_attributes() stamps bounded
# `loom.code.*` facts on the CURRENT (root) span. Both are no-ops when OTel is off and never
# raise: telemetry can't sink a request.
# ---------------------------------------------------------------------------

# Closed key set: cardinality discipline (metrics.py rule 1) applies to span
# attributes too — no ids, no query text, no paths. Values are coerced to
# bool/int/float or a <=64-char string; anything else is dropped.
CODE_SPAN_KEYS: frozenset[str] = frozenset(
    {
        "route",
        "tier",
        "results",
        "tokens_returned",
        "lag_commits",
        "stale",
        "retrieval_risk",
        "consumer",
        "abstain",
        "abstain_code",
        "ambiguous",
        "dispatch_seeds",
        "not_covered",
        "trace_run_id",
        "verdict",
        "reason",
        "kind",
        "source",
        "depth",
        "edges",
        "truncated",
        "frames",
        "chunks_matched",
        "seeds",
        "hops",
        "top_similarity",
        "page_offset",
        "page_fallback",
        "path_prefix",  # the scope a code.grep ran under
        "fanout",
        "fanout_dropped",  # namespaces fanned out / dropped
    }
)
_MAX_STR = 64


def code_span(name: str, **attrs: Any) -> Any:
    """Child span `code.<name>` of the active request span (see traced()).
    Attributes are bounded the same way as set_code_span_attributes(); a None-valued attr
    is skipped. Yields the span (or None when OTel is off), so callers can add post-hoc
    facts via span_attrs().

    When the op started a stage wall, the span's wall time is also charged to
    `stage_of(name)` on it: the one clock the response and the trace row read, OTel on
    or off."""
    try:
        from server.observability.otel import traced

        clean = {f"loom.code.{k}": v for k, v in _bounded(attrs).items()}
        inner = traced(f"code.{name}", **clean)
    except Exception:  # noqa: BLE001 — telemetry must never fail a request
        inner = contextlib.nullcontext()
    wall = _STAGE_WALL.get()
    if wall is None:
        return inner
    return _timed_span(inner, wall, stage_of(name))


# ---------------------------------------------------------------------------
# Stage wall. Per-leg spans live only in OTel, which a bench replay and the consumer's own
# ledger cannot join, so a slow call needs its per-stage timing in the response and the
# trace row too. The wall is a per-request accumulator the existing code_span() context
# managers report into (no second timing layer), read twice at the end of the op:
# `with_stage_ms(work)` puts `stage_ms_<stage>` whole-millisecond integers into
# retrieval_work and `current_stage_ms()` gives the same numbers as the trace row's Map
# column. Each span is charged its EXCLUSIVE time (children subtracted through the frame
# stack, which is a ContextVar so a leg task sees its parent's frame and a sibling task
# never sees its sibling's). The legs run under asyncio.gather, so stages are concurrent
# and their sum may exceed the request wall: the table says where time went, not that it
# adds up.
# ---------------------------------------------------------------------------


class StageWall:
    """Milliseconds charged per stage name; whole ms on the wire."""

    __slots__ = ("ms",)

    def __init__(self) -> None:
        self.ms: dict[str, float] = {}

    def add(self, stage: str, ms: float) -> None:
        self.ms[stage] = self.ms.get(stage, 0.0) + max(0.0, ms)

    def column(self) -> dict[str, int]:
        """Map(LowCardinality(String), UInt32) shape, stage order fixed."""
        return {
            k[:32]: max(0, min(int(round(v)), 4_294_967_295)) for k, v in sorted(self.ms.items())
        }


_STAGE_WALL: ContextVar[StageWall | None] = ContextVar("loom_code_stage_wall", default=None)
# Open code_span frames of THIS task's context: each frame accumulates the
# inclusive time of its finished children so the parent charges only its own.
_STAGE_FRAMES: ContextVar[tuple[list[float], ...]] = ContextVar(
    "loom_code_stage_frames", default=()
)

# Span suffix -> stage name. Unlisted suffixes are their own stage
# (`bodies`, `rerank`, `risk_annotate`, `memo_probe`, `resolve`, `chunks`, …).
_STAGE_ALIASES: dict[str, str] = {
    "lexical_leg": "lexical",
    "vector_leg": "vector",
    "fuse": "fusion",
    "history_leg": "history",
    "history_route": "history",
    "symbol_leg": "symbol",
    "fan_in_hubs": "fan_in",
    "expand_top_to_file": "bodies",
    "page_bodies": "bodies",
}


def stage_of(span_name: str) -> str:
    """`search.lexical_leg` -> `lexical`; `symbol.resolve` -> `resolve`."""
    suffix = span_name.split(".", 1)[1] if "." in span_name else span_name
    return _STAGE_ALIASES.get(suffix, suffix)


def start_stage_wall() -> StageWall:
    """Begin the op's wall: every code_span() in this task's context (and in
    the tasks it spawns) reports into it until the next start. Called once
    at the top of a code read handler, after its own `started` clock."""
    wall = StageWall()
    _STAGE_WALL.set(wall)
    _STAGE_FRAMES.set(())
    return wall


def current_stage_ms() -> dict[str, int]:
    """The active wall as the trace row's Map column; {} when no wall."""
    wall = _STAGE_WALL.get()
    return wall.column() if wall is not None else {}


def with_stage_ms(work: dict[str, Any]) -> dict[str, Any]:
    """Put `stage_ms_<stage>` integers into a retrieval_work dict (in place,
    returned for call-site brevity). Nothing else changes: a payload without an active
    wall is left byte-identical."""
    for k, v in current_stage_ms().items():
        work[f"stage_ms_{k}"] = v
    return work


@contextlib.contextmanager
def _timed_span(inner: Any, wall: StageWall, stage: str) -> Iterator[Any]:
    frame = [0.0]
    token = _STAGE_FRAMES.set((*_STAGE_FRAMES.get(), frame))
    t0 = time.perf_counter()
    try:
        with inner as span:
            yield span
    finally:
        elapsed = (time.perf_counter() - t0) * 1000.0
        _STAGE_FRAMES.reset(token)
        wall.add(stage, elapsed - frame[0])
        parents = _STAGE_FRAMES.get()
        if parents:
            parents[-1][0] += elapsed


def span_attrs(span: Any, **attrs: Any) -> None:
    """Set bounded `loom.code.*` attributes on a span yielded by code_span()
    (no-op for None / non-recording spans). Never raises."""
    try:
        if span is None or not span.is_recording():
            return
        for k, v in _bounded(attrs).items():
            span.set_attribute(f"loom.code.{k}", v)
    except Exception:  # noqa: BLE001 — telemetry never fails an op
        pass


def set_code_span_attributes(**facts: Any) -> None:
    """Stamp response facts on the CURRENT span as `loom.code.<key>`.

    Keys outside CODE_SPAN_KEYS are dropped (closed set keeps the attribute
    space bounded); values are coerced to bool / int / float / str[:64].
    Called at the end of every code read handler so route, tier, result
    count, tokens, freshness and abstention are queryable per trace in
    HyperDX/Langfuse. Never raises; no-op when no span is recording."""
    try:
        from opentelemetry import trace as _trace

        span = _trace.get_current_span()
        if span is None or not span.is_recording():
            return
        for k, v in _bounded(facts).items():
            span.set_attribute(f"loom.code.{k}", v)
    except Exception:  # noqa: BLE001 — telemetry never fails an op
        pass


def _bounded(facts: dict[str, Any]) -> dict[str, bool | int | float | str]:
    out: dict[str, bool | int | float | str] = {}
    for k, v in facts.items():
        if k not in CODE_SPAN_KEYS or v is None:
            continue
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, int):
            out[k] = int(v)
        elif isinstance(v, float):
            out[k] = v if v == v and v not in (float("inf"), float("-inf")) else 0.0
        elif isinstance(v, str):
            out[k] = v[:_MAX_STR]
        elif isinstance(v, (list, tuple, set, frozenset, dict)):
            out[k] = len(v)  # a collection's size is the bounded fact
        # anything else (objects, bytes) is dropped rather than stringified
    return out


def current_otel_ids() -> tuple[str, str]:
    """(trace_id as 32-hex, span_id as 16-hex) of the current recording span;
    ("", "") when OTel is off, the span is not recording or the context is
    invalid. This is what links loom.code_search_trace to the OTel/Langfuse
    trace from either side."""
    try:
        from opentelemetry import trace as _trace

        span = _trace.get_current_span()
        if span is None or not span.is_recording():
            return "", ""
        ctx = span.get_span_context()
        if ctx is None or not ctx.is_valid:
            return "", ""
        return format_otel_ids(ctx.trace_id, ctx.span_id)
    except Exception:  # noqa: BLE001 — telemetry never fails an op
        return "", ""


def format_otel_ids(trace_id: int, span_id: int) -> tuple[str, str]:
    """W3C hex encoding: trace_id 32 lowercase hex chars, span_id 16. Zero
    ids are invalid per the spec and render as empty strings."""
    t = format(int(trace_id), "032x") if trace_id else ""
    s = format(int(span_id), "016x") if span_id else ""
    return t, s
