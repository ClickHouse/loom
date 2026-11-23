"""Configured admission bounds apply to stored envelopes and selected identities."""

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from server.knowledge.app import mcp_contracts
from server.knowledge.domain.mcp_sources import (
    ConnectionEnvelope,
    MCPSourceContractError,
    ResourceIdentity,
    SourceContract,
)
from server.knowledge.domain.mcp_sources import encode_envelope as wire_envelope


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    values = SimpleNamespace(
        knowledge_mcp_envelope_max_bytes=1024,
        knowledge_mcp_selected_resources_max=1,
        knowledge_mcp_resource_id_max_bytes=8,
    )
    monkeypatch.setattr(mcp_contracts, "settings", values)
    return values


def contract(*identities: str) -> SourceContract:
    return SourceContract(
        version=1,
        slot="mcp:" + "a" * 32,
        generation="b" * 32 + "/1" + "c" * 31,
        profile_id="approved",
        profile_sha256="d" * 64,
        mode="snapshot",
        reader_policy="owner_only",
        resources=tuple(ResourceIdentity(type="resource", id=i) for i in identities),
    )


def test_selection_count_is_an_operator_bound(limits: SimpleNamespace) -> None:
    with pytest.raises(MCPSourceContractError, match="^MCP_SOURCE_CONTRACT_INVALID$"):
        mcp_contracts.encode_source(contract("first", "second"))


def test_resource_identity_bound_counts_utf8_bytes(limits: SimpleNamespace) -> None:
    mcp_contracts.encode_source(contract("é" * 4))
    with pytest.raises(MCPSourceContractError):
        mcp_contracts.encode_source(contract("é" * 5))


def test_reading_a_stored_selection_revalidates_current_limits(limits: SimpleNamespace) -> None:
    config, items = mcp_contracts.encode_source(contract("selected"))
    limits.knowledge_mcp_resource_id_max_bytes = 4
    with pytest.raises(MCPSourceContractError):
        mcp_contracts.decode_source(config, items)


def test_envelope_bytes_are_an_operator_bound_on_both_directions(limits: SimpleNamespace) -> None:
    envelope = ConnectionEnvelope(
        version=1,
        org="org",
        owner="owner",
        slot="mcp:" + "a" * 32,
        generation="b" * 32 + "/1" + "c" * 31,
        profile_id="approved",
        profile_sha256="d" * 64,
        endpoint="https://offline.invalid/mcp",
        audience="https://offline.invalid/audience",
        auth="bearer",
        bearer=SecretStr("t" * 1024),
    )
    raw = wire_envelope(envelope)
    assert 1024 < len(raw.encode()) < 32768
    with pytest.raises(MCPSourceContractError):
        mcp_contracts.encode_envelope(envelope)
    with pytest.raises(MCPSourceContractError):
        mcp_contracts.decode_envelope(raw)
