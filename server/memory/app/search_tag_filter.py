"""The hard tag filter's belt.

Every fact-table leg of memory.search carries a request's hard `tags` in its
SQL. The belt runs after fusion so a leg added later without that clause is
caught and disclosed (`retrieval_work.tag_filter_dropped`) instead of served;
memory.ask applies the same belt to its own pool.
"""

from __future__ import annotations

import logging
from typing import Any

from server.core.ch import get_client
from server.memory.domain.search_sql import hard_tag_filter

log = logging.getLogger(__name__)


def request_hard_tags(input: Any) -> list[str]:
    """The request's hard tag filter, [] under `tags_soft`."""
    return hard_tag_filter(getattr(input, "tags", None), getattr(input, "tags_soft", False))


# The hit sources that are rows of loom.memory_embeddings — the only rows that
# carry `tags`, so the only ones a hard tag filter can be checked against.
# Chunks, digests, entity summaries, observations, derived / computed evidence
# and record projections live in other tables and are outside the predicate.
_TAGGED_ROW_SOURCES = frozenset({"fact", "anchor"})


def stamp_row_tags[HitT](hit: HitT, tags: Any) -> HitT:
    """Remember the stored row's tags on the wire hit, privately,
    as the leg read them; the belt checks the hard filter against this."""
    try:
        hit._row_tags = tuple(str(t) for t in (tags or []) if t)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — a foreign hit type stays as it is
        pass
    return hit


def tagged_row_hit(hit: Any) -> bool:
    source = str(getattr(hit, "source", "fact") or "fact")
    key = str(getattr(hit, "memory_key", "") or "")
    return source in _TAGGED_ROW_SOURCES and bool(key) and not key.startswith("chunk:")


async def read_row_tags(
    org: str, namespaces: list[str], keys: list[str]
) -> dict[tuple[str, str], list[str]]:
    """One keyed statement: the live tags of `keys` across `namespaces`."""
    rows = await get_client().query_all(
        """
        SELECT namespace, memory_key, any(tags) AS tags
        FROM loom.memory_embeddings FINAL
        WHERE org = {org:String} AND namespace IN {nss:Array(String)}
          AND memory_key IN {keys:Array(String)} AND tombstoned_at IS NULL
        GROUP BY namespace, memory_key
        """,
        {"org": org, "nss": sorted(set(namespaces)), "keys": sorted(set(keys))},
    )
    return {
        (str(r["namespace"]), str(r["memory_key"])): [str(t) for t in (r.get("tags") or []) if t]
        for r in rows
    }


async def enforce_hard_tag_filter(
    hits: list[Any], input: Any, work: dict[str, Any], namespaces: list[str] | None = None
) -> list[Any]:
    """The belt: after every leg has fused, no memory row that
    lacks each of the request's hard `tags` survives into the served window.
    Every fact-table leg carries the predicate in SQL, so this drops
    nothing on a healthy path; it is here so a leg added later without the
    clause is caught and disclosed (`retrieval_work.tag_filter_dropped`)
    rather than served. Rows whose leg stamped `_row_tags` are checked in
    process; the rest are point-read in one statement. Fail-open on that
    read (`tag_filter_unverified` counts the rows it could not check).
    A request with no hard filter returns the list untouched and adds no key."""
    hard_tags = request_hard_tags(input)
    if not hard_tags or not hits:
        return hits
    wanted = set(hard_tags)
    unknown = [h for h in hits if tagged_row_hit(h) and getattr(h, "_row_tags", None) is None]
    by_key: dict[tuple[str, str], list[str]] = {}
    unverified = 0
    if unknown:
        nss = sorted(
            {str(getattr(h, "namespace", "") or input.namespace) for h in unknown}
            | set(namespaces or [])
        )
        try:
            by_key = await read_row_tags(input.org, nss, [str(h.memory_key) for h in unknown])
        except Exception as e:  # noqa: BLE001 — a belt never fails a search
            log.warning(
                "memory.search: hard tag belt read failed (kept %d rows): %s", len(unknown), e
            )
            unverified = len(unknown)
            by_key = {}
    kept: list[Any] = []
    dropped = 0
    for h in hits:
        if not tagged_row_hit(h):
            kept.append(h)
            continue
        row_tags = getattr(h, "_row_tags", None)
        if row_tags is None:
            if unverified:
                kept.append(h)
                continue
            ns = str(getattr(h, "namespace", "") or input.namespace)
            row_tags = tuple(by_key.get((ns, str(h.memory_key)), ()))
            stamp_row_tags(h, row_tags)
        if wanted.intersection(row_tags):
            kept.append(h)
        else:
            dropped += 1
    work["tag_filter_dropped"] = int(work.get("tag_filter_dropped", 0)) + dropped
    if unverified:
        work["tag_filter_unverified"] = int(work.get("tag_filter_unverified", 0)) + unverified
    if dropped:
        log.warning(
            "memory.search: %d fused row(s) lacked the hard tag filter %s and were dropped "
            "(a leg served past its predicate)",
            dropped,
            hard_tags,
        )
    return kept
