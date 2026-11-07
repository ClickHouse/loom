"""Buffered usage-event recording for the loom.memory family.

Recall/evidence events fire on every memory.search hit; a synchronous insert
per event would block the read path. Events buffer here and flush periodically
(or on overflow / shutdown), trading a small durability window for read latency.

`record_usage_event` is the public entry point (re-exported from memory_repo).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.core.ch_writes import async_insert_settings
from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters, scope_and

log = logging.getLogger(__name__)

# Usage event kinds. `used` is an AGENT's label — memory.outcome
# outcome="used" or run.complete memories_used; `used_cited` is memory.ask's
# own credit for the keys its synthesized answer cited, kept apart so the
# ranking fold can weigh the two differently and the agent-label gauge can
# leave the self-credit out. `acted` is the server's observation of reliance;
# `harm` is an agent's negative label.
USED_EVENT = "used"
USED_CITED_EVENT = "used_cited"
ACTED_EVENT = "acted"
HARM_EVENT = "harm"
# The kinds an agent (not the server, not memory.ask) wrote: what the
# two-counter worth term may read, and what the daily gauge
# counts. `recall`, `evidence` (also written by extraction and auto-promote),
# `contradicted` and `used_cited` are server-sourced and excluded.
AGENT_LABEL_EVENT_TYPES: tuple[str, ...] = (USED_EVENT, ACTED_EVENT, HARM_EVENT)

# Bounded so an unresponsive CH can't grow memory unbounded; overflow flushes
# (back-pressure, not drop). The flush loop drains on cancellation at shutdown.
_USAGE_BUFFER: list[dict[str, Any]] = []
_USAGE_BUFFER_LOCK: asyncio.Lock | None = None
_USAGE_BUFFER_MAX = 100
_USAGE_FLUSH_INTERVAL_S = 0.5
_USAGE_FLUSH_TASK: asyncio.Task[None] | None = None


async def _flush_usage_buffer_now(ch: _CHClient | None = None, *, background: bool) -> None:
    """Drain the usage-event buffer in one CH insert. No-op when empty.
    `background=False` for the flush a request waits on (the buffer is full)."""
    global _USAGE_BUFFER
    async with _buffer_lock():
        if not _USAGE_BUFFER:
            return
        batch = _USAGE_BUFFER
        _USAGE_BUFFER = []
    ch = ch or _default_client()
    try:
        await ch.insert(
            "loom.memory_usage_events",
            batch,
            # wait=1: a rejected insert raises here and is re-queued, not lost unseen.
            settings=async_insert_settings(background=background),
        )
    except Exception as e:  # noqa: BLE001 — requeue on any flush failure
        # Re-queue on failure, but bounded: drop rather than OOM if CH stays down.
        log.warning(
            "usage-event flush failed (%d events): %s — re-queueing",
            len(batch),
            e,
        )
        async with _buffer_lock():
            if len(_USAGE_BUFFER) + len(batch) <= _USAGE_BUFFER_MAX * 10:
                _USAGE_BUFFER = batch + _USAGE_BUFFER


async def drain() -> None:
    """Shutdown: the buffered labels land before the ClickHouse client goes
    (`boot.drain_writes`); the flush loop's own cancel-path flush runs too late."""
    await _flush_usage_buffer_now(background=True)


# The flush loop runs in a detached (empty) context; the tag names its inserts
# in the query log instead of the `untagged:` frame fallback.
USAGE_FLUSH_QUERY_TAG = "memory_usage.flush"


async def _usage_flush_loop() -> None:
    """Background task: flush the buffer every _USAGE_FLUSH_INTERVAL_S."""
    from server.core.ch import set_query_tag

    set_query_tag(USAGE_FLUSH_QUERY_TAG)
    while True:
        try:
            await asyncio.sleep(_USAGE_FLUSH_INTERVAL_S)
            await _flush_usage_buffer_now(background=True)
        except asyncio.CancelledError:
            # Drain on shutdown so we don't lose events.
            await _flush_usage_buffer_now(background=True)
            raise
        except Exception:  # noqa: BLE001 — one failure must not kill the loop
            # Don't let one transient failure kill the flush loop.
            continue


def _buffer_lock() -> asyncio.Lock:
    """The buffer's lock, created on first use."""
    global _USAGE_BUFFER_LOCK
    if _USAGE_BUFFER_LOCK is None:
        _USAGE_BUFFER_LOCK = asyncio.Lock()
    return _USAGE_BUFFER_LOCK


def _ensure_usage_flusher_started() -> None:
    """Lazily start the background flush task on first use. Called from
    record_usage_event so we don't need a server-startup hook."""
    global _USAGE_FLUSH_TASK
    if _USAGE_FLUSH_TASK is None or _USAGE_FLUSH_TASK.done():
        # Started by whichever request first records usage; it must not
        # carry that request's tag for the rest of the process.
        from server.core.background import detached_task

        _USAGE_FLUSH_TASK = detached_task(_usage_flush_loop(), name="loom-usage-flush")


