"""Current stored MCP authority, shared by execution and commit-time fencing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

from server.auth import aliases, authz
from server.clients.mcp_source import MCPSourceClient
from server.core.ch import own_write_reads
from server.core.errors import ValidationError
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import mcp_connections, mirrors
from server.knowledge.app.mcp_contracts import decode_source
from server.knowledge.app.mcp_profiles import FrozenProfileDefinition, build_mcp_source_client
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.domain.mcp_sources import ConnectionEnvelope, SourceContract
from server.knowledge.domain.sources import MIRRORED_ACCESS, KnowledgeSource
from server.knowledge.store import connected_accounts as account_store
from server.knowledge.store import sources as source_store
from server.operations.base import Role
from server.settings import settings
from server.workers import pass_context


class MCPAuthorityError(ValidationError):
    def __init__(self) -> None:
        super().__init__("MCP_SOURCE_UNAVAILABLE")


@asynccontextmanager
async def _refusals() -> AsyncIterator[None]:
    try:
        yield
    except MCPAuthorityError:
        raise
    except Exception as e:  # noqa: BLE001 — authority failures reveal no source or credential data
        del e
        raise MCPAuthorityError() from None


@dataclass(frozen=True, repr=False)
class ConnectionAuthority:
    account: ConnectedAccount
    profile: FrozenProfileDefinition
    envelope: ConnectionEnvelope
    contract: SourceContract

    def client(self, validate_access: Callable[[], Awaitable[None]]) -> MCPSourceClient:
        return build_mcp_source_client(
            self.profile,
            self.envelope,
            org=self.account.org,
            owner=self.account.subject,
            slot=self.account.kind,
            generation=self.account.connection_id,
            contract=self.contract,
            validate_access=validate_access,
        )


async def validate_connection(
    org: str, owner: str, contract: SourceContract
) -> ConnectionAuthority:
    """Check stored owner authority without requiring a foreground human token.

    Registration must hold the subject/account/namespace locks its write needs.
    This check grants neither a destination nor permission to publish a source.
    """
    async with _refusals():
        contract = SourceContract.model_validate(contract)
        await mcp_connections._ready()
        with own_write_reads():
            account = await account_store.get(org, owner, contract.slot)
            if (
                account is None
                or (account.org, account.subject, account.kind) != (org, owner, contract.slot)
                or not account.active
                or not account.credential_enc
                or account.refresh_id
                or account.connection_id != contract.generation
                or account.connected_at is None
                or not contract.generation.partition("/")[2].startswith("1")
                or not await accounts._fence_current(account)
            ):
                raise MCPAuthorityError()
            await accounts._standing(org, owner, int(account.connected_at.timestamp()))
            profile, envelope = mcp_connections._binding(account)
            bound = ConnectionAuthority(account, profile, envelope, contract)

            async def checked() -> None:
                await validate_connection(org, owner, contract)

            # The builder checks selection limits and every contract/profile binding.
            bound.client(checked)
            return bound


def source_contract(src: KnowledgeSource) -> SourceContract:
    """Parse only the supported stored contract; no payload can widen its row."""
    if (
        src.kind != "mcp"
        or src.credential_mode != "connected"
        or type(src.source_contract_version) is not int
        or src.source_contract_version != 1
        or src.active is not True
        or src.pending_delete
        or not src.owner_subject
        or src.credential_enc
        or src.secret_enc
        or src.config
        or src.items
    ):
        raise MCPAuthorityError()
    contract = decode_source(src.generic_config_json, src.generic_items_json)
    if (contract.generation, contract.slot) != (src.connection_id, src.connection_slot):
        raise MCPAuthorityError()
    resource_uri(src.source_id, contract.resources[0].id)
    # No stored writer-consent proof exists for a deliberate collection copy.
    if contract.reader_policy != "owner_only" or src.access != MIRRORED_ACCESS:
        raise MCPAuthorityError()
    return contract


@dataclass(frozen=True, repr=False)
class SourceAuthority:
    source: KnowledgeSource
    connection: ConnectionAuthority

    @property
    def contract(self) -> SourceContract:
        return self.connection.contract

    @property
    def selection_sha256(self) -> str:
        payload = [
            self.source.org,
            self.source.namespace,
            self.source.name,
            self.source.source_id,
            self.source.owner_subject,
            self.source.connection_id,
            self.contract.model_dump(mode="json"),
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    async def validate(self) -> None:
        fresh = await validate_source(self.source)
        if fresh.connection.account != self.connection.account:
            raise MCPAuthorityError()

    def client(self) -> MCPSourceClient:
        return self.connection.client(self.validate)


async def validate_source(src: KnowledgeSource) -> SourceAuthority:
    """Recheck an exact active stored row and its current destination authority.

    This is a worker/source check, not a requesting reader's authorization. A live
    reader must separately prove its identity and access to this exact selection.
    """
    async with _refusals():
        with own_write_reads():
            current = await source_store.get_source(src.org, src.namespace, src.name)
            if current is None or replace(current, mirror="") != replace(src, mirror=""):
                raise MCPAuthorityError()
            connection = await validate_registration(src)
            if src.mirror and await mirrors.live_mirror(src.org, src.namespace) != src.mirror:
                raise MCPAuthorityError()
            return SourceAuthority(src, connection)


async def validate_registration(src: KnowledgeSource) -> ConnectionAuthority:
    """Check a proposed row's binding and destination, without granting execution.

    Registration owns human caller authorization and subject/account/namespace
    locks, then repeats this check immediately before its source insert. Execution
    still requires validate_source's exact current stored-row check.
    """
    from server.knowledge.app import sources as source_ops

    async with _refusals():
        contract = source_contract(src)
        if not settings.auth_actually_required() or not settings.knowledge_mirrored_sources_enabled:
            raise MCPAuthorityError()
        with own_write_reads():
            connection = await validate_connection(src.org, src.owner_subject, contract)
            await aliases.verified_spellings(src.org, src.owner_subject)
            aliases.invalidate(src.org, [src.owner_subject])
            role = await authz.grant_role_for(src.org, src.namespace, src.owner_subject)
            if role is None or role < Role.WRITER:
                raise MCPAuthorityError()
            await source_ops._require_source_namespace(src.org, src.namespace)
            await source_ops._require_no_erasure(src.org, src.namespace)
            return connection


@asynccontextmanager
async def source_guard(src: KnowledgeSource) -> AsyncIterator[SourceAuthority]:
    """Fence a stored source check or live result release; does not grant reader access."""
    from server.knowledge.app import sources as source_ops

    async with _refusals():
        contract = source_contract(src)
    async with accounts.account_lease(src.org, src.owner_subject, contract.slot):
        async with source_ops.sources_lease(src.org, src.namespace):
            pass_context.check_stop()
            bound = await validate_source(src)
            yield bound


@asynccontextmanager
async def publication_guard(src: KnowledgeSource) -> AsyncIterator[None]:
    """Hold account then namespace leases through the actual knowledge commit."""
    async with source_guard(src) as bound:
        if bound.contract.mode == "live" or not src.mirror:
            raise MCPAuthorityError()
        yield
