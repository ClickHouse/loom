"""Read-your-writes for search: a namespace written moments ago, a hit ingested
seconds ago or an empty pool re-runs the search under sequential consistency.

The re-run enters through the package's `handle_memory_search`, imported at call
time: the handler imports this module."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from server.core.ch import sequential_reads, sequential_reads_active
from server.core.datetimes import naive_utc_now
from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput, _MemorySearchHit
from server.memory.app.recent_writes import written_recently
from server.memory.app.search.context import SearchContext

log = logging.getLogger(__package__)


def _fresh_namespace_read(
    org: str, namespace: str, *, already_sequential: bool, enabled: bool, window_s: float
) -> bool:
    """Read-your-writes up front: this process wrote `namespace` within the replication window,
    so every leg of this search reads with sequential consistency instead of hoping the SELECT
    lands on the replica that has the part. Catches the partial view the empty-pool retry
    cannot — a replica with SOME of the fresh parts returns a shorter list and nothing looks
    wrong."""
    return (
        enabled
        and not already_sequential
        and window_s > 0
        and written_recently(org, namespace, window_s)
    )


async def _written_elsewhere(
    org: str, namespace: str, *, already_sequential: bool, enabled: bool, window_s: float
) -> bool:
    """The same question for every other process: the namespace's write key, which
    a write sets before it answers (where the server has a Keeper). A failed read
    is no evidence either way: False."""
    if already_sequential or not enabled or window_s <= 0 or not org or not namespace:
        return False
    from server.core import namespace_write_marks

    try:
        return await namespace_write_marks.written_within(org, namespace, window_s)
    except Exception as e:  # noqa: BLE001 — the after-the-fact re-reads still apply
        log.warning("memory.search: write-mark read failed: %s", e)
        return False


def _newest_hit_age_s(hits: list[Any], *, now: datetime | None = None) -> float:
    """Seconds since the most recently ingested hit (`created_at`, ingest
    time); +inf when no hit carries a parseable one."""
    best = float("inf")
    ref = now or naive_utc_now()
    for h in hits:
        raw = str(getattr(h, "created_at", "") or "")[:19].replace("T", " ")
        if not raw or raw.startswith("1970"):
            continue
        try:
            t = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        best = min(best, (ref - t).total_seconds())
    return best


def _fresh_hit_reread_wanted(
    hits: list[Any], *, already_sequential: bool, enabled: bool, window_s: float
) -> bool:
    """Cross-process partial-view guard. The process-local write registry sees a write on one
    web worker in four; a search on another worker has no registry entry, but a hit ingested
    seconds ago says the namespace is being written NOW — re-read once with sequential
    consistency so the parts this replica has not seen yet are in the pool. Old namespaces
    never pay."""
    return (
        bool(hits)
        and not already_sequential
        and enabled
        and window_s > 0
        and _newest_hit_age_s(hits) <= window_s
    )


def _sequential_retry_wanted(
    *, pool_empty: bool, already_sequential: bool, enabled: bool, remaining_wall_s: float
) -> bool:
    """Read-your-writes for search: retry an empty pool once with sequential consistency.

    An ANN leg over a namespace with rows returns rows; an EMPTY pool right
    after a write is what a lagging Cloud replica looks like — the part the
    caller just inserted is not in this replica's metadata yet. Pay one
    more pass with sequential consistency only then: never when something
    came back, never inside the retry itself, never when the operator
    turned read-your-writes off, and never when the wall budget is spent."""
    return pool_empty and not already_sequential and enabled and remaining_wall_s > 0.05


async def _fresh_namespace_reread(input: MemorySearchInput) -> MemorySearchOutput | None:
    """Phase 2: read-your-writes up front. A namespace this process wrote
    within the replication window is searched again under sequential
    consistency; the caller returns that result. None = carry on."""
    # A namespace this process wrote moments ago is read with sequential consistency from the
    # first statement.
    from server.memory.app.search import handle_memory_search
    from server.memory.store import read_your_writes_enabled
    from server.settings import settings as _rw_settings

    org, ns = str(input.org or ""), str(input.namespace or "")
    window_s = float(_rw_settings.ch_read_your_writes_window_s)
    already, enabled = sequential_reads_active(), read_your_writes_enabled()
    if _fresh_namespace_read(
        org, ns, already_sequential=already, enabled=enabled, window_s=window_s
    ) or await _written_elsewhere(
        org, ns, already_sequential=already, enabled=enabled, window_s=window_s
    ):
        log.info(
            "memory.search: namespace written within the replication window; "
            "reading with sequential consistency (read-your-writes)"
        )
        with sequential_reads():
            fresh_result = await handle_memory_search(input)
        fresh_result.retrieval_work = {
            **dict(fresh_result.retrieval_work or {}),
            "read_your_writes_window": True,
        }
        return fresh_result
    return None


async def _consistency_reread(
    ctx: SearchContext, final_hits: list[_MemorySearchHit]
) -> MemorySearchOutput | None:
    """Phase 17: read-your-writes after the fact. A fresh newest hit, or an
    empty pool, re-runs the whole search under sequential consistency within
    the wall budget; the caller returns an adopted re-read. None = keep the
    first pass."""
    from server.settings import settings as _settings

    input = ctx.input
    search_budget = ctx.search_budget
    # Read-your-writes for search. The pool is empty across every leg and every namespace. On
    # ClickHouse Cloud this is the signature of a SELECT that landed on a replica which has not
    # yet seen the part the caller inserted a moment ago (e.g. an ask 200 ms after three
    # concurrent set_from_messages returns nothing while its neighbours find the chunks, and
    # the same question minutes later answers correctly). Run the whole search once more with
    # `select_sequential_consistency = 1` on every statement; adopt the retry when it found
    # anything. Costs one extra pass only on the path that was about to abstain anyway.
    from server.memory.app.search import handle_memory_search
    from server.memory.store import read_your_writes_enabled

    _rw_window_s = float(_settings.ch_read_your_writes_window_s)
    # One level of re-entry: a pass memory.search already re-entered never re-reads again.
    # A merged pass (loom_search's repository decisions) keeps only the up-front write-mark check.
    if getattr(input, "_pass_depth", 0) != 0 or getattr(input, "_merged_into_parent", False):
        return None
    reread_input = input.model_copy(update={"_pass_depth": 1})
    if (
        _fresh_hit_reread_wanted(
            final_hits,
            already_sequential=sequential_reads_active(),
            enabled=read_your_writes_enabled(),
            window_s=_rw_window_s,
        )
        and search_budget.wall_left_s() > 0.05
    ):
        log.info(
            "memory.search: newest hit ingested %.1f s ago; re-reading with sequential "
            "consistency (read-your-writes, cross-process)",
            _newest_hit_age_s(final_hits),
        )
        reread = None
        try:
            with sequential_reads():
                reread = await asyncio.wait_for(
                    handle_memory_search(reread_input), timeout=search_budget.remaining_wall_s()
                )
        except TimeoutError:
            log.warning("memory.search: fresh-hit sequential re-read exceeded the wall budget")
        except Exception as e:  # noqa: BLE001 — an optional re-read; the first pass stands
            log.warning("memory.search: fresh-hit sequential re-read failed: %s", e)
        if reread is not None and reread.results:
            reread.retrieval_work = {
                **dict(reread.retrieval_work or {}),
                "read_your_writes_fresh_hit": True,
            }
            return reread

    if _sequential_retry_wanted(
        pool_empty=not final_hits,
        already_sequential=sequential_reads_active(),
        enabled=read_your_writes_enabled(),
        remaining_wall_s=search_budget.wall_left_s(),
    ):
        log.info(
            "memory.search: empty pool over %d namespace(s); one retry with "
            "sequential consistency (read-your-writes)",
            len(ctx.namespaces),
        )
        retry_result = None
        try:
            with sequential_reads():
                retry_result = await asyncio.wait_for(
                    handle_memory_search(reread_input),
                    timeout=search_budget.remaining_wall_s(),
                )
        except TimeoutError:
            log.warning("memory.search: sequential-consistency retry exceeded the wall budget")
        except Exception as e:  # noqa: BLE001 — an optional retry; the first pass stands
            log.warning("memory.search: sequential-consistency retry failed: %s", e)
        if retry_result is not None and retry_result.results:
            log.info(
                "memory.search: sequential-consistency retry found %d hit(s) the first "
                "pass missed (lagging replica)",
                len(retry_result.results),
            )
            retry_result.retrieval_work = {
                **dict(retry_result.retrieval_work or {}),
                "read_your_writes_retry": True,
            }
            return retry_result
    return None
