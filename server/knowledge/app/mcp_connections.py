"""Owner-bound MCP enrollment and checks using the connected-account lifecycle."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import jwt

from server.auth import claims, machines, scim
from server.auth.context import current_auth
from server.clients.mcp_source import MCPSourceClient
from server.core.ch import get_client
from server.core.cursors import decode_bound_cursor, encode_bound_cursor
from server.core.diagnostics import private_work
from server.core.errors import LoomServerError, ValidationError
from server.core.secrets_at_rest import seal, unseal
from server.knowledge.api.models_mcp_connections import (
    MCPConnectInput,
    MCPConnectionEntry,
    MCPConnectOutput,
    MCPDisconnectOutput,
    MCPDiscoverOutput,
    MCPInspectInput,
    MCPInspectOutput,
    MCPListInput,
    MCPListOutput,
    MCPSlotInput,
)
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app.mcp_contracts import decode_envelope, encode_envelope
from server.knowledge.app.mcp_profiles import (
    FrozenProfileDefinition,
    MCPProfileError,
    build_mcp_source_client,
    load_mcp_profile_catalog,
)
from server.knowledge.app.mcp_source_sessions import checked_session
from server.knowledge.domain.connected_accounts import CREDENTIAL_PURPOSE, ConnectedAccount
from server.knowledge.domain.mcp_sources import (
    ConnectionEnvelope,
    MCPSourceContractError,
    bind_envelope,
    is_mcp_slot,
)
from server.knowledge.store import connected_accounts as store
from server.settings import settings

_CURSOR_TIME = datetime(1970, 1, 1, tzinfo=UTC)


class _Refused(ValidationError):
    pass


@asynccontextmanager
async def _errors() -> AsyncIterator[None]:
    try:
        with private_work():
            yield
    except _Refused:
        raise
    except Exception as e:  # noqa: BLE001 — credentials and upstream errors must never escape
        del e
        raise _Refused("MCP_CONNECTION_UNAVAILABLE") from None


async def _owner(org: str, expected: str | None = None) -> str:
    actual_org, owner = await accounts.own_subject(current_auth())
    if actual_org != org or (expected is not None and owner != expected):
        raise _Refused("MCP_CONNECTION_UNAVAILABLE")
    return owner


async def _ready() -> None:
    if not await machines.fleet_at(get_client(), 117):
        raise _Refused("MCP_CONNECTION_FLEET_UNAVAILABLE")


def _policy() -> None:
    if not settings.knowledge_source_self_service_enabled:
        raise _Refused("MCP_CONNECTION_SELF_SERVICE_DISABLED")


def _slot(org: str, owner: str, request_id: str) -> str:
    nonce = str(UUID(request_id))
    raw = json.dumps([org, owner, nonce], separators=(",", ":"))
    return "mcp:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _check_slot(slot: str) -> None:
    if not is_mcp_slot(slot):
        raise _Refused("MCP_CONNECTION_UNAVAILABLE")


def _row_scope(account: ConnectedAccount, org: str, owner: str, slot: str | None = None) -> None:
    if (
        (account.org, account.subject) != (org, owner)
        or not is_mcp_slot(account.kind)
        or (slot is not None and account.kind != slot)
    ):
        raise _Refused("MCP_CONNECTION_UNAVAILABLE")


def _reject_local_bearer(value: str | None) -> None:
    if value is None:
        return
    try:
        claims = jwt.decode(value, options={"verify_signature": False})
    except jwt.PyJWTError:
        return
    # Rejection only: an unverified issuer never grants identity or upstream access.
    issuer = claims.get("iss")
    if isinstance(issuer, str) and issuer in settings.trusted_issuers():
        raise _Refused("MCP_CONNECTION_LOCAL_CREDENTIAL")


def _envelope(account: ConnectedAccount) -> ConnectionEnvelope:
    plaintext = unseal(CREDENTIAL_PURPOSE, account.credential_enc, warn=False)
    if not plaintext:
        raise _Refused("MCP_CONNECTION_UNAVAILABLE")
    envelope = decode_envelope(plaintext)
    bind_envelope(
        envelope,
        org=account.org,
        owner=account.subject,
        slot=account.kind,
        generation=account.connection_id,
        profile_id=envelope.profile_id,
        profile_sha256=envelope.profile_sha256,
        endpoint=envelope.endpoint,
        audience=envelope.audience,
    )
    return envelope


def _binding(account: ConnectedAccount) -> tuple[FrozenProfileDefinition, ConnectionEnvelope]:
    envelope = _envelope(account)
    profile = load_mcp_profile_catalog().get(account.org, envelope.profile_id)
    config = json.loads(profile.config_snapshot)["config"]
    bind_envelope(
        envelope,
        org=account.org,
        owner=account.subject,
        slot=account.kind,
        generation=account.connection_id,
        profile_id=profile.metadata.id,
        profile_sha256=profile.metadata.profile_sha256,
        endpoint=config["endpoint"],
        audience=config["bearer_audience"],
    )
    if profile.metadata.auth != envelope.auth:
        raise _Refused("MCP_CONNECTION_UNAVAILABLE")
    _reject_local_bearer(envelope.bearer.get_secret_value() if envelope.bearer else None)
    return profile, envelope


async def _validate(account: ConnectedAccount) -> None:
    await _owner(account.org, account.subject)
    await _ready()
    current = await store.get(account.org, account.subject, account.kind)
    if (
        current is None
        or not current.active
        or not current.credential_enc
        or current.refresh_id
        or current.connection_id != account.connection_id
        or current.credential_enc != account.credential_enc
        or (current.org, current.subject, current.kind)
        != (account.org, account.subject, account.kind)
        or not current.connection_id.partition("/")[2].startswith("1")
        or current.connected_at is None
        or not await accounts._fence_current(current)
    ):
        raise _Refused("MCP_CONNECTION_UNAVAILABLE")
    await accounts._standing(current.org, current.subject, int(current.connected_at.timestamp()))
    _binding(current)


def _bound_client(account: ConnectedAccount) -> MCPSourceClient:
    profile, envelope = _binding(account)

    async def validate_access() -> None:
        await _validate(account)

    return build_mcp_source_client(
        profile,
        envelope,
        org=account.org,
        owner=account.subject,
        slot=account.kind,
        generation=account.connection_id,
        validate_access=validate_access,
    )


async def _entry(account: ConnectedAccount) -> MCPConnectionEntry:
    entry = MCPConnectionEntry(
        slot=account.kind,
        generation=account.connection_id,
        state="unavailable" if account.active else "stopped",
    )
    if not account.active:
        return entry
    try:
        envelope = _envelope(account)
    except (ValueError, _Refused):
        return entry
    entry.label = envelope.label
    entry.profile_id, entry.profile_sha256 = envelope.profile_id, envelope.profile_sha256
    try:
        _binding(account)
    except _Refused:
        return entry
    except (MCPProfileError, MCPSourceContractError):
        entry.state = "approval_drift"
        return entry
    try:
        if account.connected_at is not None and not account.refresh_id:
            await accounts._standing(
                account.org, account.subject, int(account.connected_at.timestamp())
            )
            if account.connection_id.partition("/")[2].startswith(
                "1"
            ) and await accounts._fence_current(account):
                entry.state = "configured"
    except LoomServerError:
        pass
    return entry


def _new_account(input: MCPConnectInput, owner: str, slot: str) -> ConnectedAccount:
    profile = load_mcp_profile_catalog().get(input.org, input.profile_id)
    config = json.loads(profile.config_snapshot)["config"]
    generation = accounts._next_connection(slot[4:] + "/" + "0" * 32)
    envelope = ConnectionEnvelope(
        version=1,
        org=input.org,
        owner=owner,
        slot=slot,
        generation=generation,
        profile_id=profile.metadata.id,
        profile_sha256=profile.metadata.profile_sha256,
        endpoint=config["endpoint"],
        audience=config["bearer_audience"],
        auth=profile.metadata.auth,
        bearer=input.bearer,
        label=input.label,
    )
    _reject_local_bearer(input.bearer.get_secret_value() if input.bearer else None)
    account = ConnectedAccount(
        input.org,
        owner,
        slot,
        generation,
        active=True,
        connected_at=datetime.now(UTC),
        credential_enc=seal(CREDENTIAL_PURPOSE, encode_envelope(envelope)),
    )
    _bound_client(account)
    return account


async def _retry(input: MCPConnectInput, account: ConnectedAccount) -> MCPConnectOutput:
    await _validate(account)
    profile, envelope = _binding(account)
    if (
        input.profile_id != profile.metadata.id
        or input.label != envelope.label
        or input.bearer != envelope.bearer
    ):
        raise _Refused("MCP_CONNECTION_REQUEST_CONFLICT")
    _bound_client(account)
    return MCPConnectOutput(connection=await _entry(account))


async def handle_mcp_connect(input: MCPConnectInput) -> MCPConnectOutput:
    async with _errors():
        owner = await _owner(input.org)
        _policy()
        await _ready()
        slot = _slot(input.org, owner, input.request_id)
        async with scim.subject_leases([owner]):
            async with accounts.account_lease(input.org, owner, slot):
                await _owner(input.org, owner)
                _policy()
                await _ready()
                current = await store.get(input.org, owner, slot)
                if current is not None:
                    _row_scope(current, input.org, owner, slot)
                    return await _retry(input, current)
                account = _new_account(input, owner, slot)
                if (
                    await claims.get(get_client(), accounts._fence_key(account.connection_id))
                    is not None
                ):
                    raise _Refused("MCP_CONNECTION_INCOMPLETE")
                if (
                    await store.mcp_owner_active_count(
                        input.org,
                        owner,
                        maximum=settings.knowledge_mcp_connections_max_per_person,
                    )
                    >= settings.knowledge_mcp_connections_max_per_person
                ):
                    raise _Refused("MCP_CONNECTION_QUOTA")
                await _owner(input.org, owner)
                _policy()
                await _ready()
                _binding(account)
                # Reserve this nonce before a write can have an uncertain outcome.
                await accounts._move_fence(None, account.connection_id)
                await _owner(input.org, owner)
                _policy()
                await _ready()
                _binding(account)
                await store.put(account)
                await _validate(account)
                return MCPConnectOutput(connection=await _entry(account))


async def handle_mcp_list(input: MCPListInput) -> MCPListOutput:
    async with _errors():
        owner = await _owner(input.org)
        scope = ("knowledge.connection.mcp_list", input.org, owner, str(input.limit))
        after = ""
        if input.cursor is not None:
            anchor, after = decode_bound_cursor(input.cursor, scope)
            if anchor != _CURSOR_TIME:
                raise _Refused("MCP_CONNECTION_CURSOR_INVALID")
            _check_slot(after)
        rows = await store.mcp_owner_page(input.org, owner, after=after, limit=input.limit + 1)
        for row in rows:
            _row_scope(row, input.org, owner)
        entries = [await _entry(row) for row in rows[: input.limit]]
        cursor = (
            encode_bound_cursor(scope, _CURSOR_TIME, rows[input.limit - 1].kind)
            if len(rows) > input.limit
            else None
        )
        await _owner(input.org, owner)
        return MCPListOutput(connections=entries, next_cursor=cursor)


async def handle_mcp_inspect(input: MCPInspectInput) -> MCPInspectOutput:
    async with _errors():
        owner = await _owner(input.org)
        if bool(input.slot) == bool(input.request_id):
            raise _Refused("MCP_CONNECTION_SELECTION_INVALID")
        slot = _slot(input.org, owner, input.request_id) if input.request_id else input.slot
        _check_slot(slot)
        account = await store.get(input.org, owner, slot)
        if account is None:
            incomplete = False
            if input.request_id:
                key = accounts._fence_key(slot[4:] + "/" + "0" * 32)
                incomplete = await claims.get(get_client(), key) is not None
            return MCPInspectOutput(
                found=False, slot=slot, reconciliation="incomplete" if incomplete else "absent"
            )
        _row_scope(account, input.org, owner, slot)
        entry = await _entry(account)
        await _owner(input.org, owner)
        return MCPInspectOutput(
            found=True,
            slot=slot,
            connection=entry,
            reconciliation="incomplete" if entry.state == "unavailable" else "present",
        )


async def handle_mcp_discover(input: MCPSlotInput) -> MCPDiscoverOutput:
    async with _errors():
        owner = await _owner(input.org)
        _check_slot(input.slot)
        account = await store.get(input.org, owner, input.slot)
        if account is None:
            raise _Refused("MCP_CONNECTION_UNAVAILABLE")
        _row_scope(account, input.org, owner, input.slot)
        await _validate(account)
        client = _bound_client(account)
        async with checked_session(account, client):
            discovered = await client.discover()
            await _validate(account)
            config = client.config
            mapped_read = config.read is not None and config.read.name in discovered.tools
            mapped_search = config.search is not None and config.search.name in discovered.tools
            result = MCPDiscoverOutput(
                slot=account.kind,
                generation=account.connection_id,
                resources=discovered.resources,
                mapped_read=mapped_read,
                mapped_search=mapped_search,
                supported_reads=discovered.resources or mapped_read,
                limitations=[
                    "no_verified_remote_identity",
                    "no_acl_or_permission_sync_proof",
                    "no_deletion_or_complete_enumeration_proof",
                    "no_import_or_publication_grant",
                    "no_upstream_credential_revocation",
                ],
            )
        await _validate(account)
        return result


async def handle_mcp_disconnect(input: MCPSlotInput) -> MCPDisconnectOutput:
    async with _errors():
        owner = await _owner(input.org)
        _check_slot(input.slot)
        async with scim.subject_leases([owner]):
            async with accounts.account_lease(input.org, owner, input.slot):
                await _owner(input.org, owner)
                account = await store.get(input.org, owner, input.slot)
                if account is None:
                    return MCPDisconnectOutput(disconnected=False)
                _row_scope(account, input.org, owner, input.slot)
                async with asyncio.timeout(settings.knowledge_connected_stop_timeout_s):
                    await accounts._stop_locked(account)
                return MCPDisconnectOutput(disconnected=True)
