"""Moving document originals from ClickHouse to the bucket (`loom knowledge
migrate-originals`): each original whose latest catalog row still holds its bytes
is copied to an object, hash-verified and switched (`blobs.move_to_bucket`). A
run is idempotent and resumable: what it switched is no longer walked, and a run
stopped part way leaves every original readable from wherever its row says."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from server.core import object_store as objects
from server.core.ch import set_query_tag
from server.knowledge.store import blobs

log = logging.getLogger(__name__)

# Originals read per page of the walk; each is moved one at a time.
_PAGE = 200  # not a setting: a page of a one-off operator walk


@dataclass
class Migrated:
    moved: int = 0  # switched to an object in the bucket
    bytes_moved: int = 0
    skipped: int = 0  # gone or replaced since the walk read them
    pending: int = 0  # a dry run's originals still held in ClickHouse
    errors: list[str] = field(default_factory=list)


async def migrate_originals(
    *, org: str = "", namespace: str = "", dry_run: bool = False
) -> Migrated:
    """Move every original held in ClickHouse (of `org`, or of its `namespace`) to
    the bucket; with `dry_run`, count them. An original that fails (bytes that do
    not hash to their sha, a bucket that refuses) is reported and left where it
    is; the walk goes on with the next."""
    set_query_tag("cli:knowledge-migrate-originals")
    out = Migrated()
    after = ("", "", "")
    while True:
        page = await blobs.held_in_clickhouse(after, _PAGE, org=org, namespace=namespace)
        for held in page:
            if dry_run:
                out.pending += 1
                out.bytes_moved += held.size
                continue
            try:
                if await blobs.move_to_bucket(held):
                    out.moved += 1
                    out.bytes_moved += held.size
                else:
                    out.skipped += 1
            except (blobs.CorruptOriginal, objects.ObjectStoreError) as e:
                log.warning(
                    "originals: %s/%s/%s not moved: %s", held.org, held.namespace, held.sha, e
                )
                out.errors.append(f"{held.org}/{held.namespace}/{held.sha}: {e}")
        if len(page) < _PAGE:
            return out
        last = page[-1]
        after = (last.org, last.namespace, last.sha)
