"""Canonical-owner connection lifecycle and source-bound credential authority.

Account changes precede namespace source leases. HTTP exchanges run outside both;
the connection identity and rotation reservation fence late completions.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import math
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, SupportsIndex, TypeVar, cast

from server.auth import aliases, authz, claims, revocation, scim
from server.auth.context import current_auth
from server.auth.jwt import AuthInfo
from server.auth.oauth_store import PENDING_TTL, ClickHouseStore, token_id
from server.auth.reader_identity import reader_subject, resolve_reader
from server.clients._checked_access import CheckedAccess
from server.clients.connected_oauth import Kind, OAuthClient, OAuthError, Tokens
from server.core import leases, shielded
from server.core.ch import get_client, own_write_reads, set_query_tag
from server.core.errors import AuthInsufficientError, ValidationError
from server.core.secrets_at_rest import seal, unseal
from server.knowledge.domain.connected_accounts import (
    CREDENTIAL_PURPOSE,
    EMAIL_PURPOSE,
    KINDS,
    PKCE_PURPOSE,
    ConnectedAccount,
    accepts_callback,
    refreshing,
    rotated,
    source_account_key,
    source_matches,
    stopped,
)
from server.knowledge.domain.sources import MIRRORED_ACCESS, KnowledgeSource, opaque_payload
from server.knowledge.store import connected_accounts as store
from server.knowledge.store import sources as source_store
from server.observability import audit
from server.operations.base import Role
from server.settings import settings

pending_store = ClickHouseStore()
_T = TypeVar("_T")
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


def client(kind: str) -> OAuthClient:
    if kind == "gdrive":
        ident, secret = settings.connect_gdrive_client_id, settings.connect_gdrive_client_secret
    elif kind == "notion":
        if not settings.connect_notion_read_content_only:
            raise unavailable()
        ident, secret = settings.connect_notion_client_id, settings.connect_notion_client_secret
    elif kind == "linear":
        ident, secret = settings.connect_linear_client_id, settings.connect_linear_client_secret
    else:
        raise unavailable()
    try:
        return OAuthClient(cast(Kind, kind), ident, secret, settings.public_base_url)
    except OAuthError:
        raise unavailable() from None


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


def _primary(auth: AuthInfo | None) -> AuthInfo:
    if (
        auth is None
        or not settings.auth_actually_required()
        or not auth.session_id
        or not auth.first_party
        or auth.is_service_account
        or auth.impersonated_by
        or auth.creator
        or auth.parents
        or auth.agent_principal
        or auth.read_user_id
        or auth.reads_as_creator
        or auth.issued_at is None
        or auth.org in ("", "*")
    ):
        raise AuthInsufficientError("a primary sign-in session is required")
    return auth


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


async def start(auth: AuthInfo | None, kind: str) -> tuple[str, str, str]:
    auth = _primary(auth)
    provider = client(kind)  # Missing configuration does no Keeper or provider work.
    seal(CREDENTIAL_PURPOSE, "sealable")
    org, subject = await own_subject(auth)
    state, binding = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48) if kind != "notion" else ""
    async with account_lease(org, subject, kind):
        current = await store.get(org, subject, kind)
        if current is None:
            current = ConnectedAccount(org, subject, kind, _next_connection())
            await _move_fence(None, current.connection_id)
            await store.put(current)
        if not await _fence_current(current):
            raise unavailable()
        expected = current.connection_id
        await pending_store.save_pending(
            "connect:" + token_id(state),
            {
                "kind": "connected_account",
                "provider": kind,
                "org": org,
                "subject": subject,
                "binding": token_id(binding),
                "session_id": auth.session_id,
                "token_id": auth.token_id,
                "issued_at": auth.issued_at,
                "expires_at": auth.expires_at,
                "expected_connection": expected,
                "client_id": provider.client_id,
                "redirect_uri": provider.redirect_uri,
                "verifier_enc": seal(PKCE_PURPOSE, verifier),
            },
            ttl=PENDING_TTL,
        )
    return provider.authorization_url(state, verifier=verifier or None), state, binding


async def _pending_standing(pending: dict[str, Any]) -> None:
    expires = pending.get("expires_at")
    if expires is not None and (not isinstance(expires, int) or expires <= time.time()):
        raise unavailable()
    await _standing(
        pending["org"],
        pending["subject"],
        pending["issued_at"],
        (pending["session_id"], pending["token_id"]),
    )


async def verified_callback_pending(
    kind: str, state: str, binding: str
) -> tuple[str, dict[str, Any]]:
    """Read browser-bound pending authority without consuming it or calling a provider."""
    provider = client(kind)
    if not state or len(state) > 256 or not binding:
        raise unavailable()
    key = "connect:" + token_id(state)
    pending = await pending_store.pending(key)
    if (
        pending is None
        or pending.get("kind") != "connected_account"
        or pending.get("provider") != kind
        or pending.get("client_id") != provider.client_id
        or pending.get("redirect_uri") != provider.redirect_uri
        or not isinstance(pending.get("org"), str)
        or not pending["org"]
        or not secrets.compare_digest(str(pending.get("binding") or ""), token_id(binding))
    ):
        raise unavailable()
    return key, pending


async def callback(kind: str, state: str, binding: str, code: str, *, denied: bool = False) -> None:
    deadline = time.monotonic() + settings.knowledge_connected_oauth_timeout_s
    async with asyncio.timeout_at(deadline):
        await _callback(kind, state, binding, code, denied=denied, deadline=deadline)


async def _callback(
    kind: str, state: str, binding: str, code: str, *, denied: bool, deadline: float
) -> None:
    provider = client(kind)
    key, pending = await verified_callback_pending(kind, state, binding)
    consumed = await pending_store.take_pending(key)
    if consumed is None or denied or not code or len(code) > 8192:
        raise unavailable()
    pending = consumed
    await _pending_standing(pending)
    org, subject = pending["org"], pending["subject"]
    async with account_lease(org, subject, kind):
        current = await store.get(org, subject, kind)
        if (
            current is None
            or not accepts_callback(current, pending["expected_connection"])
            or not await _fence_current(current)
        ):
            raise unavailable()
    verifier = unseal(PKCE_PURPOSE, pending["verifier_enc"], warn=False)
    if kind != "notion" and not verifier:
        raise unavailable()
    grant = await provider.exchange(code, verifier=verifier or None, deadline=deadline)
    old: ConnectedAccount | None = None
    old_credential = ""
    async with account_lease(org, subject, kind):
        await _pending_standing(pending)
        current = await store.get(org, subject, kind)
        if (
            current is None
            or not accepts_callback(current, pending["expected_connection"])
            or not await _fence_current(current)
        ):
            raise unavailable()
        # First fence the old registration, then all of its source ids, before replacing it.
        old = current
        old_credential = unseal(CREDENTIAL_PURPOSE, current.credential_enc, warn=False) or ""
        ended = await _stop_locked(current, strict=True)
        account = ConnectedAccount(
            org,
            subject,
            kind,
            _next_connection(ended.connection_id),
            account_id=grant.account_id,
            workspace_id=grant.workspace_id or "",
            scopes=grant.tokens.scopes,
            account_email_enc=seal(EMAIL_PURPOSE, grant.display_email or ""),
            credential_enc=seal(CREDENTIAL_PURPOSE, grant.tokens.refresh_token),
            active=True,
            connected_at=datetime.now(UTC),
        )
        await store.put(account)
        await _move_fence(ended.connection_id, account.connection_id)
    # Google's revoke ends the user's project grant, including a replacement for that
    # same provider identity. Such a reconnect keeps its grant and drops the old secret.
    if old is not None and (old.account_id, old.workspace_id) != (
        account.account_id,
        account.workspace_id,
    ):
        await _revoke(old, credential=old_credential, deadline=deadline)
    await audit.emit(
        "connection.connected",
        org=org,
        namespace="-",
        actor=subject,
        subject={"kind": kind, "subject": subject, "scopes": list(account.scopes)},
    )


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


async def _revoke(
    account: ConnectedAccount, *, credential: str, deadline: float | None = None
) -> None:
    if not credential or account.kind not in KINDS:
        return
    try:
        provider = client(account.kind)
        limit = time.monotonic() + settings.knowledge_connected_revoke_timeout_s
        deadline = min(deadline, limit) if deadline is not None else limit
        async with asyncio.timeout_at(deadline):
            if account.kind == "notion":
                # The provider revokes an access token, not a refresh token. Mint only
                # after local inactivation; the rotated pair is never persisted.
                tokens = await provider.refresh(
                    credential, scopes=account.scopes, deadline=deadline
                )
                await provider.revoke(tokens.access_token, refresh=False, deadline=deadline)
            else:
                await provider.revoke(credential, refresh=True, deadline=deadline)
    except (OAuthError, TimeoutError, ValidationError):
        return  # Local inactivation and cleared credentials are already durable.


async def disconnect(org: str, subject: str, kind: str) -> None:
    if kind not in KINDS:
        raise unavailable()
    set_query_tag("knowledge.connected_accounts.disconnect")
    async with asyncio.timeout(settings.knowledge_connected_stop_timeout_s):
        async with account_lease(org, subject, kind):
            account = await store.get(org, subject, kind)
            if account is None:
                account = ConnectedAccount(org, subject, kind, _next_connection())
                await _move_fence(None, account.connection_id)
            credential = unseal(CREDENTIAL_PURPOSE, account.credential_enc, warn=False) or ""
            await _stop_locked(account)
    await _revoke(account, credential=credential)
    await audit.emit(
        "connection.disconnected",
        org=org,
        namespace="-",
        actor=subject,
        subject={"kind": kind, "subject": subject},
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
    """Revocation callers pass every spelling they covered, including repeat/error tails."""
    set_query_tag("knowledge.connected_accounts.stop_subjects")
    ended = []
    async with asyncio.timeout(settings.knowledge_connected_stop_timeout_s):
        if erase_owner:
            from server.knowledge.app.connected_restore import erase_subjects

            ended = await erase_subjects(
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
                        credential = (
                            unseal(CREDENTIAL_PURPOSE, current.credential_enc, warn=False) or ""
                            if revoke
                            else ""
                        )
                        await _stop_locked(current)
                        ended.append((current, credential))
    if not revoke:
        return
    # All local owners and sources are fenced before the first slow provider call.
    deadline = time.monotonic() + settings.knowledge_connected_revoke_timeout_s
    for account, credential in ended:
        await _revoke(account, credential=credential, deadline=deadline)


async def _source_standing(source: KnowledgeSource, account: ConnectedAccount) -> None:
    from server.knowledge.app import sources as source_ops

    if opaque_payload(
        kind=source.kind, mode=source.credential_mode, version=source.source_contract_version
    ):
        raise unavailable()
    if (
        account.org != source.org
        or account.subject != source.owner_subject
        or account.kind != source.kind
    ):
        raise unavailable()
    if not account.active or account.connection_id != source.connection_id:
        raise OwnerUnavailable()
    if (
        source.credential_mode != "connected"
        or source.access != MIRRORED_ACCESS
        or not settings.auth_actually_required()
        or not settings.knowledge_mirrored_sources_enabled
        or not source.owner_subject
        or not source.active
        or not source_matches(
            account,
            org=source.org,
            owner=source.owner_subject,
            kind=source.kind,
            connection_id=source.connection_id,
        )
    ):
        raise unavailable()
    await _require_source_fence(account)
    since = int(account.connected_at.timestamp()) if account.connected_at else 0
    await _standing(source.org, source.owner_subject, since)
    # Invalidate the alias cache after an uncached verified tie read; the existing grant
    # contract then reads this person's current spellings rather than a departed alias.
    await aliases.verified_spellings(source.org, source.owner_subject)
    aliases.invalidate(source.org, [source.owner_subject])
    with own_write_reads():
        role = await authz.grant_role_for(source.org, source.namespace, source.owner_subject)
        current = await source_store.latest_source(source.org, source.namespace, source.name)
    if role is None or role < Role.WRITER:
        raise OwnerUnavailable()
    if (
        current is None
        or not current.active
        or current.source_id != source.source_id
        or current.connection_id != source.connection_id
        or current.owner_subject != source.owner_subject
        or current.kind != source.kind
        or current.credential_mode != "connected"
        or current.access != MIRRORED_ACCESS
        or current.items != source.items
        or current.config != source.config
        or opaque_payload(
            kind=current.kind,
            mode=current.credential_mode,
            version=current.source_contract_version,
        )
    ):
        raise unavailable()
    await source_ops._require_source_namespace(source.org, source.namespace)
    await source_ops._require_no_erasure(source.org, source.namespace)
    await _require_source_fence(account)


@asynccontextmanager
async def publication_guard(source: KnowledgeSource) -> AsyncIterator[None]:
    """Fence a durable publication against disconnect and source replacement.

    Provider and model waits precede this guard; only the bounded store commit holds
    both leases, in the same account-before-namespace order as local retirement.
    """
    from server.knowledge.app import sources as source_ops

    async with account_lease(source.org, source.owner_subject, source.kind):
        async with source_ops.sources_lease(source.org, source.namespace):
            account = await store.get(source.org, source.owner_subject, source.kind)
            if account is None:
                raise unavailable()
            await _source_standing(source, account)
            yield


async def _draft_standing(source: KnowledgeSource, account: ConnectedAccount) -> None:
    """A probe may use its owner's connection before the new source row exists."""
    from server.knowledge.app import sources as source_ops

    auth = current_auth()
    org, subject = await own_subject(auth)
    if (
        org != source.org
        or subject != source.owner_subject
        or source.credential_mode != "connected"
        or source.access != MIRRORED_ACCESS
        or not source.active
        or not settings.knowledge_mirrored_sources_enabled
        or not source_matches(
            account,
            org=org,
            owner=subject,
            kind=source.kind,
            connection_id=source.connection_id,
        )
        or not await _fence_current(account)
        or not await authz.row_in_scope(org, source.namespace, Role.WRITER)
    ):
        raise unavailable()
    await aliases.verified_spellings(org, subject)
    aliases.invalidate(org, [subject])
    with own_write_reads():
        role = await authz.grant_role_for(org, source.namespace, subject)
    if role is None or role < Role.WRITER:
        raise unavailable()
    await source_ops._require_source_namespace(org, source.namespace)
    await source_ops._require_no_erasure(org, source.namespace)
    if current_auth() is not auth or not await _fence_current(account):
        raise unavailable()


