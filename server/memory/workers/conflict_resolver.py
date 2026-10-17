"""Judge the writes the conflict check marked, one LLM call per row, and act on the
verdicts (`memory.app.conflict_resolver`). Up to `memory_conflict_resolver_concurrency`
rows are judged at once.

Each pass reads up to `memory_conflict_resolver_batch` marked rows, then up to
`memory_conflict_resolver_recheck_batch` rows already `conflicted` that no resolver
judged (flags from the cosine check, or from an older build's judge), so a false flag
is cleared once. Both take rows in turns across namespaces, so one namespace's
backlog does not hold the others back. The queue leaves out a namespace over its daily
LLM cost cap and an org with an erasure queued, running or due a retry; their rows
stay owed. The cap is read again before each call, for a namespace that crosses it
mid-pass. A transient provider error leaves the row owed; a refusal of the row's own
request stamps it judged and counts it errored, so it does not come back every pass. A row
is stamped judged only after its actions, so a pass cut short judges it again and
acting twice changes nothing. If the provider refuses the model or the key, the pass
logs one warning and stops; nothing else fails.
Leader-only, like every worker.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from server.core.ch import set_query_tag
from server.memory.app import conflict_resolver as resolver
from server.memory.store import conflict_resolver as queue
from server.settings import settings
from server.workers import pass_context
from server.workers.status import log_pass_errors, record_pass

log = logging.getLogger(__name__)

NAME = "conflict_resolver"


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
        self.acting = asyncio.Lock()
        self.unavailable: resolver.ModelUnavailable | None = None


async def _queued(p: _Pass) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """The pass's rows, each identity once, grouped by (org, namespace). A namespace
    over its cost cap is left out of the read, so its rows take no place in the pass."""
    from server.observability.budget import namespaces_over_cap

    pairs = await queue.queued_namespaces()
    over = await namespaces_over_cap(pairs) if pairs else {}
    capped = sorted(pair for pair, is_over in over.items() if is_over)
    p.counts.capped_namespaces = len(capped)
    rows = await queue.owed_rows(limit=int(settings.memory_conflict_resolver_batch), capped=capped)
    recheck = int(settings.memory_conflict_resolver_recheck_batch)
    if recheck > 0:
        rows += await queue.unjudged_conflicted_rows(limit=recheck, capped=capped)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen: set[tuple[str, ...]] = set()
    for r in rows:
        if _identity(r) not in seen:
            seen.add(_identity(r))
            groups.setdefault((str(r["org"]), str(r["namespace"])), []).append(dict(r))
    return groups


async def _resolve_one(p: _Pass, r: dict[str, Any], policy: str) -> None:
    from server.observability import cost_accumulator
    from server.observability.budget import namespace_over_cap
    from server.workers.cost_event import emit_worker_cost_event_for

    org, namespace = str(r["org"]), str(r["namespace"])
    async with p.slots:
        if p.unavailable is not None:
            return  # the model is gone: the row stays owed
        # Read before every call: the rows in flight at once can cross the cap together.
        if await namespace_over_cap(org, namespace):
            p.counts.skipped_budget += 1
            return
        started = time.monotonic()
        # Each row's call bills its own namespace, which the cost cap reads. This runs
        # in its own task, so the accumulator is this row's alone.
        cost_accumulator.begin_op()
        try:
            await resolver.resolve_row(r, policy=policy, counts=p.counts, acting=p.acting)
        except resolver.ModelUnavailable as e:
            p.unavailable = p.unavailable or e
        except resolver.Unjudgeable as e:
            # Asking again gets the same refusal: stamp it, so it holds no place.
            p.counts.errored += 1
            p.errors.append(f"{namespace}/{r.get('memory_key')}: refused: {e}")
            await queue.stamp_judged(r)
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
    await asyncio.gather(*jobs)


async def run_pass() -> dict[str, Any]:
    set_query_tag(f"worker.{NAME}")
    p = _Pass()
    with record_pass(NAME) as rec:
        groups = await _queued(p)
        if not groups:
            pass_context.mark_idle()
        await _resolve_all(p, groups)
        if p.unavailable is not None:
            log.warning(
                "%s: model %r is unavailable (%s); the rest of the pass is not judged",
                NAME,
                settings.conflict_resolver_model,
                p.unavailable,
            )
        outcome: dict[str, Any] = p.counts.as_dict()
        rec.outcome = outcome
        if groups:
            log.info("%s: %s", NAME, " ".join(f"{k}={v}" for k, v in outcome.items()))
        log_pass_errors(log, NAME, p.errors)
    return outcome


__all__ = ["NAME", "run_pass"]
