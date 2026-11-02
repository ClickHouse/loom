"""Recall credit only from readers that close the loop.

A `memory.search` with `auto_touch` (the default) writes one `recall` usage
event per served key; the fold turns those into the reinforcement rank term.
Crediting every read would rank what was *shown*, never what was *relied on*:
busy readers write recall credits by the hundred thousand against a handful of
`used` labels and outcome posts, benches write more into the same rows, and the
most-shown rows become the warmest.

The rule, in order (the consumer is checked ahead of the agent sentinels):

1. A bench label never credits: `agent` or `consumer` in {`latency-bench`,
   `f1-drive`} or `eval-*`. Benches should pass `auto_touch:false`; the
   server enforces it for the labels that do not.
2. A read with no NAMED consumer never credits: the consumer column's class
   fallback (`service-account` / `user` / `anonymous`) is not
   a client that can come back and post an outcome. The agent sentinels `*`
   (the SDK's and the MCP transport's cross-agent read: "all authors in my
   namespaces") and `-` (the HTTP "no agent" value) name no reader either, so
   a sentinel read with no consumer is refused as a `measurement_label` —
   the probes and peer benches read that way. With a named consumer the
   sentinel is a scope, not a label: the consumer is the identity that
   closes the loop (the closer window is keyed on it for that reason). The
   dashboard and the assistant toolset (`loom-ui`, `loom-assistant`) are a
   person looking at the namespace, not a reader that relies on what it is
   shown, so their reads are refused as `no_consumer` too.
3. A read that carries a run (its own `run_id`, the transport-bound ledger
   run) or a `session_id` is credited: the outcome can be
   posted against that run.
4. Otherwise the consumer must have posted a closer — `memory.outcome`,
   `memory.outcome_run` or `run.complete` — anywhere in the org within
   `memory_reinforcement_closer_window_days`; a cached read of
   `loom.agent_events` answers it.

Refused reads are still served in full; the response discloses the decision
as `reinforcement: "not_credited (<reason>)"`. Credits are never rewritten:
the 90-day half-life decays them. Every credit written carries the request's
`consumer` on `loom.memory_usage_events`, so the audit reads
(agent, consumer), not the agent alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from server.observability.consumer import (
    CLASS_ANONYMOUS,
    CLASS_SERVICE_ACCOUNT,
    CLASS_USER,
    INSPECTOR_CONSUMERS,
    SOURCE_CLASS,
    current_consumer,
    current_consumer_source,
)

log = logging.getLogger(__name__)

# Labels the benches and probes read under. The bench labels are a measurement wherever they
# stand; the agent sentinels `*` (the SDK's / MCP transport's cross-agent read) and `-`
# (HTTP "no agent") name no reader, so they are a measurement only when no consumer is named
# (a named consumer reading across its agents is credited).
BENCH_LABELS: frozenset[str] = frozenset({"latency-bench", "f1-drive"})
AGENT_SENTINELS: frozenset[str] = frozenset({"*", "-"})
MEASUREMENT_LABELS: frozenset[str] = BENCH_LABELS | AGENT_SENTINELS
MEASUREMENT_LABEL_PREFIXES: tuple[str, ...] = ("eval-",)
# The consumer column's class fallbacks: a subject class, not
# a client.
CLASS_CONSUMERS: frozenset[str] = frozenset(
    {"", CLASS_SERVICE_ACCOUNT, CLASS_USER, CLASS_ANONYMOUS}
)
# The operations that close a loop: an agent's label on what it relied on.
CLOSER_OPERATIONS: tuple[str, ...] = ("memory.outcome", "memory.outcome_run", "run.complete")

CREDITED = "credited"
REASON_MEASUREMENT_LABEL = "measurement_label"
REASON_NO_CONSUMER = "no_consumer"
REASON_NO_CLOSER = "no_closer"


@dataclass(frozen=True)
class CreditDecision:
    credited: bool
    reason: str = ""

    @property
    def disclosure(self) -> str:
        """The response field: `credited` | `not_credited (<reason>)`."""
        return CREDITED if self.credited else f"not_credited ({self.reason})"


def is_measurement_label(label: str | None) -> bool:
    """A label that, standing alone, names no reader that can close a loop:
    a bench label or an agent sentinel. (The per-read question, which lets
    a named consumer stand behind a sentinel, is `is_measurement_read`.)"""
    text = str(label or "").strip()
    return text in MEASUREMENT_LABELS or text.startswith(MEASUREMENT_LABEL_PREFIXES)


def is_bench_label(label: str | None) -> bool:
    """Rule 1: an explicit measurement label — `latency-bench`, `f1-drive`,
    `eval-*` — on the agent or the consumer. Never credited, whoever else is
    named on the read."""
    text = str(label or "").strip()
    return text in BENCH_LABELS or text.startswith(MEASUREMENT_LABEL_PREFIXES)


def is_agent_sentinel(label: str | None) -> bool:
    """`*` / `-`: the read names no agent (a scope, not a
    label, once a consumer is named)."""
    return str(label or "").strip() in AGENT_SENTINELS


def named_consumer(consumer: str | None, source: str | None = None) -> str:
    """The consumer as a client name: '' for the class fallback (or no
    request at all), the claimed / self-reported value otherwise."""
    text = str(consumer or "").strip()
    if source == SOURCE_CLASS or text in CLASS_CONSUMERS:
        return ""
    return text


def current_named_consumer() -> str:
    return named_consumer(current_consumer(), current_consumer_source())


def is_measurement_read(
    *, agent: str | None, consumer: str | None = None, consumer_source: str | None = None
) -> bool:
    """Rules 1 and the sentinel half of 2: a bench label on the agent or the
    consumer, or an agent sentinel (`*`, `-`) with no named consumer behind
    it. A named consumer reading as `*` is not a measurement. The consumer
    (and its source) default to the request's (ContextVar)."""
    if consumer is None:
        c: str | None = current_consumer()
        src: str | None = current_consumer_source()
    else:
        c, src = consumer, consumer_source
    if is_bench_label(agent) or is_bench_label(c):
        return True
    return is_agent_sentinel(agent) and not named_consumer(c, src)


