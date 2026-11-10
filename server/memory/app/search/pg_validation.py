"""Search under the Postgres authority: ClickHouse ranks, Postgres says what is current.

A search is admitted at a barrier (`admit`: every revision through each searched
namespace's generation is in ClickHouse, or 503 PROJECTION_BEHIND), and each memory
hit is served only while the head of its own key still shows what its document was
built from (`validate`, `stale_flags`); a page is refilled once from the ranked
remainder and counts what it dropped (`stale_dropped`). What is checked and what is
not: docs/architecture/storage-authority.md, "Search".
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from server.core import pg
from server.core.ch import sequential_reads
from server.memory.app.pg_admission import ProjectionBehindError, wait_searchable
from server.memory.app.search.telemetry import record_page
from server.memory.app.set_chunks import set_chunk_identities
from server.memory.domain.search_types import hit_row_key
from server.memory.store import pg_memory
from server.observability import pg_pipeline
from server.settings import settings

if TYPE_CHECKING:
    from server.memory.app.search.context import SearchContext


async def admit(
    org: str,
    namespaces: Sequence[str] | None,
    cap_s: float | None = None,
    owed: int = pg_memory.SEARCHED,
) -> bool:
    """Wait until these namespaces' (all the org's when None) revisions through their
    current generations have the `owed` targets in ClickHouse, no longer than
    LOOM_PG_PROJECTION_WAIT_MS (and `cap_s`, a re-entrant pass's time left); whether
    anything was behind, which the rest of the search then reads sequentially. A wait is
    recorded in `loom.pg.search_wait_ms` under the op the request invoked."""
    if not pg.enabled():
        return False
    behind = await pg_memory.unsearchable_through(org, namespaces, owed)
    if not behind:
        return False
    started = time.monotonic()
    wait_s = settings.pg_projection_wait_ms / 1000
    deadline = started + (wait_s if cap_s is None else min(wait_s, cap_s))
    try:
        for namespace, through in sorted(behind.items()):
            await wait_searchable(org, namespace, through, deadline=deadline, owed=owed)
    except ProjectionBehindError:
        pg_pipeline.record_search_wait("behind_503", _ms_since(started))
        raise
    pg_pipeline.record_search_wait("passed", _ms_since(started))
    return True


def _ms_since(started: float) -> float:
    return (time.monotonic() - started) * 1000


# The wait's name in a report's `skipped` lines and, for audit and validate, coverage.
PROJECTION = "projection"
# True inside a report's `published_reads`: a report nested in it waits no second time.
_published: ContextVar[bool] = ContextVar("pg_published_reads", default=False)


_UNPUBLISHED = {
    ProjectionBehindError.code: "ClickHouse has not yet published every write the "
    "namespace committed; the report reads what it holds",
    pg.PostgresUnavailableError.code: "the Postgres authority did not answer; the report "
    "reads what ClickHouse holds, which may miss committed writes",
}


@asynccontextmanager
async def published_reads(
    org: str,
    namespaces: Sequence[str],
    skip: Callable[[str], None] | None = None,
    *,
    down: bool = False,
) -> AsyncIterator[None]:
    """A report's reads of ClickHouse under Postgres: they start once ClickHouse holds
    every revision committed in `namespaces` so far (`admit`; a parked delivery is not
    waited for) and read sequentially when anything was behind. Past the wait, or with
    Postgres down, the call fails with that 503, or, given `skip`, reads what ClickHouse
    holds and reports why through `skip`. A report run inside another's reads (tune's
    audit) waits no second time, and neither does one whose caller already found
    Postgres down (`down`, said by that caller). On ClickHouse, the reads as they are."""
    if _published.get():
        yield
        return
    behind = True
    if not down:
        try:
            behind = await admit(org, namespaces)
        except (ProjectionBehindError, pg.PostgresUnavailableError) as e:
            if skip is None:
                raise
            skip(f"{e.code}: {_UNPUBLISHED[e.code]}")
    token = _published.set(True)
    try:
        with sequential_reads() if behind else nullcontext():
            yield
    finally:
        _published.reset(token)


def _judged_by_authority(hit: Any) -> bool:
    """A hit the Postgres authority can judge: a fact or anchor row of `loom.memory`, not
    a promoted `_shared` row."""
    identity = hit._row_identity
    return hit.source in ("fact", "anchor") and not (identity and identity[0] == "_shared")


def doc_key(h: Any, namespace: str) -> tuple[str, str, str, str, str]:
    """A hit's (namespace, agent, user_id, session_id, memory_key). Requires
    `_row_identity`."""
    return (h.namespace or namespace, *h._row_identity, h.memory_key)


def doc_version(h: Any) -> int:
    """The version of the stored row a hit's document was built from."""
    return int(h.version or h._row_version)


