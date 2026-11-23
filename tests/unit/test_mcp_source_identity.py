"""Opaque identities cannot change provenance syntax or collide across sources."""

import hashlib

import pytest

from server.knowledge.domain.mcp_identity import MCPIdentityError, resource_digest, resource_uri
from server.knowledge.domain.sources import mirror_uri_suffix

SOURCE = "abcd1234-1234-4234-8234-123456789abc"
OTHER_SOURCE = "abcd1234-1234-4234-8234-123456789abd"


@pytest.mark.parametrize(
    "identity",
    [
        "opaque-id",
        "https://example.test/resource?next=other#fragment",
        "../different/path",
        "id#" + OTHER_SOURCE,
        "file:///unused",
        "a" * 2048,
        "é" * 1024,
    ],
)
def test_address_preserves_exact_identity_and_existing_copy_suffix(identity: str) -> None:
    expected = hashlib.sha256(identity.encode()).hexdigest()
    assert resource_digest(identity) == expected
    assert resource_uri(SOURCE, identity) == "mcp-resource:" + expected + "#" + SOURCE
    assert resource_uri(SOURCE, identity).endswith(mirror_uri_suffix(SOURCE))
    assert resource_uri(OTHER_SOURCE, identity) != resource_uri(SOURCE, identity)


@pytest.mark.parametrize(
    "left,right",
    [("é", "e\u0301"), ("id", "ID"), ("a/b", "a%2Fb"), ("a#b", "a%23b")],
)
def test_opaque_identity_spellings_are_not_equated(left: str, right: str) -> None:
    assert resource_uri(SOURCE, left) != resource_uri(SOURCE, right)


@pytest.mark.parametrize("identity", ["", " ", "a\nb", "a\x00b", "a" * 2049, "é" * 1025])
def test_invalid_resource_identity_is_redacted(identity: str) -> None:
    with pytest.raises(MCPIdentityError) as failure:
        resource_uri(SOURCE, identity)
    assert str(failure.value) == "MCP_RESOURCE_IDENTITY_INVALID"


@pytest.mark.parametrize(
    "source",
    ["", SOURCE.upper(), SOURCE.replace("-", ""), "{" + SOURCE + "}", SOURCE + "#other"],
)
def test_only_canonical_stored_source_identity_is_accepted(source: str) -> None:
    with pytest.raises(MCPIdentityError) as failure:
        resource_uri(source, "private-resource")
    assert str(failure.value) == "MCP_RESOURCE_IDENTITY_INVALID"