def decide_static(
    *,
    agent: str | None,
    consumer: str | None,
    consumer_source: str | None,
    run_id: str | None,
    session_id: str | None,
) -> CreditDecision | None:
    """Rules 1–3, no I/O. None = the closer window decides (rule 4)."""
    if is_measurement_read(agent=agent, consumer=consumer, consumer_source=consumer_source):
        return CreditDecision(False, REASON_MEASUREMENT_LABEL)
    named = named_consumer(consumer, consumer_source)
    if not named or named in INSPECTOR_CONSUMERS:
        return CreditDecision(False, REASON_NO_CONSUMER)
    if str(run_id or "").strip() not in ("", "-") or str(session_id or "").strip() not in ("", "-"):
        return CreditDecision(True)
    return None


async def decide_recall_credit(
    *,
    org: str,
    agent: str | None,
    consumer: str | None,
    consumer_source: str | None,
    run_id: str | None,
    session_id: str | None,
    ch: Any | None = None,
) -> CreditDecision:
    """The whole rule for one read."""
    static = decide_static(
        agent=agent,
        consumer=consumer,
        consumer_source=consumer_source,
        run_id=run_id,
        session_id=session_id,
    )
    if static is not None:
        return static
    closed = await consumer_closed_loop(org, named_consumer(consumer, consumer_source), ch=ch)
    return CreditDecision(True) if closed else CreditDecision(False, REASON_NO_CLOSER)


# ---------------------------------------------------------------- closer window

# (org, consumer) -> (closed, expires_at monotonic). Positive and negative
# answers are both cached for the same TTL: a consumer that posts its first
# outcome is credited within one TTL (or at once on the worker that saw the
# post, via note_closer).
_CLOSER_CACHE: dict[tuple[str, str], tuple[bool, float]] = {}

_CLOSER_SQL = """
SELECT 1 AS seen
FROM loom.agent_events
WHERE org = {org:String}
  AND consumer = {consumer:String}
  AND operation IN {ops:Array(String)}
  AND success
  AND ts >= now() - INTERVAL {days:UInt32} DAY
LIMIT 1
"""


def _ttl_seconds() -> float:
    from server.settings import settings

    return float(max(0, int(settings.memory_reinforcement_closer_cache_seconds)))


async def consumer_closed_loop(org: str, consumer: str, *, ch: Any | None = None) -> bool:
    """Has `consumer` posted a closer in the org within the window? Cached per
    (org, consumer); a read error answers False and caches nothing."""
    if not consumer:
        return False
    key = (org, consumer)
    now = time.monotonic()
    hit = _CLOSER_CACHE.get(key)
    if hit is not None and hit[1] > now:
        return hit[0]
    from server.settings import settings

    try:
        if ch is None:
            from server.core.ch import get_client

            ch = get_client()
        rows = await ch.query_all(
            _CLOSER_SQL,
            params={
                "org": org,
                "consumer": consumer,
                "ops": list(CLOSER_OPERATIONS),
                "days": int(settings.memory_reinforcement_closer_window_days),
            },
        )
    except Exception as e:  # noqa: BLE001 — a credit is telemetry; never fail the read
        log.warning("recall-credit closer lookup failed (not credited): %s", e)
        return False
    closed = bool(rows)
    _CLOSER_CACHE[key] = (closed, now + _ttl_seconds())
    return closed


def note_closer(org: str, consumer: str | None) -> None:
    """A closer just landed for (org, consumer) on this worker: the next
    read is credited without waiting for the negative cache to expire."""
    c = str(consumer or "").strip()
    if not c:
        return
    _CLOSER_CACHE[(org, c)] = (True, time.monotonic() + _ttl_seconds())


def note_closer_from_request(org: str) -> None:
    """`note_closer` for the request's own named consumer (the outcome / run
    handlers call this after their credits land)."""
    note_closer(org, current_named_consumer())


def _reset_for_tests() -> None:
    _CLOSER_CACHE.clear()


__all__ = [
    "AGENT_SENTINELS",
    "BENCH_LABELS",
    "CLASS_CONSUMERS",
    "CLOSER_OPERATIONS",
    "CREDITED",
    "MEASUREMENT_LABELS",
    "MEASUREMENT_LABEL_PREFIXES",
    "REASON_MEASUREMENT_LABEL",
    "REASON_NO_CLOSER",
    "REASON_NO_CONSUMER",
    "CreditDecision",
    "consumer_closed_loop",
    "current_named_consumer",
    "decide_recall_credit",
    "decide_static",
    "is_agent_sentinel",
    "is_bench_label",
    "is_measurement_label",
    "is_measurement_read",
    "named_consumer",
    "note_closer",
    "note_closer_from_request",
]
