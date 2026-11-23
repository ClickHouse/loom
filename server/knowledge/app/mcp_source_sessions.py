"""Schema refusal stops its exact MCP binding without erasing retained content."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace

from server.clients.mcp_source import MCPSourceClient, MCPSourceError
from server.core.ch import own_write_reads
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.knowledge.domain.mcp_sources import is_mcp_slot
from server.knowledge.store import connected_accounts as store


async def quarantine_mapping(expected: ConnectedAccount) -> None:
    from server.knowledge.app.connected_accounts import account_lease

    if not is_mcp_slot(expected.kind):
        return
    async with account_lease(expected.org, expected.subject, expected.kind):
        with own_write_reads():
            current = await store.get(expected.org, expected.subject, expected.kind)
        if (
            current is None
            or not current.active
            or (current.org, current.subject, current.kind, current.connection_id)
            != (expected.org, expected.subject, expected.kind, expected.connection_id)
        ):
            return
        await store.put(replace(current, active=False))


@asynccontextmanager
async def checked_session(
    account: ConnectedAccount, client: MCPSourceClient
) -> AsyncIterator[MCPSourceClient]:
    try:
        async with client:
            yield client
    except MCPSourceError as error:
        if error.code == "schema":
            await quarantine_mapping(account)
        raise