async def stale_flags(org: str, namespace: str, hits: list[Any]) -> list[bool]:
    """For each hit, whether the authority no longer serves it; none without the
    Postgres authority."""
    if not pg.enabled() or not hits:
        return [False] * len(hits)
    judged = [_judged_by_authority(h) for h in hits]
    # A chunk made into its row's hit shows the chunk's type: its row is checked by any type.
    full = {
        i: (*doc_key(h, namespace), "" if h._chunk_typed else h.memory_type, doc_version(h))
        for i, h in enumerate(hits)
        if judged[i] and h._row_identity is not None
    }
    # A hit showing a set chunk's passage: its writer's current value must still cut it.
    chunked = {
        i: (h.namespace or namespace, h._row_chunk[0], h._row_chunk[1], h.memory_key)
        for i, h in enumerate(hits)
        if judged[i] and h._row_chunk is not None
    }
    live, heads = await asyncio.gather(
        pg_memory.live_versions(org, list(set(full.values()))),
        pg_memory.chunk_heads(org, list(set(chunked.values()))),
    )
    # Cut each writer's heads once, as far as the furthest chunk the page shows, so a chunk
    # written before the chunk cap was lowered still matches.
    reach: dict[tuple[str, str, str, str], int] = {}
    for i, key in chunked.items():
        _, _, index, _ = hits[i]._row_chunk
        reach[key] = max(reach.get(key, 0), index + 1)
    cut: dict[tuple[str, str, str, str], set[tuple[int, str]]] = {
        key: {
            identity
            for value, memory_type in versions
            for identity in set_chunk_identities(
                value, memory_type, key[3], at_least=reach.get(key, 0)
            )
        }
        for key, versions in heads.items()
    }
    out = []
    for i, h in enumerate(hits):
        if not judged[i]:
            out.append(False)
        elif i not in full and i not in chunked:
            out.append(True)  # its identity was not read: fail closed
        else:
            ok = i not in full or full[i] in live
            if i in chunked:
                _, _, index, sha = h._row_chunk
                ok = ok and (index, sha) in cut.get(chunked[i], set())
            out.append(not ok)
    return out


async def validate(ctx: SearchContext) -> list[Any]:
    """Drop the stale memory hits from the page and refill it from the remainder,
    updating `final_hits`, `page_tail`, `candidates_total`, `stale_dropped` and
    `stale_versions` on `ctx`. The page as it is without the Postgres authority or
    under an `as_of` replay."""
    page: list[Any] = ctx.final_hits
    if not pg.enabled() or ctx.input.as_of is not None:
        return page
    tail = ctx.page_tail[: len(page)]
    stale = await stale_flags(ctx.input.org, ctx.input.namespace, page + tail)
    flagged = list(zip(page + tail, stale, strict=True))
    kept = [h for h, s in flagged[: len(page)] if not s]
    live_tail = [h for h, s in flagged[len(page) :] if not s]
    room = len(page) - len(kept)
    kept += live_tail[:room]
    # The remainder a next page serves: the checked rows past those used, then the rest.
    ctx.page_tail = live_tail[room:] + ctx.page_tail[len(tail) :]
    gone_hits = [h for h, s in flagged if s]
    ctx.final_hits = kept
    ctx.stale_dropped += len(gone_hits)
    # `candidates_total` counts distinct rows: a row goes when none of its hits is left.
    gone = {hit_row_key(h) for h in gone_hits}
    gone -= {hit_row_key(h) for h in kept} | {hit_row_key(h) for h in ctx.page_tail}
    ctx.candidates_total = max(0, ctx.candidates_total - len(gone))
    ctx.stale_versions = {
        doc_key(h, ctx.input.namespace): doc_version(h)
        for h in gone_hits
        if h._row_identity is not None
    }
    record_page(ctx)  # the page as validated, before the tiers and the floor
    return kept
