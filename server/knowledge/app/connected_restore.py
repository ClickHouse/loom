"""Offline retirement of restored connection authority, before serving any copy."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from server.auth import claims
from server.core import ch as ch_module
from server.core.ch import set_query_tag
from server.core.secrets_at_rest import unseal
from server.erasure.app.connected_accounts import require_restore_erased
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import source_push
from server.knowledge.app import sources as source_ops
from server.knowledge.domain.connected_accounts import CREDENTIAL_PURPOSE, KINDS, ConnectedAccount
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import connected_accounts as account_store
from server.knowledge.store import sources as source_store
from server.settings import settings

Owner = tuple[str, str, str]
Registration = tuple[str, str, str, str]


async def offline_refusal(ch: Any) -> str:
    try:
        recent = await account_store.restore_workers_recent(ch=ch)
    except Exception:  # noqa: BLE001 — unverified worker standing refuses offline maintenance
        return "worker standing could not be verified; restore cleanup refused"
    if recent:
        return (
            "a worker reported a recent pass: stop every server and worker before "
            "connected-account restore cleanup"
        )
    return ""


def _include(
    groups: dict[Owner, set[str]], prefixes: dict[str, Owner], rows: list[Registration]
) -> None:
    for org, subject, kind, connection in rows:
        if (
            any(
                not isinstance(value, str) or not value or value != value.strip()
                for value in (org, subject, kind, connection)
            )
            or kind not in KINDS
        ):
            raise accounts.unavailable()
        key = accounts._fence_key(connection)
        if connection.partition("/")[2][0] not in "01":
            raise accounts.unavailable()
        owner = org, subject, kind
        if key in prefixes and prefixes[key] != owner:
            raise accounts.unavailable()
        prefixes[key] = owner
        groups[owner].add(connection)


async def _metadata(
    ch: Any, owner: Owner | None = None
) -> tuple[list[Registration], list[KnowledgeSource]]:
    keys = await account_store.restore_keys(owner=owner, ch=ch)
    sources = await source_store.connected_history(owner=owner, ch=ch)
    for source in sources:
        if any(
            not isinstance(value, str) or not value
            for value in (source.namespace, source.name, source.source_id)
        ):
            raise accounts.unavailable()
        keys.append((source.org, source.owner_subject, source.kind, source.connection_id))
    if owner is not None and any(row[:3] != owner for row in keys):
        raise accounts.unavailable()
    return keys, sources


async def _stop_key(connection: str, ch: Any) -> str:
    key = accounts._fence_key(connection)
    observed = await claims.get(ch, key)
    if observed is not None:
        if not isinstance(observed, str) or accounts._fence_key(observed) != key:
            raise accounts.unavailable()
        if observed.partition("/")[2][0] not in "01":
            raise accounts.unavailable()
    ended = (
        observed
        if observed is not None and observed.partition("/")[2].startswith("0")
        else accounts._next_connection(connection, inactive=True)
    )
    landed = await claims.swap(ch, key, observed, ended, accounts._FENCE_EXPIRY)
    if landed != ended:
        raise accounts.unavailable()
    return ended


async def _retire_history(sources: list[KnowledgeSource], ch: Any) -> None:
    seen = set()
    for source in sources:
        identity = source.org, source.namespace, source.source_id
        if identity in seen:
            continue
        seen.add(identity)
        async with source_ops.sources_lease(source.org, source.namespace):
            pending = await source_ops._queue_retirement(source)
            await source_push.retire(source)
            current = await source_store.latest_source(
                source.org, source.namespace, source.name, ch=ch
            )
            if (
                current is not None
                and current.source_id == source.source_id
                and current.credential_mode == "connected"
                and current.owner_subject == source.owner_subject
                and current.kind == source.kind
            ):
                await source_store.insert_source(
                    dataclasses.replace(
                        current,
                        active=False,
                        credential_enc="",
                        secret_enc="",
                        pending_delete=pending or current.pending_delete,
                    ),
                    ch=ch,
                )


async def _restore_owner(
    owner: Owner, groups: dict[Owner, set[str]], prefixes: dict[str, Owner], ch: Any
) -> tuple[ConnectedAccount, str] | None:
    async with accounts.account_lease(*owner, ch=ch):
        keys, sources = await _metadata(ch, owner)
        current = await account_store.get(*owner, ch=ch)
        if current is not None:
            keys.append((current.org, current.subject, current.kind, current.connection_id))
            if (current.org, current.subject, current.kind) != owner:
                raise accounts.unavailable()
        _include(groups, prefixes, keys)
        stopped: dict[str, str] = {}
        for connection in sorted(groups[owner]):
            key = accounts._fence_key(connection)
            if key not in stopped:
                stopped[key] = await _stop_key(connection, ch)
        credential = ""
        if current is not None and current.credential_enc:
            try:
                credential = unseal(CREDENTIAL_PURPOSE, current.credential_enc, warn=False) or ""
            except Exception:  # noqa: BLE001 — local deletion never depends on recovering a remote secret
                pass
        ended = ConnectedAccount(
            *owner, next(iter(stopped.values())), disconnected_at=datetime.now(UTC)
        )
        # Preserve a credential-free recovery marker until every historical source
        # id has durable retirement work and both raw-history censuses are empty.
        await account_store.put(ended, ch=ch)
        await _retire_history(sources, ch)
        await accounts._erase_locked(ended, keep_marker=False)
        return (current, credential) if current is not None else None


async def restore_all(*, apply: bool, ch: Any) -> None:
    """Maintenance for the configured deployment; no account survives a restore.

    Callers stop serving processes first. Recent worker activity always refuses,
    including when an unrelated recovery command accepts a live override.
    """
    if ch is not ch_module.get_client():
        raise RuntimeError("connected_restore_target_mismatch")
    set_query_tag("knowledge.connected_accounts.restore")
    why = await offline_refusal(ch)
    if why:
        raise RuntimeError(why)
    groups: dict[Owner, set[str]] = defaultdict(set)
    prefixes: dict[str, Owner] = {}
    keys, _sources = await _metadata(ch)
    _include(groups, prefixes, keys)
    if not apply:
        return
    revocations = []
    async with asyncio.timeout(settings.knowledge_connected_stop_timeout_s):
        for owner in sorted(groups):
            why = await offline_refusal(ch)
            if why:
                raise RuntimeError(why)
            if ended := await _restore_owner(owner, groups, prefixes, ch):
                revocations.append(ended)
        await require_restore_erased(ch=ch)
    deadline = time.monotonic() + settings.knowledge_connected_revoke_timeout_s
    for account, credential in revocations:
        await accounts._revoke(account, credential=credential, deadline=deadline)
