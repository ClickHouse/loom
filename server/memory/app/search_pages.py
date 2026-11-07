"""memory.search page handles — the rest of the ranked pool, parked once and
served by handle.

A search fuses a pool of `pool_top_k` candidates and cuts it to `top_k`.
Without a handle the caller sees the top of a list and cannot tell whether it
is the whole list (most searches return exactly `top_k` rows). A reader that
can page past the retriever's first cut is no longer capped by it.

Re-running the ANN legs with an offset would not give a second page: ANN
results are not stable across calls and a search is paid again. Instead the
ranked tail past `top_k` is serialized once into `loom.context_blobs` (the
store `context.offload` already uses: ZSTD, TTL, namespace-scoped) and the
response carries `candidates_total` and a `next_page` handle. A later
`memory.search(page=<handle>)` reads the parked list, serves the next slice
and hands back the handle for the slice after that. Pages are stable, cost
one keyed read, and stay inside the read scope the first call resolved.

Handle format: `page/<sha256 prefix>@<offset>`. The blob's JSON is
`{"v": 1, "query": ..., "top_k": ..., "candidates_total": ...,
"namespaces_searched": [...], "namespaces_skipped": [...], "hits": [...]}`
where `hits` is the tail in rank order, each a `_MemorySearchHit` dump, and
the namespace lists are the first call's, so every page says what it covers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from server.core import pg
from server.core.datetimes import naive_utc_now
from server.memory.domain.search_types import RowKey, hit_row_key
from server.settings import settings

if TYPE_CHECKING:
    from server.memory.api.models_search import _MemorySearchHit

PAGE_PREFIX = "page/"
PAGE_VERSION = 1


def split_tail(pool: list[Any], final: list[Any]) -> tuple[int, list[Any]]:
    """(distinct candidates in the pool, the ranked tail not served).

    The pool may hold the same row more than once (several legs found it);
    the count is by distinct row so "20 of 124" means 124 memories. The tail
    is every pool row absent from the served block, best rank first."""
    served = {hit_row_key(h) for h in final}
    seen: set[RowKey] = set()
    distinct: list[Any] = []
    for h in sorted(pool, key=lambda x: float(getattr(x, "rank_score", 0.0) or 0.0), reverse=True):
        k = hit_row_key(h)
        if k in seen:
            continue
        seen.add(k)
        distinct.append(h)
    tail = [h for h in distinct if hit_row_key(h) not in served]
    return len(seen | served), tail


def _parked(hit: Any) -> dict[str, Any]:
    """A hit as a page stores it, with what a later page's check against the Postgres
    authority reads (`row_identity`, `row_chunk`, `row_version`, `chunk_typed`, off the
    wire)."""
    if not hasattr(hit, "model_dump"):
        return dict(hit)
    out: dict[str, Any] = hit.model_dump()
    if not pg.enabled():
        return out
    if getattr(hit, "_row_identity", None) is not None:
        out["row_identity"] = list(hit._row_identity)
    if getattr(hit, "_row_chunk", None) is not None:
        out["row_chunk"] = list(hit._row_chunk)
    if getattr(hit, "_row_version", 0):
        out["row_version"] = int(hit._row_version)
    if getattr(hit, "_chunk_typed", False):
        out["chunk_typed"] = True
    return out


def unparked(row: dict[str, Any]) -> _MemorySearchHit | None:
    """A parked row back as a hit, with what `_parked` kept off the wire; None for a row
    this build cannot read."""
    from server.memory.api.models_search import _MemorySearchHit

    r = dict(row)
    identity, chunk = r.pop("row_identity", None), r.pop("row_chunk", None)
    row_version = int(r.pop("row_version", 0) or 0)
    chunk_typed = bool(r.pop("chunk_typed", False))
    try:
        hit = _MemorySearchHit.model_validate(r)
    except Exception:  # noqa: BLE001 — a row this build cannot read is skipped
        return None
    if identity is not None:
        hit._row_identity = tuple(identity)
    if chunk is not None:
        hit._row_chunk = tuple(chunk)
    hit._row_version = row_version
    hit._chunk_typed = chunk_typed
    return hit


def encode_page(
    *,
    query: str,
    top_k: int,
    candidates_total: int,
    tail: list[Any],
    namespaces_searched: list[str],
    namespaces_skipped: list[str],
    replay: bool = False,
) -> str:
    """`replay`: the search was an `as_of` replay, whose pages the Postgres authority does
    not check (`pg_validation`)."""
    hits = [_parked(h) for h in tail]
    page: dict[str, Any] = {
        "v": PAGE_VERSION,
        "query": query,
        "top_k": int(top_k),
        "candidates_total": int(candidates_total),
        "namespaces_searched": namespaces_searched,
        "namespaces_skipped": namespaces_skipped,
        "hits": hits,
    }
    if replay:
        page["replay"] = True
    return json.dumps(
        page,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def page_blob_key(content: str) -> str:
    return PAGE_PREFIX + hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]


def make_handle(blob_key: str, offset: int) -> str:
    return f"{blob_key}@{int(offset)}"


def parse_handle(handle: str) -> tuple[str, int] | None:
    """`page/<sha>@<offset>` → (blob_key, offset); None for anything else."""
    h = (handle or "").strip()
    if not h.startswith(PAGE_PREFIX) or "@" not in h:
        return None
    key, _, off = h.rpartition("@")
    if not off.isdigit() or len(key) <= len(PAGE_PREFIX):
        return None
    return key, int(off)


# The fire-and-forget idiom; the page is read seconds later by a caller that
# already holds the key.
PARK_INSERT_SETTINGS: dict[str, int] = {"async_insert": 1, "wait_for_async_insert": 0}


async def store_page(
    ch: Any,
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    run_id: str,
    content: str,
    ttl_s: int | None = None,
) -> str:
    """Park the encoded tail; returns the blob key. Content-addressed, so an
    identical tail (a repeated search) is one row under the ReplacingMergeTree.

    The insert is fire-and-forget (`async_insert=1`,
    `wait_for_async_insert=0`). The key is computed here, not by the write,
    so the handle is valid the moment this returns; the row lands within
    the async-insert flush window. A committed park would hold the search
    response for 2.1–6.6 s on ClickHouse Cloud. The async-insert buffer is
    ClickHouse's, not this replica's, so a Loom restart loses no page; a page
    lost with a ClickHouse node answers `page_expired` and the caller re-runs
    the search."""
    if ttl_s is None:
        ttl_s = settings.memory_search_page_ttl_s
    key = page_blob_key(content)
    now = naive_utc_now()
    await ch.insert(
        "loom.context_blobs",
        [
            {
                "org": org,
                "namespace": namespace,
                "agent": agent or "*",
                "user_id": user_id or "-",
                "session_id": session_id or "-",
                "run_id": run_id or "-",
                "blob_key": key,
                "content": content,
                "content_bytes": len(content.encode("utf-8")),
                "content_type": "application/json",
                "summary": "memory.search ranked tail",
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "created_at": now,
                "expires_at": now + timedelta(seconds=int(ttl_s)),
                "tombstoned_at": None,
            }
        ],
        settings=PARK_INSERT_SETTINGS,
    )
    return key


async def load_page(ch: Any, *, org: str, namespace: str, blob_key: str) -> dict[str, Any] | None:
    row = await ch.query_one(
        """
        SELECT content
        FROM loom.context_blobs FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND blob_key = {key:String}
          AND tombstoned_at IS NULL AND expires_at > now()
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "key": blob_key},
    )
    if not row:
        return None
    try:
        payload = json.loads(str(row.get("content") or ""))
    except Exception:  # noqa: BLE001 — an unreadable page blob is a miss
        return None
    if not isinstance(payload, dict) or int(payload.get("v") or 0) != PAGE_VERSION:
        return None
    return payload
