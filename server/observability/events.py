"""Agent-events emitter — batched async writer for `loom.agent_events`.

Every operation emits exactly one row. The transport layer calls
`emit()` after each request (success or failure). Writes are batched:
flush every 100 ms or every 500 rows, whichever comes first.

Failure to write an event must never fail a request. Errors are logged
and dropped; the at-most-once semantics are acceptable for cost
attribution at this scale.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.ch import get_client
from server.core.ch_writes import batched_insert_settings
from server.observability.instance import stamp_row

log = logging.getLogger(__name__)

FLUSH_INTERVAL_S = 0.1
FLUSH_BATCH = 500
SHUTDOWN_DRAIN_S = 5.0


def for_this_cell(event: AgentEvent) -> AgentEvent:
    """A request event for an org a cell does not hold keeps its op, timing and
    outcome but names no tenant: its org, identity and arguments belong to another
    cell, whose erasure never reaches this one (docs/architecture/cells.md)."""
    from server.auth import tenancy

    if event.kind != "request" or tenancy.attributable(event.org):
        return event
    return dataclasses.replace(
        event,
        org="-",
        namespace="-",
        agent="-",
        user_id="-",
        session_id="-",
        memory_key="",
        impersonated_by="",
        run_id="",
        subject="",
        consumer="",
        args={},
    )


@dataclass
class AgentEvent:
    ts: datetime
    org: str
    namespace: str
    agent: str
    user_id: str
    session_id: str
    operation: str
    request_id: str
    latency_ms: int
    success: bool
    trace_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    embedding_tokens: int = 0
    upstream_cost_usd: float = 0.0
    bytes_written: int = 0
    error_code: str = ""
    # Only set on memory.* ops; lets cost.summary group by key.
    memory_key: str = ""
    # JWT subject behind an X-Loom-On-Behalf-Of call; user_id holds the
    # impersonated identity.
    impersonated_by: str = ""
    # The run this call served: the request's own `run_id`
    # argument, bounded; '' when the caller opened no run. Joins agent_runs.
    run_id: str = ""
    # 'request' — one completed call with its outcome, written by a transport;
    # 'worker' — a background pass's cost row.
    # Per-principal counts and policy predicates filter on it
    # so machine traffic never counts as an agent's own calls.
    kind: str = ""
    # Bounded projection of the call's arguments: identifier-class fields
    # verbatim, free text as digests — see observability/event_args.py.
    args: dict[str, str] = field(default_factory=dict)
    # LLM model that carried the op's spend ('' = no LLM call) and how many
    # completions it made (a cascade escalation is 2).
    model: str = ""
    llm_calls: int = 0
    # The client module or product the request spoke for — the
    # request's own `consumer`, else the token's subject class. Its own
    # column; `args` carries the raw request value too.
    consumer: str = ""
    # Size of what the call handed back to the agent, in tokens (len of the
    # serialized response JSON / 4 — the chars/4 estimate the MCP listing
    # budget uses). 0 = unknown (a failed call).
    # The LLM columns above are what the server spent; this
    # is what the agent has to read.
    response_tokens: int = 0
    # The traffic class the request ran under — production |
    # interactive | bench — resolved from the token claim, the namespace row
    # or the request's hint.
    traffic_class: str = ""
    # The verified token subject that made the call
    # (`sa:<client>-<hash>` for a service account, the login subject for a
    # person). `user_id` is the request body's field and `consumer` the
    # subject's class, so neither could answer "when was this token last
    # used".
    subject: str = ""
    # The SLI columns (server/observability/sli.py): `http` | `mcp` | '' for
    # rows no transport wrote, the op's class, `ok` | `client` | `server`, and
    # the latency budget the request was held to (0 is a real budget, which
    # every success exceeds; a row with no registered op has 0 and no class).
    transport: str = ""
    slo_class: str = ""
    status_class: str = ""
    budget_ms: int = 0

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "ts": self.ts,
            "org": self.org,
            "namespace": self.namespace,
            "agent": self.agent,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "operation": self.operation,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "latency_ms": self.latency_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "embedding_tokens": self.embedding_tokens,
            "upstream_cost_usd": self.upstream_cost_usd,
            "bytes_written": self.bytes_written,
            "success": self.success,
            "error_code": self.error_code,
            "memory_key": self.memory_key,
            "impersonated_by": self.impersonated_by,
            "subject": self.subject,
            "kind": self.kind,
            "args": dict(self.args),
            "model": self.model,
            "llm_calls": self.llm_calls,
            "transport": self.transport,
            "slo_class": self.slo_class,
            "status_class": self.status_class,
            "budget_ms": self.budget_ms,
            "response_tokens": self.response_tokens,
            "consumer": self.consumer,
            "traffic_class": self.traffic_class,
            # Which build wrote the row (the /v1/health field);
            # Which process (hostname:pid) and which launcher
            # label. `stamp_row` adds build_commit, instance, instance_label.
        }
        return stamp_row(row)


@dataclass
class EventEmitter:
    """Singleton emitter. Use `get_emitter()` to fetch."""

    queue: asyncio.Queue[AgentEvent] = field(default_factory=lambda: asyncio.Queue(maxsize=10_000))
    _task: asyncio.Task[None] | None = None
    _running: bool = False
    # Batches the timer popped and is still inserting. `flush_now` waits for
    # this to reach 0 so a caller that needs the trace to be history does not
    # see an empty queue and read before the witness row lands. A counter,
    # not an asyncio.Lock: the singleton outlives the event loop of any one
    # test or request and a Lock binds to the loop that first waits on it.
    _inflight: int = 0
    # Events lost because the queue was full, or because their batch's insert
    # failed. Process totals, served by `/v1/health?include=events`.
    dropped_queue_full: int = 0
    dropped_flush_failed: int = 0
    # Refusal rows an MCP batch did not write because it already wrote them.
    # A server fault past its cap is lost from the SLIs like the two above; a
    # caller's repeated refusal is the caller's doing, so it is counted apart.
    dropped_batch_repeat: int = 0
    batch_repeats: int = 0
    # Batches whose insert failed; paces the flush-failure log.
    flush_failures: int = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Process-lifetime task: it must not inherit the tag, request id or
        # auth of whichever request first started it.
        from server.core.background import detached_task

        self._task = detached_task(self._drain_loop(), name="loom-events-drain")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            try:
                await asyncio.wait_for(self._drain_remaining(), timeout=SHUTDOWN_DRAIN_S)
            except TimeoutError:
                log.warning("agent_events drain timed out on shutdown")
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 — stop ignores how it ended
                pass
            self._task = None

    def emit(self, event: AgentEvent) -> None:
        """Fire-and-forget. Never raises; never blocks the request."""
        event = for_this_cell(event)
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_queue_full += 1
            n = self.dropped_queue_full
            # A full queue means every request is dropping: one line per event
            # would be a log storm on top of the backlog.
            if n in (1, 10, 100) or n % 1000 == 0:
                log.warning(
                    "agent_events queue full; %d events dropped so far (latest %s)",
                    n,
                    event.operation,
                )

    def stats(self) -> dict[str, int]:
        return {
            "queued": self.queue.qsize(),
            "dropped_queue_full": self.dropped_queue_full,
            "dropped_flush_failed": self.dropped_flush_failed,
            "dropped_batch_repeat": self.dropped_batch_repeat,
            "batch_repeats": self.batch_repeats,
        }

    async def _drain_loop(self) -> None:
        # Detached (empty) context: the tag names these inserts in the query
        # log instead of the `untagged:` frame fallback.
        from server.core.ch import set_query_tag

        set_query_tag(FLUSH_QUERY_TAG)
        while self._running:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            await self._flush_once()

    async def flush_now(self) -> None:
        """Drain everything queued on THIS replica right now.

        The history-policy gate calls this before it reads the trace, so a
        rule whose witness is the caller's immediately preceding call on this
        replica sees it instead of racing the 100 ms flush timer. It also
        waits for a batch the timer already popped and is still inserting —
        otherwise an empty queue would say "history" while the witness row is
        mid-flight.
        Cross-replica lag remains the flush interval plus the batched-insert
        wait; the ledger shows it as a shadow miss. Never raises: a failed flush
        is logged by `_flush_once`.
        """
        while True:
            if not self.queue.empty():
                await self._flush_once()
            elif self._inflight > 0:
                await asyncio.sleep(0.005)
            else:
                return

    async def _drain_remaining(self) -> None:
        while not self.queue.empty():
            await self._flush_once()

    async def _flush_once(self) -> None:
        batch: list[AgentEvent] = []
        while not self.queue.empty() and len(batch) < FLUSH_BATCH:
            try:
                batch.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not batch:
            return
        self._inflight += 1
        try:
            # With ch_batched_inserts on, every process's flushes share parts; visible on return.
            # The client projects rows onto the live columns: a column the service lacks costs
            # that column, not the batch.
            await get_client().insert(
                "loom.agent_events",
                [e.to_row() for e in batch],
                settings=batched_insert_settings(),
            )
        except Exception as e:  # fail open: a lost batch never fails a request
            self.dropped_flush_failed += len(batch)
            self.flush_failures += 1
            n = self.flush_failures
            # ClickHouse down fails a batch every flush interval: the traceback
            # once, then a line at 10, 100 and every 1000th failed batch.
            if n in (1, 10, 100) or n % 1000 == 0:
                log.warning(
                    "agent_events flush failed (%d events dropped; %d batches, %d events "
                    "lost so far): %s",
                    len(batch),
                    n,
                    self.dropped_flush_failed,
                    e,
                    exc_info=n == 1,
                )
        finally:
            self._inflight -= 1


FLUSH_QUERY_TAG = "agent_events.flush"


_emitter = EventEmitter()


def get_emitter() -> EventEmitter:
    return _emitter