async def token_for(
    source: KnowledgeSource,
    *,
    deadline: float,
    pending: bool = False,
    request_guard: Callable[[], Awaitable[None]] | None = None,
) -> tuple[Tokens, ConnectedAccount]:
    """A source-bound, serialized rotation. Access material remains in memory only."""
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise unavailable()
    standing = _draft_standing if pending else _source_standing
    provider = client(source.kind)
    account: ConnectedAccount | None = None
    reservation = ""
    dispatched = False
    try:
        async with account_lease(source.org, source.owner_subject, source.kind):
            account = await store.get(source.org, source.owner_subject, source.kind)
            if account is None or account.refresh_id:
                raise unavailable()
            await standing(source, account)
            if request_guard is not None:
                await request_guard()
            credential = unseal(CREDENTIAL_PURPOSE, account.credential_enc, warn=False)
            if not credential:
                raise unavailable()
            reservation = secrets.token_hex(16)
            await store.put(refreshing(account, refresh_id=reservation))
        if request_guard is not None:
            await request_guard()
        dispatched = True
        tokens = await provider.refresh(credential, scopes=account.scopes, deadline=deadline)
        async with account_lease(account.org, account.subject, account.kind):
            current = await store.get(account.org, account.subject, account.kind)
            if current is None or (current.org, current.subject, current.kind) != (
                account.org,
                account.subject,
                account.kind,
            ):
                raise unavailable()
            # A returned rotation belongs to the owner, independently of the
            # presenter's namespace role or the continued existence of this source.
            since = int(current.connected_at.timestamp()) if current.connected_at else 0
            await _standing(current.org, current.subject, since)
            await _require_source_fence(current)
            try:
                updated = rotated(
                    current,
                    expected_connection=account.connection_id,
                    refresh_id=reservation,
                    credential_enc=seal(CREDENTIAL_PURPOSE, tokens.refresh_token),
                )
            except ValueError:
                raise unavailable() from None
            await store.put(updated)
    except BaseException:  # A lost rotating response cannot leave a reusable old secret.
        if account is not None and reservation:
            try:
                await shielded.shielded(
                    _abort_refresh(account, reservation, dispatched=dispatched),
                    name="connected refresh cleanup",
                )
            except Exception:  # noqa: BLE001 — keep the original failure; its reservation refuses reuse
                pass
        raise
    # Credential maintenance is complete. Delivery can still be refused without
    # discarding the owner's valid rotation or stopping unrelated sources.
    await standing(source, updated)
    if request_guard is not None:
        await request_guard()
    return tokens, updated


