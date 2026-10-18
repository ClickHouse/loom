"""Judge the writes the conflict check marked, one LLM call per row, up to
`memory_conflict_resolver_concurrency` at once, and act on the verdicts
(`memory.app.conflict_resolver`). The queue reads, the cost cap, the failure rule and
the backlog drain are in docs/architecture/memory-architecture.md (the conflict
resolver). Leader-only, like every worker.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

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
# A pass whose owed read came back full reads the next batch at once, for up to this
# long (half the pass's deadline); a short read ends it, and the interval follows.
BURST_S = 300.0
# A row judged and left owed (a flagged peer the answer leaves out, an error after the
# call) is paid for again every pass: after this many such attempts it is stamped,
# errored. Counted per (identity, content hash) on this leader.
MAX_ATTEMPTS = 3


@dataclass
class _State:
    """What one leader keeps from pass to pass."""

    # time.monotonic() before which the recheck is not read.
    recheck_after: float = 0.0
    # Why the model was unavailable at the last pass that knew (None: available): the
    # warning is logged when this changes, not on every pass.
    unavailable: str | None = None
    # No key last pass: unlike an outage, its end is known without a call.
    no_key: bool = False
    # Attempts that left a row owed, per (identity, content hash).
    attempts: dict[tuple[str, ...], int] = field(default_factory=dict)


_state = _State()


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
        # The last refusal (its kind), how many rows in a row got it, and `judged` then.
        self.refusal = ""
        self.refusals_in_a_row = 0
        self.judged_at_refusal = 0
        # Rows refused for a reason of their own: stamped once the pass ends, unless the
        # refusals stopped it (then the request, not the rows, was at fault).
        self.refused: list[dict[str, Any]] = []


def _cap_share() -> dict[str, Any]:
    """The resolver's own spend against its share of the namespace's daily cap: the
    rest of the cap stays for the requests (memory.ask, extraction), which read the
    whole spend against the whole cap."""
    share = float(settings.memory_conflict_resolver_cap_share)
    # A read that fails counts as over: a background spender does not pay unread.
    return {"operation": f"worker.{NAME}", "share": share, "fail_open": False}


async def _queued(p: _Pass) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], bool]:
    """The pass's rows, each identity once, grouped by (org, namespace), and whether the
    owed read came back full. A namespace over its cost cap is left out of the read, so
    its rows take no place in the pass."""
    from server.observability.budget import namespaces_over_cap

    recheck = int(settings.memory_conflict_resolver_recheck_batch)
    due = recheck > 0 and time.monotonic() >= _state.recheck_after
    pairs = await queue.queued_namespaces(recheck=due)
    over = await namespaces_over_cap(pairs, **_cap_share()) if pairs else {}
    capped = sorted(pair for pair, is_over in over.items() if is_over)
    p.counts.capped_namespaces = len(capped)
    batch = int(settings.memory_conflict_resolver_batch)
    # A new order of the namespaces within a turn each pass, so none waits behind the rest.
    seed = random.getrandbits(32)
    rows = await queue.owed_rows(limit=batch, capped=capped, seed=seed)
    full = len(rows) >= batch
    if due:
        found = await queue.unjudged_conflicted_rows(limit=recheck, capped=capped, seed=seed)
        if len(found) < recheck:
            _state.recheck_after = time.monotonic() + RECHECK_IDLE_S
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
    from server.observability.events import get_emitter
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
            if await namespace_over_cap(org, namespace, **_cap_share()):
                p.counts.skipped_budget += 1
                return
            if await resolver.resolve_row(r, policy=policy, counts=p.counts) and (
                await _out_of_attempts(r)
            ):
                p.counts.errored += 1
                p.errors.append(f"{namespace}/{r.get('memory_key')}: a flagged peer never judged")
        except (resolver.ModelUnavailable, LeaseUnavailable) as e:
            # The same answer for every row: stop before paying for more calls.
            p.unavailable = p.unavailable or e
        except (resolver.Unjudgeable, resolver.Unanswered) as e:
            # Asking again gets the same answer: stamped at the pass's end, so it holds no
            # place, unless the same refusal for row after row stops the pass.
            p.counts.errored += 1
            p.errors.append(f"{namespace}/{r.get('memory_key')}: {e}")
            p.refused.append(r)
            _count_refusal(p, e)
        except Exception as e:  # noqa: BLE001 — one row's failure never stops the pass
            p.counts.errored += 1
            p.errors.append(f"{namespace}/{r.get('memory_key')}: {type(e).__name__}: {e}")
            if not _transient(e) and await _out_of_attempts(r):
                p.errors.append(f"{namespace}/{r.get('memory_key')}: stamped after {MAX_ATTEMPTS}")
        finally:
            emit_worker_cost_event_for(
                org=org, namespace=namespace, operation=f"worker.{NAME}", started=started
            )
            # The next row's cap read follows at once: the spend must be readable by then.
            await get_emitter().flush_now()


def _transient(e: Exception) -> bool:
    """A provider failure that made no paid call: the row stays owed, uncounted."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        return status in (408, 409, 429) or status >= 500
    return isinstance(e, (httpx.TransportError, TimeoutError))


