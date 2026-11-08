"""Register code namespaces in the namespace directory.

The code plane keys every row on (org, namespace); without a loom.namespaces row its
namespaces would be invisible to the dashboard's sidebar, un-mintable from Setup, and
indistinguishable from memory namespaces. Each ingest pass calls
ensure_registered() once per process per repo entry: if no directory row exists, insert
one with kind='code' and visibility='private' (read access stays grant-driven — see
docs). An existing row is re-inserted in full in three cases only: the config says the
repository is public and the row is a standard one not yet open (join_policy=open); the
config classifies the entry `restricted` and the row does not (narrowed); a maintainer's
access row says `standard` and the row says restricted (widened, the maintainer's own
decision). The entry's mirror and checkouts are tightened by the ingest pass, not here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("loom.code_index.registry")

_DONE: set[tuple[str, str]] = set()
# The branches whose maintainer sync ran in this process after the self-service
# registry was read: before that read the sync does nothing, so it runs again.
_SYNCED: set[tuple[str, str]] = set()


def describe(cfg: Any) -> str:
    return f"Code index of {cfg.repo} @ {cfg.git_branch} (read-only reference data)"


async def sync_maintainers(cfg: Any, client: Any) -> None:
    """On first sight of a branch namespace each process, bring onto it the admins
    of the repository's branches that reach it (`maintainers._reaches`). Until
    the self-service registry is first read the sync does nothing, and a later
    pass syncs the branch again."""
    from server.code.app.index import maintainers
    from server.code.app.index.repo_registry import registry_read

    read = registry_read()  # before the sync: a read landing during it is not this sync's
    await maintainers.sync_quietly(cfg.org, cfg.namespace, ch=client)
    if read:
        _SYNCED.add((cfg.org, cfg.namespace))


async def ensure_registered(cfg: Any, *, ch: Any | None = None, sync: bool = True) -> bool:
    """Idempotent. Returns True when a row was inserted this call. `sync=False` leaves the
    maintainers' fan-out, whose claims wait, to a caller that runs `sync_maintainers` once
    its own claim is released."""
    from server.auth.grant_claims import admins_change
    from server.core.ch import get_client

    key = (cfg.org, cfg.namespace)
    if key in _DONE:
        if sync and key not in _SYNCED:
            await sync_maintainers(cfg, ch or get_client())
        return False
    client = ch or get_client()
    try:
        # Under the claim `code.repo.set_access` restricts under, whose reads are
        # sequential: a stale read must not open, widen or recreate a restricted row.
        async with admins_change(cfg.org, cfg.namespace):
            inserted = await _write_row(cfg, client)
        _DONE.add(key)
        if sync:
            await sync_maintainers(cfg, client)
        return inserted
    except Exception as e:  # noqa: BLE001 — the next ingest pass retries; indexing must not stop
        logger.warning(
            "code namespace registration skipped for %s/%s: %s", cfg.org, cfg.namespace, e
        )
        return False


async def _write_row(cfg: Any, client: Any) -> bool:
    """`ensure_registered`'s directory write; True when a row was inserted."""
    from server.identity.app.namespace_classification import note_restricted
    from server.identity.app.registration import namespace_row

    restricted = cfg.classification == "restricted"
    public = cfg.public and not restricted
    # SELECT * so a narrowing re-insert mirrors EVERY column forward
    # (ReplacingMergeTree: a partial row resets what it omits).
    row = await namespace_row(cfg.org, cfg.namespace, ch=client)
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    if row is None:
        await client.insert(
            "loom.namespaces",
            [
                {
                    "org": cfg.org,
                    "namespace": cfg.namespace,
                    "owner_email": "",
                    "description": describe(cfg),
                    "tags": ["code"],
                    "active": True,
                    "kind": "code",
                    # Every code row is visibility=private. Discovery does not depend on
                    # it (the Repositories page and namespace.browse list code rows by
                    # kind), and `public` would let authz read the index without a join.
                    # join_policy alone says how a person gets in.
                    "visibility": "private",
                    "classification": "restricted" if restricted else "standard",
                    "join_policy": "open" if public and not restricted else "approval",
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
        if restricted:
            note_restricted(cfg.org, cfg.namespace)
        logger.info(
            "registered code namespace %s/%s (kind=code, classification=%s)",
            cfg.org,
            cfg.namespace,
            "restricted" if restricted else "standard",
        )
        return True
    classification = str(row.get("classification") or "standard")
    if public and classification == "standard" and str(row.get("join_policy")) != "open":
        # The config says the repository is public; the row is brought to open +
        # discoverable. Open means joinable, not readable — the row stays
        # visibility=private; reads need the join.
        opened = dict(row)
        opened.update(join_policy="open", visibility="private", updated_at=now)
        await client.insert("loom.namespaces", [opened])
        logger.info(
            "code namespace %s/%s directory row set open (config says public)",
            cfg.org,
            cfg.namespace,
        )
    if restricted and classification != "restricted":
        narrowed = dict(row)
        narrowed["classification"] = "restricted"
        narrowed["visibility"] = "private"
        narrowed["updated_at"] = now
        await client.insert("loom.namespaces", [narrowed])
        note_restricted(cfg.org, cfg.namespace)
        logger.warning(
            "code namespace %s/%s directory row narrowed to restricted "
            "(config says so; the row said %s)",
            cfg.org,
            cfg.namespace,
            row.get("classification"),
        )
    if not restricted and cfg.access_source == "registry" and classification == "restricted":
        if await _access_still_standard(cfg, client):
            # A maintainer opened the repository; the directory follows the access
            # row, not the entry's default
            widened = dict(row)
            widened["classification"] = "standard"
            widened["join_policy"] = "open" if public else "approval"
            widened["updated_at"] = now
            await client.insert("loom.namespaces", [widened])
            logger.warning(
                "code namespace %s/%s directory row widened to standard (a maintainer's "
                "access row says so; the row said restricted)",
                cfg.org,
                cfg.namespace,
            )
    return False


async def _access_still_standard(cfg: Any, client: Any) -> bool:
    """The access row read now still says standard: this process's snapshot of it
    may predate a restriction."""
    from server.code.store.index import CodeRepo

    access = await CodeRepo(client).code_repo_row(cfg.org, cfg.namespace)
    return access is not None and str(access.get("classification")) != "restricted"


def reset_for_tests() -> None:
    _DONE.clear()
    _SYNCED.clear()
