"""Register code namespaces in the namespace directory.

The code plane keys every row on (org, namespace); without a loom.namespaces row its
namespaces would be invisible to the dashboard's sidebar, un-mintable from Setup, and
indistinguishable from memory namespaces. Each ingest pass calls
ensure_registered() once per process per repo entry: if no directory row exists, insert
one with kind='code' and visibility='private' (read access stays grant-driven — see
docs), from the registry row read then, or the snapshot when there is none. An
existing row is re-inserted in full in three cases only, each deciding from the registry
row read again under the namespace's admins claim, as this process's snapshot of it may
predate a maintainer's change: a standard row not yet open is opened when an access row
newer than it says open, or with no access row when the operator's entry says public; a
row not restricted is narrowed when the entry is restricted and no access row says
otherwise; a restricted row is widened when a standard access row is newer than it (the
maintainer's own decision). A newer access row is required because
`code.repo.set_access` writes the directory row first. The entry's mirror and checkouts
are tightened by the ingest pass, not here.
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
        # sequential: a stale read must not open or widen a restricted row.
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


async def insert_first_row(cfg: Any, client: Any, *, restricted: bool, join_policy: str) -> None:
    """Write a code namespace's first directory row. The caller holds the namespace's
    admins claim, under which it found no row."""
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    row = {
        "org": cfg.org,
        "namespace": cfg.namespace,
        "owner_email": "",
        "description": describe(cfg),
        "tags": ["code"],
        "active": True,
        "kind": "code",
        # Every code row is visibility=private. Discovery does not depend on it (the
        # Repositories page and namespace.browse list code rows by kind), and `public`
        # would let authz read the index without a join. join_policy alone says how a
        # person gets in.
        "visibility": "private",
        "classification": "restricted" if restricted else "standard",
        "join_policy": join_policy,
        "created_at": now,
        "updated_at": now,
    }
    await client.insert("loom.namespaces", [row])


async def _write_row(cfg: Any, client: Any) -> bool:
    """`ensure_registered`'s directory write; True when a row was inserted."""
    from server.code.store.index import CodeRepo
    from server.identity.app.namespace_classification import note_restricted
    from server.identity.app.registration import namespace_row

    restricted = cfg.classification == "restricted"
    public = cfg.public and not restricted
    # SELECT * so a narrowing re-insert mirrors EVERY column forward
    # (ReplacingMergeTree: a partial row resets what it omits).
    row = await namespace_row(cfg.org, cfg.namespace, ch=client)
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    if row is None:
        # The registry row read now decides over this process's snapshot of it.
        access = await CodeRepo(client).code_repo_row(cfg.org, cfg.namespace)
        if access is not None:
            restricted = str(access.get("classification")) == "restricted"
            public = not restricted and str(access.get("join_policy")) == "open"
        join_policy = "open" if public else "approval"
        await insert_first_row(cfg, client, restricted=restricted, join_policy=join_policy)
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
    opening = public and classification == "standard" and str(row.get("join_policy")) != "open"
    narrowing = restricted and classification != "restricted"
    widening = not restricted and cfg.access_source == "registry" and classification == "restricted"
    if not (opening or narrowing or widening):
        return False
    # Each follows the registry row read now: this process's snapshot of it may
    # predate a maintainer's change. With no registry row the operator's entry decides.
    access = await CodeRepo(client).code_repo_row(cfg.org, cfg.namespace)
    access_restricted = access is not None and str(access.get("classification")) == "restricted"
    # A standard access row newer than the directory row: a maintainer's decision the
    # directory has not taken yet.
    follows_access = access is not None and not access_restricted and _newer(access, row)
    if access is None:
        access_open = str(cfg.source) != "registry"  # the operator's entry alone says public
    else:
        access_open = follows_access and str(access.get("join_policy")) == "open"
    if opening and access_open:
        # The access row newer than the row (or, with none, the operator's entry) says
        # public; the row is brought to open + discoverable. Open means joinable, not
        # readable — the row stays visibility=private; reads need the join.
        opened = dict(row)
        opened.update(join_policy="open", visibility="private", updated_at=now)
        await client.insert("loom.namespaces", [opened])
        logger.info(
            "code namespace %s/%s directory row set open (%s says public)",
            cfg.org,
            cfg.namespace,
            "the operator's entry" if access is None else "the access row",
        )
    if narrowing and (access is None or access_restricted):
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
    if widening and follows_access:
        # A maintainer opened the repository; the directory follows the access row,
        # written after it, not the entry's default
        widened = dict(row)
        widened["classification"] = "standard"
        widened["join_policy"] = str((access or {}).get("join_policy") or "approval")
        widened["updated_at"] = now
        await client.insert("loom.namespaces", [widened])
        logger.warning(
            "code namespace %s/%s directory row widened to standard (a maintainer's "
            "access row says so; the row said restricted)",
            cfg.org,
            cfg.namespace,
        )
    return False


def _newer(access: dict[str, Any], row: dict[str, Any]) -> bool:
    """The access row was written after the directory row: only then is it a decision
    the directory has not taken yet (`code.repo.set_access` writes the directory row
    first)."""
    written, seen = access.get("updated_at"), row.get("updated_at")
    return written is not None and seen is not None and written > seen


def reset_for_tests() -> None:
    _DONE.clear()
    _SYNCED.clear()