async def _out_of_attempts(r: Mapping[str, Any]) -> bool:
    """One more attempt that left the row owed; True (and the row stamped) on the last."""
    key = (*_identity(r), str(r.get("content_sha256") or ""))
    attempts = _state.attempts
    if len(attempts) > 10_000:
        attempts.clear()  # leader-local and bounded: a row seen again starts over
    attempts[key] = attempts.get(key, 0) + 1
    if attempts[key] < MAX_ATTEMPTS:
        return False
    try:
        await queue.stamp_judged(r)
    except Exception as e:  # noqa: BLE001 — one row's stamp never stops the pass
        log.warning("%s: stamp after %d attempts failed: %s", NAME, MAX_ATTEMPTS, e)
        return False  # still at the last attempt: the next pass stamps it
    del attempts[key]
    return True


# The same refusal this many rows in a row is the request's fault, not the rows': the
# pass stops and the rest stay owed, instead of being stamped judged unjudged.
SAME_REFUSAL_STOP = 3


def _count_refusal(p: _Pass, e: Exception) -> None:
    # Its kind, not its message: an Unanswered names the cluster's output cap.
    cause = e.__cause__
    status = getattr(getattr(cause, "response", None), "status_code", "")
    kind = f"{type(e).__name__}:{status}"
    if kind != p.refusal or p.counts.judged > p.judged_at_refusal:
        p.refusals_in_a_row = 0
    p.refusal, p.judged_at_refusal = kind, p.counts.judged
    p.refusals_in_a_row += 1
    if p.refusals_in_a_row >= SAME_REFUSAL_STOP:
        p.unavailable = p.unavailable or e


async def _stamp_refused(p: _Pass) -> None:
    """The rows refused for a reason of their own, once the pass ran to its end."""
    if p.unavailable is not None:
        return
    for r in p.refused:
        try:
            await queue.stamp_judged(r)
        except Exception as e:  # noqa: BLE001 — stamped again next pass
            p.errors.append(f"{r['namespace']}/{r.get('memory_key')}: stamp: {e}")


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
    model = settings.conflict_resolver_model
    if reason is not None and _state.unavailable is None:
        log.warning("%s: model %r is unavailable (%s); rows stay owed", NAME, model, reason)
    elif reason is None and _state.unavailable is not None:
        log.info("%s: model %r is available again", NAME, model)
    _state.unavailable = reason


async def run_pass() -> dict[str, Any]:
    set_query_tag(f"worker.{NAME}")
    p = _Pass()
    with record_pass(NAME) as rec:
        missing = resolver.model_missing()
        if missing is None and _state.no_key:
            _note_availability(None)  # the key is back, whether or not a row is queued
        _state.no_key = missing is not None
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
        await _stamp_refused(p)
        await resolver.resolve_done_intents(p.counts)
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
