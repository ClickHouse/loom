"""Canonical-owner connection standing, fencing and local credential erasure."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from server.auth import aliases, claims, revocation, scim
from server.auth.jwt import AuthInfo
from server.auth.reader_identity import reader_subject, resolve_reader
from server.core import leases
from server.core.ch import get_client, set_query_tag
from server.core.errors import AuthInsufficientError, ValidationError
from server.core.secrets_at_rest import unseal
from server.knowledge.domain.connected_accounts import (
    CREDENTIAL_PURPOSE,
    EMAIL_PURPOSE,
    ConnectedAccount,
    source_account_key,
    stopped,
)
from server.knowledge.store import connected_accounts as store
from server.knowledge.store import sources as source_store
from server.settings import settings

# KeeperMap's UInt32 epoch ceiling; a registration's fence never expires while it lives.
_FENCE_EXPIRY = 2**32 - 1


def unavailable() -> ValidationError:
    return ValidationError("the connected account is unavailable", code="CONNECTION_UNAVAILABLE")


class OwnerUnavailable(ValidationError):
    """Confirmed standing loss; config and unverifiable backend errors never use this."""

    def __init__(self) -> None:
        super().__init__("the connected account is unavailable", code="CONNECTION_UNAVAILABLE")


def _next_connection(previous: str = "", *, inactive: bool = False) -> str:
    key = previous.partition("/")[0] if previous else secrets.token_hex(16)
    # The generation's first nibble distinguishes a durable stop from a live checkout.
    generation = ("0" if inactive else "1") + secrets.token_hex(16)[1:]
    return key + "/" + generation


def _fence_key(connection_id: str) -> str:
    parts = connection_id.split("/")
    if len(parts) != 2 or any(
        len(p) != 32 or any(c not in "0123456789abcdef" for c in p) for p in parts
    ):
        raise unavailable()
    return "connected-account-generation:" + parts[0]


async def _fence_current(account: ConnectedAccount, *, ch: Any = None) -> bool:
    return (
        await claims.get(ch or get_client(), _fence_key(account.connection_id))
        == account.connection_id
    )


async def _require_source_fence(account: ConnectedAccount) -> None:
    value = await claims.get(get_client(), _fence_key(account.connection_id))
    if value == account.connection_id:
        return
    if value is None:
        raise unavailable()  # No verified generation proves only uncertainty.
    if _fence_key(value) != _fence_key(account.connection_id):
        raise unavailable()
    raise OwnerUnavailable()


async def _move_fence(previous: str | None, current: str, *, ch: Any = None) -> None:
    if previous is not None and _fence_key(previous) != _fence_key(current):
        raise unavailable()
    value = await claims.swap(
        ch or get_client(), _fence_key(current), previous, current, _FENCE_EXPIRY
    )
    if value != current:
        raise unavailable()


@asynccontextmanager
async def account_lease(
    org: str, subject: str, kind: str, *, ch: Any = None
) -> AsyncIterator[None]:
    key = hashlib.sha256(f"{org}\0{subject}\0{kind}".encode()).hexdigest()
    async with leases.cluster_lease(
        "connected-account:" + key,
        ttl_s=settings.knowledge_connected_lease_ttl_s,
        wait_s=settings.knowledge_connected_lease_wait_s,
        ch=ch,
    ):
        yield


async def _standing(org: str, subject: str, since: int, keys: tuple[str, ...] = ()) -> None:
    if not settings.auth_actually_required():
        raise unavailable()
    identity = await resolve_reader(org, subject)
    if identity is None or identity.subject != subject:
        raise OwnerUnavailable()
    spellings = await aliases.verified_spellings(org, subject)
    if not await store.account_active(org, spellings):
        raise OwnerUnavailable()
    if await scim.refuses_subject(subject, org=org, also=tuple(sorted(identity.emails))):
        raise OwnerUnavailable()
    for at in (await revocation.cutoffs([*spellings, *keys])).values():
        if revocation.kills(at, since):
            raise OwnerUnavailable()


async def own_subject(auth: AuthInfo | None) -> tuple[str, str]:
    subject = reader_subject(auth)
    if (
        auth is None
        or not subject
        or auth.org in ("", "*")
        or auth.issued_at is None
        or auth.is_service_account
        or auth.reads_as_creator
    ):
        raise AuthInsufficientError("a personal signed-in identity is required")
    if auth.expires_at is not None and auth.expires_at <= time.time():
        raise unavailable()
    canonical = await aliases.canonical_subject(auth.org, subject)
    await _standing(
        auth.org,
        canonical,
        min((auth.issued_at, *auth.parent_iats)),
        (auth.subject, auth.token_id, auth.session_id, *auth.parents),
    )
    return auth.org, canonical


async def _stop_locked(account: ConnectedAccount, *, strict: bool = False) -> ConnectedAccount:
    from server.knowledge.app import source_push
    from server.knowledge.app import sources as source_ops

    set_query_tag("knowledge.connected_accounts.stop")
    ended = stopped(
        account,
        connection_id=_next_connection(account.connection_id, inactive=True),
        at=datetime.now(UTC),
    )
    key = _fence_key(account.connection_id)
    previous = await claims.get(get_client(), key)
    if previous != account.connection_id and previous is not None:
        if strict or not isinstance(previous, str):
            raise unavailable()
        if _fence_key(previous) != key or not previous.partition("/")[2].startswith("0"):
            raise unavailable()
        # Only a durable stop can repair its lost row write. A newer live checkout
        # belongs to another connection and must never be overwritten or revoked.
        ended = dataclasses.replace(ended, connection_id=previous)
    elif strict and previous != account.connection_id:
        raise unavailable()
    # A validated stopped fence already records this stop under the account lease.
    landed = previous
    if previous != ended.connection_id:
        landed = await claims.swap(get_client(), key, previous, ended.connection_id, _FENCE_EXPIRY)
    if landed != ended.connection_id:
        raise unavailable()
    await store.put(ended)
    account_key = account.org, account.subject, account.kind
    for source in await source_store.connected_sources(account.org, account.subject):
        binding = source_account_key(
            org=source.org,
            owner=source.owner_subject,
            kind=source.kind,
            mode=source.credential_mode,
            slot=source.connection_slot,
        )
        if binding != account_key:
            continue
        async with source_ops.sources_lease(source.org, source.namespace):
            current = await source_store.latest_source(source.org, source.namespace, source.name)
            if (
                current is None
                or current.source_id != source.source_id
                or source_account_key(
                    org=current.org,
                    owner=current.owner_subject,
                    kind=current.kind,
                    mode=current.credential_mode,
                    slot=current.connection_slot,
                )
                != account_key
            ):
                continue
            pending = await source_ops._queue_retirement(current)
            await source_push.retire(current)
            await source_store.insert_source(
                dataclasses.replace(
                    current,
                    active=False,
                    credential_enc="",
                    secret_enc="",
                    pending_delete=pending or current.pending_delete,
                )
            )
    # Disconnect retains only a canonical-owner marker. Historical provider identity
    # and secrets are physically removed, with a census independent of user_id scopes.
    ended = dataclasses.replace(ended, connected_at=None)
    await _erase_locked(ended, keep_marker=True)
    return ended


async def _erase_locked(account: ConnectedAccount, *, keep_marker: bool) -> None:
    from server.erasure.app.connected_accounts import require_erased, require_sources_erased
    from server.knowledge.app import sources as source_ops

    if (
        account.active
        or account.credential_enc
        or account.account_email_enc
        or await claims.get(get_client(), _fence_key(account.connection_id))
        != account.connection_id
    ):
        raise unavailable()
    if not keep_marker:
        for namespace in await source_store.connected_owner_namespaces(
            account.org, account.subject, account.kind
        ):
            async with source_ops.sources_lease(account.org, namespace):
                await source_store.erase_connected_owner(
                    account.org, namespace, account.subject, account.kind
                )
                await require_sources_erased(account.org, namespace, account.subject, account.kind)
    await store.erase(account.org, [account.subject], kind=account.kind)
    if keep_marker:
        await store.put(account)
    await require_erased(
        account.org,
        account.subject,
        account.kind,
        keep_connection=account.connection_id if keep_marker else None,
    )


async def _owner_cutoff_allows(
    owner: tuple[str, str, str], current: ConnectedAccount | None
) -> bool:
    spellings = await aliases.verified_spellings(owner[0], owner[1])
    cutoffs = await revocation.cutoffs(spellings, permanent=current is None)
    if current is not None:
        since = int(current.connected_at.timestamp()) if current.connected_at else 0
        return any(revocation.kills(row, since) for row in cutoffs.values())
    if not cutoffs:
        return False
    if any(row is None for row in cutoffs.values()):
        return True
    # A source-only generation has no trusted birth time to compare with a dated cutoff.
    raise unavailable()


async def stop_subjects(
    subjects: list[str],
    *,
    org: str | None = None,
    revoke: bool = True,
    erase_owner: bool = False,
    respect_cutoff: bool = False,
) -> None:
    """Fence local accounts for every covered spelling, including repeat/error tails.

    The lifecycle hook's revoke flag never dispatches provider HTTP. Upstream
    credential revocation requires the owner's provider administration tools.
    """
    set_query_tag("knowledge.connected_accounts.stop_subjects")
    async with asyncio.timeout(settings.knowledge_connected_stop_timeout_s):
        if erase_owner:
            from server.knowledge.app.connected_restore import erase_subjects

            await erase_subjects(
                subjects, org=org, respect_cutoff=respect_cutoff, revoke=revoke, ch=get_client()
            )
        else:
            found = await store.owned(subjects, org=org)
            if respect_cutoff:
                wanted = set(subjects)
                known = {(account.org, account.subject) for account in found}
                for owner_org, owner in await store.owner_identities(org=org):
                    if (owner_org, owner) in known:
                        continue
                    spellings = await aliases.verified_spellings(owner_org, owner)
                    if wanted.intersection(spellings):
                        # Verified ties are org-specific. Never canonicalize in one org
                        # and then use that identity to stop rows in another org.
                        found.extend(await store.owned([owner], org=owner_org))
            for account in found:
                async with account_lease(account.org, account.subject, account.kind):
                    current = await store.get(account.org, account.subject, account.kind)
                    if current is not None:
                        if respect_cutoff:
                            spellings = await aliases.verified_spellings(
                                current.org, current.subject
                            )
                            cutoffs = await revocation.cutoffs(spellings)
                            since = (
                                int(current.connected_at.timestamp()) if current.connected_at else 0
                            )
                            if not any(revocation.kills(row, since) for row in cutoffs.values()):
                                continue
                        await _stop_locked(current)


async def reseal_registry(
    column: str, purpose: str, *, dry_run: bool, ch: Any
) -> tuple[int, list[str]]:
    """Re-read under the same writer lease so resealing cannot revive a stopped secret."""
    from server.core.ch import set_query_tag
    from server.core.secrets_at_rest import resealed

    if (column, purpose) not in {
        ("credential_enc", CREDENTIAL_PURPOSE),
        ("account_email_enc", EMAIL_PURPOSE),
    }:
        raise ValueError("unknown connected account secret")
    set_query_tag("knowledge.connected_accounts.reseal")
    after, count, missing = ("", "", ""), 0, []
    while rows := await store.page(after, ch=ch):
        for account in rows:
            after = account.org, account.subject, account.kind
            async with account_lease(*after, ch=ch):
                current = await store.get(*after, ch=ch)
                if current is None:
                    continue
                if not await _fence_current(current, ch=ch):
                    continue
                raw = (
                    current.credential_enc
                    if column == "credential_enc"
                    else current.account_email_enc
                )
                if not raw:
                    continue
                if not unseal(purpose, raw, warn=False):
                    missing.append("/".join(after))
                elif (new := resealed(purpose, raw)) is not None:
                    count += 1
                    if not dry_run:
                        updated = (
                            dataclasses.replace(current, credential_enc=new)
                            if column == "credential_enc"
                            else dataclasses.replace(current, account_email_enc=new)
                        )
                        await store.put(updated, ch=ch)
    return count, missing
