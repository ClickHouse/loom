"""Observed read-mapping drift cannot leave a connection's old copy proofs usable."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from mcp import types

from server.core.errors import ValidationError
from server.knowledge.api.models_mcp_connections import MCPSlotInput
from server.knowledge.app import mcp_connections
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app import mcp_source_sessions as sessions
from tests.unit.test_mcp_source_sync import RESOURCE, TOOL_INPUT, TOOL_OUTPUT, execute
from tests.unit.test_mcp_source_sync import harness as harness
from tests.unit.test_mcp_source_sync import storage as storage
from tests.unit.test_mcp_source_sync import upstream as upstream


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["tool"], indirect=True)
async def test_observed_mapping_drift_quarantines_without_erasing_credentials_or_copies(
    storage, upstream, monkeypatch
):
    h = storage
    h.select(mode="selected_refresh")
    assert (await execute(h)).complete
    before = h.account
    written = []

    async def put(account):
        written.append(account)
        h.account = account

    monkeypatch.setattr(authority.account_store, "put", put)

    @upstream._mcp_server.list_tools()
    async def drifted():
        schema = {**TOOL_INPUT, "description": "Unapproved mapping revision"}
        return [types.Tool(name="read_selected", inputSchema=schema, outputSchema=TOOL_OUTPUT)]

    result = await execute(h)
    assert not result.complete and result.status != "ok"
    assert len(written) == 1 and h.account == replace(before, active=False)
    assert h.account.credential_enc == before.credential_enc
    assert h.source.active and len(h.doc_rows) == len(h.originals) == 1
    assert h.reads == ["tool:" + RESOURCE]
    with pytest.raises(authority.MCPAuthorityError):
        await authority.validate_connection(h.source.org, h.source.owner_subject, h.contract)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"connection_id": "a" * 32 + "/0" + "d" * 31},
        {"active": False},
        {"org": "other"},
        {"subject": "other"},
        {"kind": "mcp:" + "f" * 32},
    ],
)
async def test_schema_quarantine_cannot_rewrite_a_changed_or_foreign_connection(
    harness, monkeypatch, change
):
    h = harness
    bound = await authority.validate_source(h.source)
    h.account = replace(h.account, **change)
    put = AsyncMock()
    monkeypatch.setattr(authority.account_store, "put", put)
    await sessions.quarantine_mapping(bound.connection.account)
    put.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["tool"], indirect=True)
async def test_owner_discovery_schema_refusal_stops_the_exact_connection(
    harness, upstream, monkeypatch
):
    before = harness.account
    monkeypatch.setattr(mcp_connections, "_owner", AsyncMock(return_value="owner"))
    monkeypatch.setattr(mcp_connections, "_validate", AsyncMock())
    monkeypatch.setattr(
        mcp_connections, "build_mcp_source_client", authority.build_mcp_source_client
    )

    @upstream._mcp_server.list_tools()
    async def drifted():
        return [types.Tool(name="read_selected", inputSchema={"type": "object"})]

    with pytest.raises(ValidationError):
        await mcp_connections.handle_mcp_discover(MCPSlotInput(org="org", slot=before.kind))
    assert harness.account == replace(before, active=False)
    assert not harness.reads and not harness.doc_rows and not harness.originals