def _reset_for_tests() -> None:
    """Drop every buffered event and stop the flusher (test seam).

    The flush loop is a detached task on the running loop that inserts
    through `_default_client()` at fire time — in a test suite with a
    session-scoped loop, that is whichever fake client the test running
    0.5 s later has patched in. The buffer is cleared before the task is
    cancelled, so the cancel-path drain finds nothing and touches no
    client; the lock and task are dropped so the next recorder starts a
    fresh flusher.
    """
    global _USAGE_BUFFER, _USAGE_BUFFER_LOCK, _USAGE_FLUSH_TASK
    _USAGE_BUFFER = []
    task = _USAGE_FLUSH_TASK
    _USAGE_FLUSH_TASK = None
    _USAGE_BUFFER_LOCK = None
    if task is not None and not task.done():
        task.cancel()


def _event_consumer(consumer: str | None) -> str:
    """The `consumer` column's value: the caller's, else the request's
    (ContextVar — claim, body field or subject class), else
    '' outside a request."""
    if consumer is not None:
        return str(consumer)
    try:
        from server.observability.consumer import current_consumer

        return str(current_consumer() or "")
    except Exception:  # noqa: BLE001 — telemetry never fails the write
        return ""


async def record_usage_event(
    *,
    memory_id: str,
    org: str,
    namespace: str,
    event_type: str,  # "evidence" | "recall" | "used" | "used_cited" | "harm" | "acted" | ...
    event_count: int = 1,
    agent: str = "",
    consumer: str | None = None,
    ch: _CHClient | None = None,
) -> None:
    """Append a usage event for the AggregatingMergeTree totals. Buffered and
    flushed by the background loop; the call that fills the buffer waits for its
    flush (the short batched window). A failed flush requeues its batch, dropping it
    when the buffer would pass 10x the bound.

    `agent` carries which client triggered the event (for per-agent recall
    breakdown); empty string is valid for paths without a clear agent.
    `consumer` is the product behind the request (on this
    ledger so the credit audit reads (agent, consumer)):
    None takes the request's own,
    '' is a path with no request (a worker, an import).
    """

    _ensure_usage_flusher_started()
    event = {
        "memory_id": memory_id,
        "org": org,
        "namespace": namespace,
        "event_type": event_type,
        "event_count": int(event_count),
        "agent": agent,
        "consumer": _event_consumer(consumer),
        "ts": naive_utc_now(),
    }
    should_flush = False
    async with _buffer_lock():
        _USAGE_BUFFER.append(event)
        if len(_USAGE_BUFFER) >= _USAGE_BUFFER_MAX:
            should_flush = True
    if should_flush:
        # Back-pressure: caller waits for the drain so the buffer can't grow unbounded.
        await _flush_usage_buffer_now(ch=ch, background=False)


async def record_usage_for_keys(
    org: str,
    ns_keys: dict[str, list[str]],
    *,
    event_type: str,
    agent: str = "",
    consumer: str | None = None,
    ch: _CHClient | None = None,
) -> int:
    """Resolve (namespace, memory_key) pairs to live memory_ids and append one
    usage event of `event_type` per resolved row. Returns rows credited.
    `consumer` as in `record_usage_event` (None = the request's).

    One funnel for key-addressed usage credit: the search handler's recall
    auto-touch and memory.ask's cited-key `used` credit resolve keys the same
    way, so a tombstoned or unknown key is silently skipped in both rather
    than raising in one."""
    from server.core.repo_base import _default_client

    ch = ch or _default_client()
    rows = await newest_key_ids(org, ns_keys, ch=ch)
    credited = 0
    for r in rows:
        await record_usage_event(
            memory_id=str(r["memory_id"]),
            org=org,
            namespace=str(r["namespace"]),
            event_type=event_type,
            event_count=1,
            agent=agent,
            consumer=consumer,
            ch=ch,
        )
        credited += 1
    return credited


async def newest_key_ids(
    org: str,
    ns_keys: dict[str, list[str]],
    *,
    scope: ReadFilters = WHOLE_NAMESPACE,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """The newest live row of each (namespace, memory_key) pair: `memory_id`, `namespace`,
    `memory_key`. A key is shared by every session's copy of the same fact (hundreds of
    rows for a generic key); usage on each copy would turn one read into hundreds of
    events, so the newest live row carries it. `scope` keeps the pick inside a narrowed
    reader's rows, the ones it could have been served."""
    pairs = {(ns, k) for ns, keys in ns_keys.items() for k in keys if ns and k}
    if not pairs:
        return []
    ch = ch or _default_client()
    params: dict[str, Any] = {
        "org": org,
        "namespaces": sorted({ns for ns, _k in pairs}),
        "keys": sorted({k for _ns, k in pairs}),
    }
    rows = await ch.query_all(
        """
        SELECT argMax(toString(memory_id), updated_at) AS memory_id, namespace, memory_key
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace IN {namespaces:Array(String)}
          AND memory_key IN {keys:Array(String)}
          AND tombstoned_at IS NULL"""
        + scope_and(scope, params)
        + """
        GROUP BY namespace, memory_key
        """,
        params=params,
    )
    # the same key in another namespace is not what was read
    return [r for r in rows if (str(r["namespace"]), str(r["memory_key"])) in pairs]
