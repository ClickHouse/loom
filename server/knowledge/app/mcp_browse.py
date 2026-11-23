"""Protected metadata windows and direct reads of saved live selections."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from server.auth import authz
from server.auth.answer_tokens import is_answer_token
from server.auth.context import current_auth
from server.auth.jwt import AuthInfo
from server.auth.session_protection import protected_for
from server.core import request_tail, request_work
from server.core.diagnostics import private_work
from server.knowledge.api.models_mcp_connections import (
    MCPBrowseInput,
    MCPBrowseItem,
    MCPBrowseOutput,
)
from server.knowledge.api.models_mcp_reads import MCPReadInput, MCPReadOutput
from server.knowledge.app import mcp_connections as connections
from server.knowledge.app import mcp_live, session_admission
from server.knowledge.app.mcp_source_sessions import checked_session
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.store import connected_accounts as account_store
from server.knowledge.store import sources as source_store
from server.operations.base import Role
from server.settings import settings

_LIMITATIONS = [
    "no_verified_remote_identity",
    "no_acl_or_permission_sync_proof",
    "no_deletion_or_complete_enumeration_proof",
    "no_import_or_publication_grant",
]


def _context(org: str) -> AuthInfo:
    auth = current_auth()
    if (
        auth is None
        or auth.org != org
        or not settings.auth_actually_required()
        or not settings.knowledge_mirrored_sources_enabled
        or not protected_for(auth)
        or (is_answer_token(auth) and not auth.answer_mirrors)
    ):
        raise connections._Refused("MCP_PROTECTED_SESSION_REQUIRED")
    return auth


async def _admit(org: str) -> AuthInfo:
    auth = _context(org)
    if not request_tail.required_active() or not request_work.mark_mirrored():
        raise connections._Refused("MCP_PROTECTED_SESSION_REQUIRED")
    await session_admission.require(auth)
    if _context(org) is not auth:
        raise connections._Refused("MCP_PROTECTED_SESSION_REQUIRED")
    return auth


@dataclass(repr=False)
class _Publication:
    org: str
    auth: AuthInfo
    validate: Callable[[], Awaitable[None]]
    deadline: float
    _checked: bool = False

    async def check(self) -> None:
        self._checked = False
        async with connections._errors():
            async with asyncio.timeout_at(self.deadline):
                if _context(self.org) is not self.auth:
                    raise connections._Refused("MCP_PUBLICATION_UNAVAILABLE")
                await session_admission.require(self.auth)
                await self.validate()
                if _context(self.org) is not self.auth:
                    raise connections._Refused("MCP_PUBLICATION_UNAVAILABLE")
                self._checked = True

    def fresh(self) -> bool:
        return self._checked and time.monotonic() < self.deadline


async def _retain(proof: _Publication) -> None:
    await proof.check()
    if not proof.fresh() or not request_tail.retain_publication(proof, proof.check, proof.fresh):
        raise connections._Refused("MCP_PUBLICATION_UNAVAILABLE")


def _hint(row: ConnectedAccount, resource_id: str) -> str:
    scope = [row.org, row.subject, row.kind, row.connection_id, resource_id]
    digest = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
    return "mcp-resource-hint:" + digest


async def _window(input: MCPBrowseInput, row: ConnectedAccount) -> MCPBrowseOutput:
    await connections._validate(row)
    client = connections._bound_client(row)
    config = client.config
    if input.mode == "search" and config.search is None:
        result = MCPBrowseOutput(
            slot=row.kind,
            generation=row.connection_id,
            mode=input.mode,
            resources=None,
            mapped_read=config.read is not None,
            mapped_search=False,
            reason="unsupported_mode",
            limitations=[*_LIMITATIONS, "remote_capabilities_unchecked"],
        )
        await connections._validate(row)
        return result
    async with checked_session(row, client):
        await connections._validate(row)
        discovered = await client.discover()
        await connections._validate(row)
        config = client.config
        result = MCPBrowseOutput(
            slot=row.kind,
            generation=row.connection_id,
            mode=input.mode,
            resources=discovered.resources,
            mapped_read=config.read is not None and config.read.name in discovered.tools,
            mapped_search=config.search is not None and config.search.name in discovered.tools,
            capabilities_checked=True,
            limitations=[*_LIMITATIONS, "first_window_only_manual_ids_supported"],
        )
        result.supported = (
            discovered.resources if input.mode == "resources" else result.mapped_search
        )
        if not result.supported:
            result.reason = "unsupported_mode"
        else:
            await connections._validate(row)
            page = (
                await client.list_resources()
                if input.mode == "resources"
                else await client.search(input.query)
            )
            await connections._validate(row)
            limit = min(input.limit, config.limits.max_results)
            result.items = [
                MCPBrowseItem(
                    id=item.resource_id, title=item.title, uri=_hint(row, item.resource_id)
                )
                for item in page.items[:limit]
            ]
            unknown = input.mode == "search" and (
                config.search is None or config.search.next_cursor_path is None
            )
            result.complete = page.complete and len(page.items) <= limit and not unknown
            result.truncated = not result.complete
            result.reason = (
                "result_limit"
                if len(page.items) > limit
                else "first_window_only"
                if not page.complete
                else "search_completeness_unavailable"
                if unknown
                else None
            )
    await connections._validate(row)
    return result


async def handle_mcp_browse(input: MCPBrowseInput) -> MCPBrowseOutput:
    async with connections._errors():
        with private_work():
            auth = await _admit(input.org)
            if (
                (input.mode == "resources" and input.query)
                or (input.mode == "search" and not input.query.strip())
                or len(input.query.encode()) > 4096
            ):
                raise connections._Refused("MCP_BROWSE_QUERY_INVALID")
            deadline = time.monotonic() + settings.knowledge_live_search_timeout_s
            async with asyncio.timeout_at(deadline):
                owner = await connections._owner(input.org)
                connections._check_slot(input.slot)
                row = await account_store.get(input.org, owner, input.slot)
                if row is None:
                    raise connections._Refused("MCP_CONNECTION_UNAVAILABLE")
                connections._row_scope(row, input.org, owner, input.slot)
                result = await _window(input, row)

                async def validate() -> None:
                    await connections._validate(row)

                await _retain(_Publication(input.org, auth, validate, deadline))
                return result


async def handle_mcp_read(input: MCPReadInput) -> MCPReadOutput:
    async with connections._errors():
        with private_work():
            auth = await _admit(input.org)
            deadline = time.monotonic() + settings.knowledge_live_search_timeout_s
            async with asyncio.timeout_at(deadline):
                if not await authz.row_in_scope(input.org, input.namespace, Role.VIEWER):
                    raise connections._Refused("MCP_SOURCE_UNAVAILABLE")
                src = await source_store.get_source(input.org, input.namespace, input.name)
                if src is None or (src.org, src.namespace, src.name) != (
                    input.org,
                    input.namespace,
                    input.name,
                ):
                    raise connections._Refused("MCP_SOURCE_UNAVAILABLE")
                evidence, contribution = await mcp_live.read_selected(src, auth, input.resource_id)
                await _retain(_Publication(input.org, auth, contribution.check, deadline))
                receipt = evidence.receipt
                if receipt is None:
                    raise connections._Refused("MCP_SOURCE_UNAVAILABLE")
                return MCPReadOutput(
                    source_id=src.source_id,
                    resource_id=input.resource_id,
                    uri=resource_uri(src.source_id, input.resource_id),
                    title=evidence.title,
                    text=evidence.text,
                    mime_type=evidence.mime_type,
                    resource_identity_verified=receipt.identity_verified,
                    limitations=_LIMITATIONS,
                )
