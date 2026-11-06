"""Moving document originals from ClickHouse to the bucket (`loom knowledge
migrate-originals`): each original whose latest catalog row still holds its bytes,
and that the namespace still needs (`blobs.needed`; the rest are the sweep's), is
copied to an object, hash-verified and switched (`blobs.move_to_bucket`). A run is
idempotent and resumable: what it switched is no longer walked, and a run stopped
part way leaves every original readable from wherever its row says."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from server.core import object_store as objects
from server.core.ch import set_query_tag
from server.core.errors import ValidationError
from server.knowledge.store import blobs

log = logging.getLogger(__name__)

# Keys read per page of a namespace's walk, and namespaces per page of their listing;
# each original is moved one at a time.
_PAGE = 200  # not a setting: a page of a one-off operator walk


@dataclass
class Migrated:
    moved: int = 0  # switched to an object in the bucket
    skipped: int = 0  # gone, replaced or erased since the walk read them
    # Nothing needs them now (no live document, unexpired upload or open ingestion): not
    # moved, and the sweep's once older than document_blob_min_age_seconds.
    unneeded: int = 0
    pending: int = 0  # a dry run's originals still held in ClickHouse
    bytes: int = 0  # of the originals moved, or with a dry run of those held
    errors: list[str] = field(default_factory=list)
    # Switched as an erasure that may cover it started and had not finished (or its check
    # failed): what that erasure decides for the original holds for the switched row.
    left_to_erasure: list[str] = field(default_factory=list)


async def migrate_originals(
    *, org: str = "", namespace: str = "", dry_run: bool = False
) -> Migrated:
    """Move every original held in ClickHouse that its namespace still needs (of
    `org`, or of its `namespace`) to the bucket, counting the others as left to the
    sweep; with `dry_run`, count them. An original that fails (bytes that do not hash
    to their sha or fill their size, a bucket that refuses) is reported and left where
    it is, and the walk goes on with the next. One an erasure held back makes the walk
    read the namespace's fence again: an erasure holding it refuses the rest of the
    namespace in one line. A ClickHouse error stops the run: the next run goes on."""
    set_query_tag("cli:knowledge-migrate-originals")
    out = Migrated()
    for org_, ns in await _scopes(org, namespace):
        # One read of the ledger for the namespace; again after an erasure held one back.
        refusal = None if dry_run else await blobs.fence(org_, ns)
        after = ""
        while not refusal:
            page = await blobs.held_in_clickhouse(org_, ns, after, _PAGE)
            wanted = await blobs.needed(org_, ns, [h.sha for h in page.held])
            for held in page.held:
                if held.sha not in wanted:
                    out.unneeded += 1
                    continue
                if not await _one(held, dry_run, out):
                    refusal = await blobs.fence(org_, ns)
                    if refusal:
                        break
            if not page.after:
                break
            after = page.after
        if refusal:
            # The rest of the namespace is refused alike: once, not once per original.
            out.errors.append(f"{org_}/{ns}: {refusal}; its originals are left in ClickHouse")
    return out


async def _scopes(org: str, namespace: str) -> list[tuple[str, str]]:
    """The (org, namespace) pairs to walk: the one named, or each holding originals."""
    if org and namespace:
        return [(org, namespace)]
    pairs: list[tuple[str, str]] = []
    after = ("", "")
    while True:
        page = await blobs.namespaces(after, _PAGE, org=org)
        pairs += page
        if len(page) < _PAGE:
            return pairs
        after = page[-1]


async def _one(held: blobs.Held, dry_run: bool, out: Migrated) -> bool:
    """Count one original. False when an erasure held it back or raced its switch."""
    if dry_run:
        out.pending += 1
        out.bytes += held.size
        return True
    try:
        if await blobs.move_to_bucket(held):
            out.moved += 1
            out.bytes += held.size
        else:
            out.skipped += 1
    except blobs.ErasureOpen as e:
        log.warning("originals: %s/%s/%s not moved: %s", held.org, held.namespace, held.sha, e)
        out.errors.append(f"{held.org}/{held.namespace}/{held.sha}: {e}")
        return False
    except blobs.SwitchedUnderErasure as e:
        out.moved += 1
        out.bytes += held.size
        out.left_to_erasure.append(f"{held.org}/{held.namespace}/{held.sha}: {e}")
        return False
    except (
        blobs.CorruptOriginal,
        objects.ObjectStoreError,
        ValidationError,  # its object key would be too long for the bucket
    ) as e:
        log.warning("originals: %s/%s/%s not moved: %s", held.org, held.namespace, held.sha, e)
        out.errors.append(f"{held.org}/{held.namespace}/{held.sha}: {e}")
    return True