async def checked_for(
    source: KnowledgeSource, *, deadline: float, pending: bool = False
) -> CheckedAccess:
    """Worker and registration probes retain the exact owner/source generation."""
    tokens, account = await token_for(source, deadline=deadline, pending=pending)
    auth = current_auth() if pending else None

    async def validate() -> None:
        if pending and current_auth() is not auth:
            raise unavailable()
        current = await store.get(account.org, account.subject, account.kind)
        if current is None or current.connection_id != account.connection_id:
            raise unavailable()
        if pending:
            await _draft_standing(source, current)
        else:
            await _source_standing(source, current)

    return CheckedAccess(
        tokens.access_token, kind=cast(Kind, source.kind), deadline=deadline, validate=validate
    )


async def _abort_refresh(account: ConnectedAccount, reservation: str, *, dispatched: bool) -> None:
    async with asyncio.timeout(settings.knowledge_connected_stop_timeout_s):
        async with account_lease(account.org, account.subject, account.kind):
            current = await store.get(account.org, account.subject, account.kind)
            if (
                current is not None
                and current.connection_id == account.connection_id
                and current.refresh_id == reservation
            ):
                if dispatched:
                    await _stop_locked(current)
                else:
                    # No provider request could have consumed the old refresh token.
                    await store.put(dataclasses.replace(current, refresh_id=""))


