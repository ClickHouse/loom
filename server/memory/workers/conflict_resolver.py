"""Judge the writes the conflict check marked, one LLM call per row, up to
`memory_conflict_resolver_concurrency` at once, and act on the verdicts
(`memory.app.conflict_resolver`). The queue reads, the cost cap, the failure rule and
the backlog drain are in docs/architecture/memory-architecture.md (the conflict
resolver). Leader-only, like every worker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from server.core.ch import set_query_tag
from server.core.leases import LeaseUnavailable
from server.memory.app import conflict_resolver as resolver
from server.memory.store import conflict_resolver as queue
from server.settings import settings
from server.workers import pass_context
from server.workers.status import log_pass_errors, record_pass

log = logging.getLogger(__name__)

NAME = "conflict_resolver"

# The recheck's read scans every `conflicted` row (its predicate prunes nothing). A
# backlog drains a batch per pass; once a read comes back short, the next waits this long.
RECHECK_IDLE_S = 300.0
_recheck_after = 0.0  # time.monotonic() before which the recheck is not read
# A pass whose owed read came back full reads the next batch at once, for up to this
# long (half the pass's deadline); a short read ends it, and the interval follows.
BURST_S = 300.0
# Why the model was unavailable at the last pass that knew (None: available). The
# warning is logged when this changes, not on every pass.
_unavailable: str | None = None
# The last pass found no key: unlike an outage, its end is known without a call.
_no_key = False


def _identity(r: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(r[c]) for c in ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
    )


class _Pass:
    """What the rows judged at the same time share."""

    def __init__(self) -> None:
        self.counts = resolver.Counts()
        self.errors: list[str] = []
        self.slots = asyncio.Semaphore(int(settings.memory_conflict_resolver_concurrency))
        # Why the pass stopped: the model, or the keys' locks, is unavailable.
        self.unavailable: Exception | None = None


async def _queued(p: _Pass) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], bool]:
    """The pass's rows, each identity once, grouped by (org, namespace), and whether the
    owed read came back full. A namespace over its cost cap is left out of the read, so
    its rows take no place in the pass."""
    from server.observability.budget import namespaces_over_cap

    global _recheck_after
    recheck = int(settings.memory_conflict_resolver_recheck_batch)
    due = recheck > 0 and time.monotonic() >= _recheck_after
    pairs = await queue.queued_namespaces(recheck=due)
    over = await namespaces_over_cap(pairs) if pairs else {}
    capped = sorted(pair for pair, is_over in over.items() if is_over)
    p.counts.capped_namespaces = len(capped)
    batch = int(settings.memory_conflict_resolver_batch)
    rows = await queue.owed_rows(limit=batch, capped=capped)
    full = len(rows) >= batch
    if due:
        found = await queue.unjudged_conflicted_rows(limit=recheck, capped=capped)
        if len(found) < recheck:
            _recheck_after = time.monotonic() + RECHECK_IDLE_S
        rows += found
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen: set[tuple[str, ...]] = set()
    for r in rows:
        if _identity(r) not in seen:
            seen.add(_identity(r))
            groups.setdefault((str(r["org"]), str(r["namespace"])), []).append(dict(r))
    return groups, full


async def _resolve_one(p: _Pass, r: dict[str, Any], policy: str) -> None:
    from server.observability import cost_accumulator
    from server.observability.budget import namespace_over_cap
    from server.workers.cost_event import emit_worker_cost_event_for

    org, namespace = str(r["org"]), str(r["namespace"])
    async with p.slots:
        if p.unavailable is not None:
            return  # the model or the locks are gone: the row stays owed
        started = time.monotonic()
        # Each row's call bills its own namespace, which the cost cap reads. This runs
        # in its own task, so the accumulator is this row's alone.
        cost_accumulator.begin_op()
        try:
            # Read before every call: the rows in flight at once can cross the cap together.
            if await namespace_over_cap(org, namespace):
                p.counts.skipped_budget += 1
                return
            await resolver.resolve_row(r, policy=policy, counts=p.counts)
        except (resolver.ModelUnavailable, LeaseUnavailable) as e:
            # The same answer for every row: stop before paying for more calls.
            p.unavailable = p.unavailable or e
        except (resolver.Unjudgeable, resolver.Unanswered) as e:
            # Asking again gets the same answer: stamp it, so it holds no place.
            p.counts.errored += 1
            p.errors.append(f"{namespace}/{r.get('memory_key')}: {e}")
            try:
                await queue.stamp_judged(r)
            except Exception as stamp_error:  # noqa: BLE001 — stamped again next pass
                p.errors.append(f"{namespace}/{r.get('memory_key')}: stamp: {stamp_error}")
        except Exception as e:  # noqa: BLE001 — one row's failure never stops the pass
            p.counts.errored += 1
            p.errors.append(f"{namespace}/{r.get('memory_key')}: {type(e).__name__}: {e}")
        finally:
            emit_worker_cost_event_for(
                org=org, namespace=namespace, operation=f"worker.{NAME}", started=started
            )


async def _resolve_all(p: _Pass, groups: dict[tuple[str, str], list[dict[str, Any]]]) -> None:
    """Every row of the pass, `memory_conflict_resolver_concurrency` at a time."""
    from server.core.ch import get_client
    from server.memory.app.conflicts import namespace_conflict_policy

    jobs = []
    for (org, namespace), rows in groups.items():
        policy = await namespace_conflict_policy(org, namespace, ch=get_client())
        jobs += [_resolve_one(p, r, policy) for r in rows]
    # A task group: whatever escapes a row cancels the others and waits for them, so
    # the pass never ends while a row still writes.
    async with asyncio.TaskGroup() as group:
        for job in jobs:
            group.create_task(job)


def _note_availability(reason: str | None) -> None:
    """Log the model going away or coming back, once per change."""
    global _unavailable
    model = settings.conflict_resolver_model
    if reason is not None and _unavailable is None:
        log.warning("%s: model %r is unavailable (%s); rows stay owed", NAME, model, reason)
    elif reason is None and _unavailable is not None:
        log.info("%s: model %r is available again", NAME, model)
    _unavailable = reason


async def run_pass() -> dict[str, Any]:
    global _no_key
    set_query_tag(f"worker.{NAME}")
    p = _Pass()
    with record_pass(NAME) as rec:
        missing = resolver.model_missing()
        if missing is None and _no_key:
            _note_availability(None)  # the key is back, whether or not a row is queued
        _no_key = missing is not None
        if missing is not None:
            # Known before any read: the queue is not read for rows no call can judge.
            _note_availability(missing)
            pass_context.mark_idle()
            idle: dict[str, Any] = p.counts.as_dict()
            rec.outcome = idle
            return idle
        started = time.monotonic()
        found = False  # any batch of the pass held rows
        while True:
            groups, full = await _queued(p)
            found = found or bool(groups)
            judged, errored = p.counts.judged, p.counts.errored
            await _resolve_all(p, groups)
            # A full batch means a backlog: take the next one now, not after the interval,
            # while batches judge cleanly and the pass is well inside its deadline.
            if not (
                full
                and p.unavailable is None
                and p.counts.judged > judged
                and p.counts.errored == errored
                and time.monotonic() - started < BURST_S
            ):
                break
        if not found:
            pass_context.mark_idle()
        if isinstance(p.unavailable, LeaseUnavailable):
            p.errors.append(f"stopped: {p.unavailable}")
        elif p.unavailable is not None:
            _note_availability(str(p.unavailable))
        elif p.counts.judged:
            _note_availability(None)
        outcome: dict[str, Any] = p.counts.as_dict()
        rec.outcome = outcome
        if found:
            log.info("%s: %s", NAME, " ".join(f"{k}={v}" for k, v in outcome.items()))
        log_pass_errors(log, NAME, p.errors)
    return outcome


__all__ = ["NAME", "run_pass"]
