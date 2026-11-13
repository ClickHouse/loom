"""Once a day, group each namespace's recent misses into the gaps
`knowledge.gaps.list` serves.

Per namespace with misses in the window: read the most asked normalised queries
(`knowledge_gaps_max_groups`) that at least `knowledge_gaps_min_askers` distinct people
asked in that form, embed them in one batch, merge the ones that are alike
(`knowledge_gaps_merge_similarity`), and write them as a new build. The threshold is
applied before merging, so every query a gap shows was asked by that many people. Every
build ends with one marker row (empty `gap_id`), written last; readers take the latest
build with its marker, so a build with no gap left replaces the last one instead of
leaving it listed. A pass that read the misses before a user erasure deleted them could
write that user back, so after writing a build the pass checks the erasure ledger and
drops its build when an erasure that may reach the namespace was unfinished when the read
began or has finished since (the next pass rebuilds without what it erased).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from server.core.datetimes import naive_utc_now_seconds
from server.knowledge.app import gaps as gaps_app
from server.knowledge.domain.gaps import Group, gap_id, merge_groups, ranked
from server.knowledge.store import gaps as gaps_store
from server.llm import customer
from server.observability import cost_accumulator
from server.settings import settings
from server.workers import pass_context
from server.workers.cost_event import emit_worker_cost_event_for
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass
class KnowledgeGapsResult:
    namespaces: int = 0
    gaps: int = 0
    # Namespaces that kept their last build: held, over the cost cap, consent unread, or
    # a build an erasure raced.
    kept: int = 0


async def first_pass_due() -> bool:
    """Due at start when some namespace has misses or a live gap and no build is newer
    than `knowledge_gaps_interval_seconds`: a daily pass would otherwise wait out the
    boot stagger on processes that live a few hours, and a restart must not rebuild what
    a pass just wrote."""
    if not await gaps_store.namespaces_to_build(gaps_store.MISS_DAYS, 1):
        return False
    age = await gaps_store.seconds_since_last_build()
    return age is None or age >= settings.knowledge_gaps_interval_seconds


async def run_pass() -> KnowledgeGapsResult:
    result = KnowledgeGapsResult()
    if not gaps_app.every_replica_erases_gaps():
        # An older replica's erasure has no step for the gap tables: write nothing.
        pass_context.mark_idle()
        return result
    with record_pass("knowledge_gaps") as rec:
        pairs = await gaps_store.namespaces_to_build(
            gaps_store.MISS_DAYS, settings.knowledge_gaps_max_namespaces
        )
        for org, namespace in pairs:
            # A lost lease stops the pass: the new leader runs its own.
            pass_context.check_stop()
            pass_context.set_namespace(f"{org}/{namespace}")
            try:
                built = await build_namespace(org, namespace)
            except customer.SwitchUnread:
                raise  # the switch holds every org's calls alike: the pass stops
            except customer.HOLDS:
                result.kept += 1  # model calls are off for the org: it keeps its last build
                continue
            except Exception as e:  # noqa: BLE001 — one namespace's failure must not stop the rest
                pass_context.note_failure(e, f"knowledge_gaps {org}/{namespace}")
                continue
            if built is None:
                result.kept += 1
            else:
                result.namespaces += 1
                result.gaps += built
        rec.outcome = {"namespaces": result.namespaces, "gaps": result.gaps, "kept": result.kept}
    if not pairs:
        pass_context.mark_idle()
    return result


async def build_namespace(org: str, namespace: str) -> int | None:
    """Write one build of the namespace's gaps; returns how many it holds, or None when
    the namespace keeps its last build this pass."""
    from server.observability import capture_consent

    read_at = int(time.time())
    # A namespace whose capture level is now `off` shows nothing: an empty build. An
    # unreadable level skips this pass, so a failed read never deletes misses.
    try:
        refused = await capture_consent.level_uncached(org, namespace) == "off"
    except Exception as e:  # noqa: BLE001 — unknown consent: no build and no delete this pass
        log.warning("knowledge_gaps: consent unreadable for %s/%s: %s", org, namespace, e)
        return None
    groups: list[Group] = []
    if refused:
        # The builds go with the misses: a user erasure finds a namespace through them.
        await gaps_store.delete_builds(org, namespace)
        await gaps_store.delete_misses(org, namespace)
    else:
        rows = await gaps_store.miss_groups(
            org,
            namespace,
            days=gaps_store.MISS_DAYS,
            min_askers=settings.knowledge_gaps_min_askers,
            limit=settings.knowledge_gaps_max_groups,
            # Never below the threshold, or a gap could show fewer askers than it needed.
            max_askers=max(
                settings.knowledge_gaps_max_askers_per_group, settings.knowledge_gaps_min_askers
            ),
        )
        groups = [_group(r) for r in rows]
    gaps: list[Group] = []
    if groups:
        if await _over_cap(org, namespace):
            # Embeddings are spend: an over-cap namespace keeps its last build.
            return None
        vectors = await _embed(org, namespace, [g.head for g in groups])
        merged = await asyncio.to_thread(
            merge_groups, groups, vectors, settings.knowledge_gaps_merge_similarity
        )
        gaps = ranked(merged)
    # Nanoseconds: two passes overlapping on one namespace never share a build id.
    build = time.time_ns()
    now = naive_utc_now_seconds()
    base = {"org": org, "namespace": namespace, "build": build, "built_at": now}
    await gaps_store.insert_build(
        [
            {
                **base,
                "gap_id": gap_id(org, namespace, g.head),
                "head": g.head,
                "samples": g.samples,
                "askers": len(g.askers),
                "asks": g.asks,
                "last_at": g.last_at,
            }
            for g in gaps
        ]
    )
    # The gaps first, the marker last: a build without its marker is never read, so one
    # an erasure raced is dropped before anyone sees it. Any user's erasure counts: a
    # group holds at most `knowledge_gaps_max_askers_per_group` of its askers, so the
    # pass cannot tell whether the erased user was among the rest.
    if await gaps_app.erasure_since(org, namespace, read_at, None):
        log.info(
            "knowledge_gaps: %s/%s keeps its last build: an erasure may reach it", org, namespace
        )
        if gaps:  # an empty build wrote nothing yet: no marker, no rows
            await gaps_store.delete_build(org, namespace, build)
        # A refused namespace's builds are already gone: it keeps none.
        return 0 if refused else None
    marker = {
        **base,
        "gap_id": "",
        "head": "",
        "samples": [],
        "askers": 0,
        "asks": 0,
        "last_at": now,
    }
    await gaps_store.insert_build([marker])
    return len(gaps)


async def _embed(org: str, namespace: str, texts: list[str]) -> list[list[float]]:
    """The heads' vectors, as a call made for the org: its model-call switch and the
    inference attribution apply, and the spend lands in its cost events."""
    from server.embedding import get_embedder

    started = time.monotonic()
    cost_accumulator.begin_op()
    try:
        with customer.for_org(org, namespace=namespace):
            return await get_embedder().embed_many(texts)
    finally:
        emit_worker_cost_event_for(
            org=org, namespace=namespace, operation="worker.knowledge_gaps", started=started
        )


async def _over_cap(org: str, namespace: str) -> bool:
    """Whether the namespace is over its daily cost cap, read fail-closed."""
    from server.observability.budget import namespaces_over_cap

    over = await namespaces_over_cap([(org, namespace)], fail_open=False)
    return bool(over.get((org, namespace)))


def _group(row: dict[str, Any]) -> Group:
    return Group(
        head=str(row["head"]),
        asks=int(row["asks"]),
        last_at=row["last_at"],
        askers={str(a) for a in row["askers"]},
    )