class SourceAuthority:
    """An internal credential checkout bound to the selected source and requester."""

    __slots__ = ("source", "requester", "_account", "_access", "_auth", "_deadline", "_role")

    def __init__(
        self,
        source: KnowledgeSource,
        requester: str,
        account: ConnectedAccount,
        tokens: Tokens,
        auth: AuthInfo,
        deadline: float,
        *,
        required_role: Role = Role.VIEWER,
    ) -> None:
        self.source, self.requester = source, requester
        self._account, self._auth = account, auth
        self._deadline = deadline
        self._role = required_role
        self._access = CheckedAccess(
            tokens.access_token,
            kind=cast(Kind, source.kind),
            deadline=deadline,
            validate=self.validate,
        )

    def __repr__(self) -> str:
        return "SourceAuthority(<redacted>)"

    async def validate(self) -> None:
        if time.monotonic() >= self._deadline:
            raise unavailable()
        auth = current_auth()
        if auth is not self._auth:
            raise unavailable()
        org, subject = await own_subject(auth)
        if org != self.source.org or subject != self.requester:
            raise unavailable()
        if not await authz.row_in_scope(org, self.source.namespace, self._role):
            raise unavailable()
        current = await store.get(self._account.org, self._account.subject, self._account.kind)
        if current is None or current.connection_id != self._account.connection_id:
            raise unavailable()
        await _source_standing(self.source, current)
        await _requester_standing(self._auth, self.source, self.requester, self._role)

    def __reduce_ex__(self, protocol: SupportsIndex) -> Any:
        raise TypeError("a connected authority cannot be serialized")

    async def run(self, call: Callable[[CheckedAccess], Awaitable[_T]]) -> _T:
        async with asyncio.timeout_at(self._deadline):
            await self.validate()
            result = await call(self._access)
            await self.validate()
            return result


