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
# A pass stopped by the model, the keys' locks or a provider outage: the next passes
# would pay for the same answer, so the worker waits this long first. A namespace whose
# rows got the same refusal row after row waits as long, alone.
PAUSE_AFTER_STOP_S = 300.0


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
    # Attempts that left a row owed, per (identity, content hash): how many, and the
    # peers they judged it against.
    attempts: dict[tuple[str, ...], tuple[int, frozenset[str]]] = field(default_factory=dict)
    # time.monotonic() before which no pass runs, after a pass that stopped.
    paused_until: float = 0.0
    # (org, namespace) -> time.monotonic() before which its rows are not read, after the
    # same refusal for row after row of it.
    paused_namespaces: dict[tuple[str, str], float] = field(default_factory=dict)


_state = _State()


def _identity(r: Mapping[str, Any]) -> tuple[str, ...]:
    return (str(r["org"]), str(r["namespace"]), *queue.key_identity(r))


class _Pass:
    """What the rows judged at the same time share."""

    def __init__(self) -> None:
        self.counts = resolver.Counts()
        self.errors: list[str] = []
        self.slots = asyncio.Semaphore(int(settings.memory_conflict_resolver_concurrency))
        # Why the pass stopped: the model, the keys' locks or the provider is unavailable.
        self.unavailable: Exception | None = None
        # Per (org, namespace) for refusals, and once for outages: the last kind and how
        # many rows in a row got it, with no answer between.
        self.streaks: dict[Any, tuple[str, int]] = {}
        # Namespaces whose refusals stopped them in this pass.
        self.stopped: set[tuple[str, str]] = set()
        # Rows refused for a reason of their own: stamped once the pass ends, unless the
        # refusals stopped their namespace (then the request, not the rows, was at fault).
        self.refused: list[dict[str, Any]] = []
        # The identities the pass took, judged once a pass whatever the burst reads again.
        self.tried: set[tuple[str, ...]] = set()
        # The follow intents its retirements settled, resolved at its end.
        self.done_intents: resolver.Intents = {}


def _cap_share() -> dict[str, Any]:
    """The resolver's own spend against its share of the namespace's daily cap: the
    rest of the cap stays for the requests (memory.ask, extraction), which read the
    whole spend against the whole cap. It stops at the whole cap too."""
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
    max_age = int(settings.memory_conflict_resolver_max_owed_age_days)
    pairs = await queue.queued_namespaces(recheck=due, max_age_days=max_age)
    over = await namespaces_over_cap(pairs, **_cap_share()) if pairs else {}
    capped = sorted(pair for pair, is_over in over.items() if is_over)
    p.counts.capped_namespaces = len(capped)
    now = time.monotonic()
    paused = {ns for ns, until in _state.paused_namespaces.items() if until > now}
    _state.paused_namespaces = {ns: _state.paused_namespaces[ns] for ns in paused}
    capped = sorted(set(capped) | paused)
    batch = int(settings.memory_conflict_resolver_batch)
    # A new order of the namespaces within a turn each pass, so none waits behind the rest.
    seed = random.getrandbits(32)
    rows = await queue.owed_rows(limit=batch, capped=capped, seed=seed, max_age_days=max_age)
    full = len(rows) >= batch
    if due:
        found = await queue.unjudged_conflicted_rows(limit=recheck, capped=capped, seed=seed)
        if len(found) < recheck:
            _state.recheck_after = time.monotonic() + RECHECK_IDLE_S
        rows += found
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        # Each identity once a pass: a row left owed comes back in the burst's next read.
        if _identity(r) not in p.tried:
            p.tried.add(_identity(r))
            groups.setdefault((str(r["org"]), str(r["namespace"])), []).append(dict(r))
    return groups, full


