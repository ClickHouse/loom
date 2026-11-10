"""The knowledge plane's part of `loom reconcile` (`server/cli/restore.py`): after a
ClickHouse restore, every document head ClickHouse lost is owed again, held parked until
the erasures ran, then released for publication."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from server.knowledge.store import pg_datasets, pg_documents

log = logging.getLogger(__name__)


async def hold(after: datetime) -> int:
    """Owe again every knowledge head committed after `after`, held parked; returns how
    many."""
    return await pg_documents.hold_for_republish(after)


async def hold_documents_behind(ch: Any, *, page: int = 2000) -> int:
    """Every Postgres document head ClickHouse holds at an older version, or not at all,
    owed again and held, a page at a time; returns how many."""
    held = 0
    after: tuple[str, str, str] | None = None
    while True:
        heads = await pg_documents.head_page(after, page)
        if not heads:
            return held
        keys = [(h["org"], h["namespace"], h["source_uri"]) for h in heads]
        rows = await ch.query_all(
            "SELECT org, namespace, source_uri, max(version) AS v FROM loom.documents "
            "WHERE (org, namespace, source_uri) IN {keys:Array(Tuple(String, String, String))} "
            "GROUP BY org, namespace, source_uri",
            params={"keys": keys},
        )
        published = {(r["org"], r["namespace"], r["source_uri"]): int(r["v"]) for r in rows}
        behind = [
            k for h, k in zip(heads, keys, strict=True) if published.get(k, -1) < int(h["version"])
        ]
        held += await pg_documents.hold_heads(behind)
        after = keys[-1]


async def held_sources() -> list[dict[str, Any]]:
    """The held live heads (org, namespace, source_uri): their documents are published
    again, but their chunks lived in ClickHouse alone, so those written after the point
    are gone until the source is re-chunked (`loom rechunk`)."""
    return await pg_documents.held_sources()


async def dataset_sources_lost(after: datetime) -> list[dict[str, Any]]:
    """The sources (org, namespace, source_uri) whose Postgres catalog rows name a dataset
    snapshot written after `after`: its rows lived in ClickHouse alone, so the dataset
    reads with no rows until the source's file is uploaded and ingested again."""
    return await pg_datasets.sources_with_snapshots_since(int(after.timestamp() * 1e9))


async def publish_held(*, batch: int = 200, passes: int = 1000) -> int:
    """Release what the holds held and publish every knowledge delivery due, as the
    delivery worker would; returns the documents published (see the memory plane's)."""
    from server.knowledge.app.pg_delivery import publish_due

    await pg_documents.release_held()
    published = 0
    for _ in range(passes):
        claimed, sent, _failed = await publish_due(batch)
        published += sent
        if not claimed:
            return published
    log.warning("loom reconcile: knowledge deliveries still due after %d passes", passes)
    return published