async def resolve(
    org: str,
    namespace: str,
    name: str,
    *,
    deadline: float,
    expected_source_id: str = "",
) -> SourceAuthority:
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise unavailable()
    auth = current_auth()
    actual_org, subject = await own_subject(auth)
    if actual_org != org or not await authz.row_in_scope(org, namespace, Role.VIEWER):
        raise unavailable()
    source = await source_store.get_source(org, namespace, name)
    if (
        source is None
        or source.owner_subject != subject
        or (expected_source_id and source.source_id != expected_source_id)
    ):
        raise unavailable()
    if auth is None:
        raise unavailable()

    async def requester_standing() -> None:
        await _requester_standing(auth, source, subject, Role.VIEWER)

    async with asyncio.timeout_at(deadline):
        tokens, account = await token_for(
            source, deadline=deadline, request_guard=requester_standing
        )
    return SourceAuthority(source, subject, account, tokens, auth, deadline)


async def _requester_standing(
    auth: AuthInfo, source: KnowledgeSource, requester: str, role: Role
) -> None:
    if current_auth() is not auth:
        raise unavailable()
    org, subject = await own_subject(auth)
    if (
        org != source.org
        or subject != requester
        or not await authz.row_in_scope(org, source.namespace, role)
        or current_auth() is not auth
    ):
        raise unavailable()