async def _resolve_one(p: _Pass, r: dict[str, Any], policy: str) -> None:
    from server.observability import cost_accumulator
    from server.observability.budget import namespace_over_cap
    from server.observability.events import get_emitter
    from server.workers.cost_event import emit_worker_cost_event_for

    org, namespace = str(r["org"]), str(r["namespace"])
    async with p.slots:
        if p.unavailable is not None or (org, namespace) in p.stopped:
            return  # the model, the locks or this namespace's requests fail: the row stays owed
        started = time.monotonic()
        # Each row's call bills its own namespace, which the cost cap reads. This runs
        # in its own task, so the accumulator is this row's alone.
        cost_accumulator.begin_op()
        try:
            # Read before every call: the rows in flight at once can cross the cap together.
            # The calls finished so far are flushed first, so the spend read sees them.
            await get_emitter().flush_now()
            if await namespace_over_cap(org, namespace, **_cap_share()):
                p.counts.skipped_budget += 1
                return
            key = _attempt_key(r)
            shown = _state.attempts.get(key, (0, frozenset()))[1]
            answered = p.counts.judged
            judged = await resolver.resolve_row(
                r, policy=policy, counts=p.counts, shown=shown, intents=p.done_intents
            )
            if p.counts.judged > answered:
                # An answer: the namespace's refusals and the outages are not in a row.
                p.streaks.pop((org, namespace), None)
                p.streaks.pop("outage", None)
            if judged is not None and await _out_of_attempts(r, judged):
                p.counts.errored += 1
                p.errors.append(f"{namespace}/{r.get('memory_key')}: a flagged peer never judged")
        except (resolver.ModelUnavailable, LeaseUnavailable) as e:
            # The same answer for every row: stop before paying for more calls.
            p.unavailable = p.unavailable or e
        except (resolver.Unjudgeable, resolver.Unanswered) as e:
            # Asking again gets the same answer: stamped at the pass's end, so it holds no
            # place, unless the same refusal for row after row stops its namespace.
            p.counts.errored += 1
            p.errors.append(f"{namespace}/{r.get('memory_key')}: {e}")
            p.refused.append(r)
            if _streak(p, (org, namespace), e):
                p.stopped.add((org, namespace))
                _state.paused_namespaces[(org, namespace)] = time.monotonic() + PAUSE_AFTER_STOP_S
            # An answer with nothing usable was paid for: it counts an attempt, so a row
            # that never gets one ends; a refused request (a 4xx) was not, and does not.
            if isinstance(e, resolver.Unanswered) and await _out_of_attempts(r):
                p.errors.append(f"{namespace}/{r.get('memory_key')}: stamped after {MAX_ATTEMPTS}")
        except (httpx.HTTPError, TimeoutError) as e:
            # A provider outage (5xx, 429, a timeout, no connection): the row stays owed,
            # no attempt; the same outage row after row stops the pass.
            p.counts.errored += 1
            p.errors.append(f"{namespace}/{r.get('memory_key')}: {type(e).__name__}: {e}")
            if _streak(p, "outage", e):
                p.unavailable = p.unavailable or e
        except Exception as e:  # noqa: BLE001 — one row's failure never stops the pass
            p.counts.errored += 1
            p.errors.append(f"{namespace}/{r.get('memory_key')}: {type(e).__name__}: {e}")
            # Counted only once the judge answered: an error before it paid for nothing.
            if isinstance(e, resolver.JudgedThenFailed) and await _out_of_attempts(r):
                p.errors.append(f"{namespace}/{r.get('memory_key')}: stamped after {MAX_ATTEMPTS}")
        finally:
            emit_worker_cost_event_for(
                org=org, namespace=namespace, operation=f"worker.{NAME}", started=started
            )


def _attempt_key(r: Mapping[str, Any]) -> tuple[str, ...]:
    return (*_identity(r), str(r.get("content_sha256") or ""))


async def _out_of_attempts(r: Mapping[str, Any], judged: frozenset[str] = frozenset()) -> bool:
    """One more attempt that left the row owed (`judged`: the peers it judged, kept with
    those judged before); True, and the row stamped, on the last."""
    key = _attempt_key(r)
    attempts = _state.attempts
    if len(attempts) > 10_000:
        attempts.clear()  # leader-local and bounded: a row seen again starts over
    count, before = attempts.get(key, (0, frozenset()))
    count += 1
    attempts[key] = (count, before | judged)
    if count < MAX_ATTEMPTS:
        return False
    try:
        await queue.stamp_judged(r)
    except Exception as e:  # noqa: BLE001 — one row's stamp never stops the pass
        log.warning("%s: stamp after %d attempts failed: %s", NAME, MAX_ATTEMPTS, e)
        return False  # still at the last attempt: the next pass stamps it
    del attempts[key]
    return True


# The same refusal (or outage) this many rows in a row is the request's fault, not the
# rows': the rest stay owed, instead of being stamped judged unjudged.
SAME_REFUSAL_STOP = 3


def _streak(p: _Pass, where: Any, e: Exception) -> bool:
    """Count `e` in its streak (`where`: a namespace, or the provider); True once the
    same kind came SAME_REFUSAL_STOP rows in a row with no answer between."""
    # Its kind, not its message: an Unanswered names the cluster's output cap.
    response = getattr(e, "response", None) or getattr(e.__cause__, "response", None)
    kind = f"{type(e).__name__}:{getattr(response, 'status_code', '')}"
    last, count = p.streaks.get(where, ("", 0))
    count = count + 1 if kind == last else 1
    p.streaks[where] = (kind, count)
    return count >= SAME_REFUSAL_STOP


async def _stamp_refused(p: _Pass) -> None:
    """The rows refused for a reason of their own, once the pass ran to its end and their
    namespace was not stopped."""
    if p.unavailable is not None:
        return
    for r in p.refused:
        if (str(r["org"]), str(r["namespace"])) in p.stopped:
            continue
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
        if time.monotonic() < _state.paused_until:
            # The last pass stopped: the same answer would be paid for again.
            pass_context.mark_idle()
            paused: dict[str, Any] = p.counts.as_dict()
            rec.outcome = paused
            return paused
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
        await resolver.resolve_done_intents(p.done_intents)
        if p.unavailable is not None:
            _state.paused_until = time.monotonic() + PAUSE_AFTER_STOP_S
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
