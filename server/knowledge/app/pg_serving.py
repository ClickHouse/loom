"""Knowledge reads under the Postgres authority: ClickHouse ranks the chunks, the heads say
which of them are served (`server.knowledge.domain.serving`). One Postgres read per fetch
(two when a full page is fetched again). Under the ClickHouse authority, every chunk
ClickHouse reads as live is served.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from server.core import pg
from server.knowledge.domain.serving import serves
from server.knowledge.store import pg_documents


class _Hit(Protocol):
    @property
    def namespace(self) -> str: ...
    @property
    def source_uri(self) -> str: ...
    @property
    def revision(self) -> int: ...
    @property
    def doc_version(self) -> int: ...


async def stale(org: str, chunks: Sequence[_Hit]) -> list[bool]:
    """For each chunk, whether its source's head does not serve it; all False without
    the Postgres authority."""
    if not pg.enabled() or not chunks:
        return [False] * len(chunks)
    heads = await pg_documents.serving(org, [(c.namespace, c.source_uri) for c in chunks])
    return [
        not serves(heads.get((c.namespace, c.source_uri)), c.revision, c.doc_version)
        for c in chunks
    ]


async def keep_served[H: _Hit](org: str, hits: list[H]) -> tuple[list[H], int]:
    """The hits their sources' heads serve, in order, and how many were left out."""
    flags = await stale(org, hits)
    return [h for h, gone in zip(hits, flags, strict=True) if not gone], sum(flags)


class _Ranked(_Hit, Protocol):
    @property
    def chunk_idx(self) -> int: ...


async def served_search[H: _Ranked](
    org: str, fetch: Callable[[int], Awaitable[list[H]]], k: int
) -> tuple[list[H], int]:
    """`fetch(k)`'s hits that are served, one per chunk (the best ranked: a source with no
    head serves every version ClickHouse holds), and how many stale ones it left out.
    When it left any out of a full page, more may rank past it, so it fetches once more,
    `2 * k` deep. Without the Postgres authority, `fetch(k)`'s hits as they are."""

    async def once(n: int) -> tuple[list[H], int, int]:
        hits = await fetch(n)
        kept, dropped = await keep_served(org, hits)
        if not pg.enabled():
            return kept, dropped, len(hits)
        seen: set[tuple[str, str, int]] = set()
        first = []
        for h in kept:
            if (key := (h.namespace, h.source_uri, h.chunk_idx)) not in seen:
                seen.add(key)
                first.append(h)
        return first, dropped, len(hits)

    kept, dropped, fetched = await once(k)
    if len(kept) < fetched and fetched >= k:
        kept, dropped, _ = await once(2 * k)
    return kept, dropped