async def resolve_probe(source: KnowledgeSource, *, deadline: float) -> SourceAuthority:
    """An explicit administrative probe retains its caller's admin standing.

    This narrow control-plane exception never participates in live document checkout.
    """
    if not math.isfinite(deadline) or deadline <= time.monotonic():
        raise unavailable()
    auth = current_auth()
    org, subject = await own_subject(auth)
    if auth is None or org != source.org:
        raise unavailable()
    role = Role.WRITER if subject == source.owner_subject else Role.ADMIN

    async def requester_standing() -> None:
        await _requester_standing(auth, source, subject, role)

    async with asyncio.timeout_at(deadline):
        await requester_standing()
        tokens, account = await token_for(
            source, deadline=deadline, request_guard=requester_standing
        )
        await requester_standing()
    return SourceAuthority(source, subject, account, tokens, auth, deadline, required_role=role)


async def validate_indexed(source: KnowledgeSource, auth: AuthInfo | None) -> None:
    """Indexed content needs current owner authority, without minting an access token."""
    if auth is None or current_auth() is not auth:
        raise unavailable()
    # The source owner's standing is independent of which admitted reader matches
    # its stored ACL. Live provider checkout remains restricted by resolve().
    if auth.org != source.org:
        raise unavailable()
    if not await authz.row_in_scope(source.org, source.namespace, Role.VIEWER):
        raise unavailable()
    client(source.kind)
    account = await store.get(source.org, source.owner_subject, source.kind)
    if account is None or account.refresh_id:
        raise unavailable()
    await _source_standing(source, account)
    if not unseal(CREDENTIAL_PURPOSE, account.credential_enc, warn=False):
        raise unavailable()
    if current_auth() is not auth:
        raise unavailable()


async def available(account: ConnectedAccount) -> bool:
    """An owner-visible active projection must honor the same authoritative fence."""
    if not account.active or not account.credential_enc:
        return False
    try:
        client(account.kind)
        since = int(account.connected_at.timestamp()) if account.connected_at else 0
        await _standing(account.org, account.subject, since)
        return bool(
            unseal(CREDENTIAL_PURPOSE, account.credential_enc, warn=False)
        ) and await _fence_current(account)
    except Exception:  # noqa: BLE001 — unknown registration standing is never reported usable
        return False


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
