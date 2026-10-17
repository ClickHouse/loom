"""Judge the writes the conflict check marked, one LLM call per row, and act on the
verdicts (`memory.app.conflict_resolver`).

Each pass reads up to `memory_conflict_resolver_batch` marked rows, oldest first,
then up to `memory_conflict_resolver_recheck_batch` rows already `conflicted` that
no resolver judged (flags from the cosine check, or from an older build's judge),
so a false flag is cleared once. Rows are handled per (org, namespace): a
namespace over its daily LLM cost cap, or an org with an erasure queued or running,
is skipped and stays owed. A row is stamped judged only after its actions, so a
pass cut short judges it again and acting twice changes nothing. If the provider
does not know the model, the pass logs one warning and stops; nothing else fails.
Leader-only, like every worker.
"""

from __future__ import annotations

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


async def _queued() -> dict[tuple[str, str], list[dict[str, Any]]]:
    """The pass's rows, each identity once, grouped by (org, namespace)."""
    rows = await queue.owed_rows(limit=int(settings.memory_conflict_resolver_batch))
    recheck = int(settings.memory_conflict_resolver_recheck_batch)
    if recheck > 0:
        rows += await queue.unjudged_conflicted_rows(limit=recheck)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen: set[tuple[str, ...]] = set()
    for r in rows:
        if _identity(r) not in seen:
            seen.add(_identity(r))
            groups.setdefault((str(r["org"]), str(r["namespace"])), []).append(dict(r))
    return groups


async def _resolve_group(
    org: str,
    namespace: str,
    rows: list[dict[str, Any]],
    counts: resolver.Counts,
    errors: list[str],
) -> None:
    from server.core.ch import get_client
    from server.memory.app.conflicts import namespace_conflict_policy
    from server.observability import cost_accumulator
    from server.workers.cost_event import emit_worker_cost_event_for

    if await queue.erasure_underway(org):
        counts.skipped_erasure += len(rows)
        return
    policy = await namespace_conflict_policy(org, namespace, ch=get_client())
    for r in rows:
        started = time.monotonic()
        # Each row's call bills its own namespace, which the cost cap reads.
        cost_accumulator.begin_op()
        try:
            await resolver.resolve_row(r, policy=policy, counts=counts)
        except resolver.ModelUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 — one row's failure never stops the pass
            counts.errored += 1
            errors.append(f"{namespace}/{r.get('memory_key')}: {type(e).__name__}: {e}")
        finally:
            emit_worker_cost_event_for(
                org=org, namespace=namespace, operation=f"worker.{NAME}", started=started
            )


async def run_pass() -> dict[str, Any]:
    from server.observability.budget import namespaces_over_cap

    set_query_tag(f"worker.{NAME}")
    counts = resolver.Counts()
    errors: list[str] = []
    with record_pass(NAME) as rec:
        groups = await _queued()
        if not groups:
            pass_context.mark_idle()
        over_cap = await namespaces_over_cap(groups) if groups else {}
        try:
            for (org, namespace), rows in groups.items():
                if over_cap.get((org, namespace), False):
                    counts.skipped_budget += len(rows)
                    continue
                await _resolve_group(org, namespace, rows, counts, errors)
        except resolver.ModelUnavailable as e:
            log.warning(
                "%s: model %r is unavailable (%s); nothing judged this pass",
                NAME,
                settings.conflict_resolver_model,
                e,
            )
        outcome: dict[str, Any] = counts.as_dict()
        rec.outcome = outcome
        if groups:
            log.info("%s: %s", NAME, " ".join(f"{k}={v}" for k, v in outcome.items()))
        log_pass_errors(log, NAME, errors)
    return outcome


__all__ = ["NAME", "run_pass"]
