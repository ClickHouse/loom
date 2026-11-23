"""Operator approvals bind credentials and selection without granting publication."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
from dataclasses import FrozenInstanceError
from typing import Any

import httpx
import pytest

from server.clients.mcp_source import MCPLimits, MCPSourceClient, MCPToolMapping
from server.knowledge.app import mcp_profiles
from server.knowledge.app.mcp_profiles import (
    MAX_CATALOG_BYTES,
    MCPProfileCatalog,
    MCPProfileError,
    build_mcp_source_client,
)
from server.knowledge.domain.mcp_sources import ConnectionEnvelope, SourceContract

SLOT = "mcp:" + "a" * 32
GENERATION = "b" * 32 + "/0" + "c" * 31
ENDPOINT = "https://documents.example/mcp"
AUDIENCE = "https://documents.example/readers"


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("profile operations must not construct HTTP clients or open sockets")

    monkeypatch.setattr(httpx, "AsyncClient", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


async def access_check() -> None:
    raise AssertionError("access checks belong to actual client use")


def row(identity: str = "approved", **changes: Any) -> dict[str, Any]:
    result = {
        "id": identity,
        "label": "Document server",
        "auth": "none",
        "allowed_orgs": ["org-a"],
        "config": {"endpoint": ENDPOINT},
    }
    result.update(changes)
    return result


def catalog(*rows: dict[str, Any]) -> MCPProfileCatalog:
    return MCPProfileCatalog.parse(json.dumps({"version": 1, "profiles": rows}))


def envelope(profile: Any, **changes: Any) -> ConnectionEnvelope:
    payload = {
        "version": 1,
        "org": "org-a",
        "owner": "owner-a",
        "slot": SLOT,
        "generation": GENERATION,
        "profile_id": profile.metadata.id,
        "profile_sha256": profile.metadata.profile_sha256,
        "endpoint": ENDPOINT,
        "audience": None,
        "auth": "none",
    }
    payload.update(changes)
    return ConnectionEnvelope.model_validate(payload)


def selection(profile: Any, **changes: Any) -> SourceContract:
    payload = {
        "version": 1,
        "slot": SLOT,
        "generation": GENERATION,
        "profile_id": profile.metadata.id,
        "profile_sha256": profile.metadata.profile_sha256,
        "mode": "live",
        "reader_policy": "owner_only",
        "resources": ({"type": "resource", "id": "doc://selected"},),
    }
    payload.update(changes)
    return SourceContract.model_validate(payload)


def client(
    profile: Any, sealed: ConnectionEnvelope | None = None, **changes: Any
) -> MCPSourceClient:
    context = {
        "org": "org-a",
        "owner": "owner-a",
        "slot": SLOT,
        "generation": GENERATION,
        "validate_access": access_check,
    }
    context.update(changes)
    return build_mcp_source_client(profile, sealed or envelope(profile), **context)


def tool_mapping() -> dict[str, Any]:
    return {
        "name": "read_document",
        "argument_name": "document_id",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string", "maxLength": 128},
                "scope": {"type": "string", "maxLength": 128},
            },
            "required": ["document_id", "scope"],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "maxLength": 128},
                "text": {"type": "string", "maxLength": 1024},
            },
            "required": ["id", "text"],
            "additionalProperties": False,
        },
        "fixed_arguments": {"scope": "operator-scope"},
    }


def test_empty_catalog_has_no_eligible_profiles() -> None:
    empty = MCPProfileCatalog.parse()
    assert empty.page("org-a").items == ()
    assert empty.page("org-a").next_after is None
    assert catalog().sha256 == empty.sha256
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        empty.get("org-a", "missing")


def test_org_filter_precedes_paging_and_search() -> None:
    profiles = catalog(
        row("d", label="Other"),
        row("b", allowed_orgs=["org-b"]),
        row("c", label="DOCUMENT second"),
        row("a"),
        row("e", allowed_orgs=[]),
    )
    first = profiles.page("org-a", "document", limit=1)
    assert [p.id for p in first.items] == ["a"]
    assert first.next_after == "a"
    second = profiles.page("org-a", "document", after=first.next_after, limit=1)
    assert [p.id for p in second.items] == ["c"]
    assert second.next_after is None
    assert profiles.page("org-c").items == ()
    for identity in ("b", "e", "absent"):
        with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
            profiles.get("org-a", identity)


def test_org_wildcard_requires_explicit_operator_approval() -> None:
    profiles = catalog(row("all", allowed_orgs=["*"]), row("none", allowed_orgs=[]))
    assert [p.id for p in profiles.page("another-org").items] == ["all"]


@pytest.mark.parametrize(
    "changes",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"query": "x" * 257},
        {"after": "x" * 129},
        {"query": "secret\n"},
        {"org": ""},
    ],
)
def test_page_arguments_are_bounded(changes: dict[str, Any]) -> None:
    args = {"org": "org-a", **changes}
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        catalog(row()).page(**args)


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "[]",
        '{"version":1,"profiles":[],"secret":true}',
        '{"version":1,"version":1,"profiles":[]}',
        '{"version":true,"profiles":[]}',
        '{"version":2,"profiles":[]}',
        '{"version":1,"profiles":null}',
        '{"version":1,"profiles":[],"x":NaN}',
        '{"version":1,"profiles":[{"id":"secret","id":"other"}]}',
        b"\xff",
        b" " * (MAX_CATALOG_BYTES + 1),
    ],
)
def test_invalid_catalog_errors_never_echo_payload(payload: str | bytes) -> None:
    with pytest.raises(MCPProfileError) as caught:
        MCPProfileCatalog.parse(payload)
    assert str(caught.value) == "MCP_PROFILE_INVALID"
    assert "secret" not in repr(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": "secret"},
        {"id": ""},
        {"id": "x" * 129},
        {"label": "x" * 257},
        {"label": "secret\u0085"},
        {"allowed_orgs": "org-a"},
        {"allowed_orgs": ["org-a"] * 2},
        {"auth": "oauth"},
        {"allow_collection_copy": 1},
        {"approved_resource_ids": []},
        {"config": {"endpoint": ENDPOINT, "approved_resource_ids": ["doc://injected"]}},
        {"config": {"endpoint": ENDPOINT, "selection": []}},
        {"config": {"endpoint": ENDPOINT, "limits": {"max_results": True}}},
        {"config": {"endpoint": ENDPOINT, "limits": {"unknown": 1}}},
        {"config": {"endpoint": ENDPOINT, "limits": {"timeout_seconds": float("inf")}}},
        {"config": {"endpoint": ENDPOINT, "approved_tools": "read"}},
        {"config": {"endpoint": ENDPOINT, "read": {"name": "read"}}},
        {"config": {"endpoint": ENDPOINT, "bearer_audience": AUDIENCE}},
        {"auth": "bearer"},
    ],
)
def test_profile_shape_errors_are_redacted(change: dict[str, Any]) -> None:
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        catalog(row(**change))


def test_duplicate_ids_and_nested_duplicate_fields_refuse() -> None:
    with pytest.raises(MCPProfileError):
        catalog(row(), row())
    payload = json.dumps({"version": 1, "profiles": [row()]}).replace(
        '"endpoint":', '"endpoint":"secret","endpoint":'
    )
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        MCPProfileCatalog.parse(payload)


def test_profile_depth_nodes_and_schema_bytes_are_bounded() -> None:
    deep: Any = "secret"
    for _ in range(18):
        deep = [deep]
    for schema in ({"deep": deep}, {"nodes": [0] * 4100}, {"description": "x" * 16_384}):
        mapping = tool_mapping()
        mapping["input_schema"] = schema
        with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
            catalog(row(config={"endpoint": ENDPOINT, "read": mapping}))


def test_resource_profile_uses_real_defaults_and_opens_nothing() -> None:
    profile = catalog(row()).get("org-a", "approved")
    opened = client(profile)
    assert isinstance(opened, MCPSourceClient)
    assert opened.config.limits == MCPLimits()
    assert opened.config.approved_resource_ids == ()
    assert opened.config.read is None
    assert opened.credential is None
    assert opened.validate_access is access_check


def test_mapped_tool_profile_keeps_public_defaults_and_fixed_scope() -> None:
    mapping = tool_mapping()
    profile = catalog(
        row(
            config={
                "endpoint": ENDPOINT,
                "approved_tools": ["read_document"],
                "read": mapping,
            }
        )
    ).get("org-a", "approved")
    opened = client(profile, contract=selection(profile))
    actual = opened.config.read
    assert actual is not None
    defaults = MCPToolMapping("read_document", {}, {}, "document_id")
    assert (actual.items_path, actual.id_path, actual.title_path, actual.revision_path) == (
        defaults.items_path,
        defaults.id_path,
        defaults.title_path,
        defaults.revision_path,
    )
    assert actual.fixed_arguments == {"scope": "operator-scope"}
    assert opened.config.approved_resource_ids == ("doc://selected",)


def test_bearer_comes_only_from_decoded_envelope() -> None:
    profile = catalog(
        row(
            auth="bearer",
            config={
                "endpoint": ENDPOINT,
                "bearer_audience": AUDIENCE,
            },
        )
    ).get("org-a", "approved")
    sealed = envelope(profile, auth="bearer", audience=AUDIENCE, bearer="sealed-upstream-secret")
    opened = client(profile, sealed)
    assert opened.credential is not None
    assert opened.credential.token == "sealed-upstream-secret"
    assert opened.credential.audience == AUDIENCE
    assert "secret" not in repr(opened)


@pytest.mark.parametrize(
    "changes",
    [
        {"org": "org-b"},
        {"owner": "owner-b"},
        {"slot": "mcp:" + "d" * 32},
        {"generation": "e" * 32 + "/0" + "f" * 31},
        {"profile_id": "other"},
        {"profile_sha256": "0" * 64},
        {"endpoint": ENDPOINT + "/other"},
    ],
)
def test_wrong_envelope_binding_refuses_before_client_construction(
    changes: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = catalog(row()).get("org-a", "approved")
    sealed = envelope(profile, **changes)

    def refuse(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("bad binding reached the client constructor")

    monkeypatch.setattr(mcp_profiles, "MCPSourceClient", refuse)
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        client(profile, sealed)


def test_full_audience_and_auth_mode_match_exactly() -> None:
    bearer = catalog(
        row(
            auth="bearer",
            config={
                "endpoint": ENDPOINT,
                "bearer_audience": AUDIENCE,
            },
        )
    ).get("org-a", "approved")
    for sealed in (
        envelope(bearer),
        envelope(bearer, auth="bearer", audience=AUDIENCE + "/other", bearer="sealed-secret"),
    ):
        with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
            client(bearer, sealed)
    anonymous = catalog(row()).get("org-a", "approved")
    with pytest.raises(MCPProfileError):
        client(anonymous, envelope(anonymous, auth="bearer", audience=AUDIENCE, bearer="secret"))


@pytest.mark.parametrize(
    "changes",
    [
        {"slot": "mcp:" + "d" * 32},
        {"generation": "e" * 32 + "/0" + "f" * 31},
        {"profile_id": "other"},
        {"profile_sha256": "0" * 64},
    ],
)
def test_contract_must_match_current_binding(changes: dict[str, Any]) -> None:
    profile = catalog(row()).get("org-a", "approved")
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        client(profile, contract=selection(profile, **changes))


def test_decoded_models_and_callback_are_required() -> None:
    profile = catalog(row()).get("org-a", "approved")
    for args in (
        {"contract": selection(profile).model_dump()},
        {"validate_access": None},
    ):
        with pytest.raises(MCPProfileError):
            client(profile, **args)
    with pytest.raises(MCPProfileError):
        client(profile, envelope(profile).model_dump())  # type: ignore[arg-type]
    with pytest.raises(MCPProfileError):
        client(row(), envelope(profile))


def test_tampered_models_are_revalidated() -> None:
    profile = catalog(row()).get("org-a", "approved")
    bad_envelope = envelope(profile).model_copy(update={"bearer": "secret"})
    bad_selection = selection(profile).model_copy(update={"resources": ({"id": "injected"},)})
    for sealed, contract in ((bad_envelope, None), (envelope(profile), bad_selection)):
        with pytest.raises(MCPProfileError):
            client(profile, sealed, contract=contract)


def test_catalog_rejects_unsafe_schema_before_building() -> None:
    mapping = tool_mapping()
    mapping["input_schema"]["$ref"] = "https://unapproved.example/schema"
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        catalog(
            row(
                config={
                    "endpoint": ENDPOINT,
                    "approved_tools": ["read_document"],
                    "read": mapping,
                }
            )
        )


def test_offline_catalog_does_not_instantiate_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("catalog parsing or paging constructed a client")

    monkeypatch.setattr(mcp_profiles, "MCPSourceClient", refuse)
    profiles = catalog(row(config={"endpoint": "https://offline.invalid/mcp"}))
    assert profiles.page("org-a").items[0].id == "approved"


def test_snapshots_and_selected_scope_are_detached_between_users() -> None:
    raw = row(
        allowed_orgs=["org-a", "org-b"],
        config={
            "endpoint": ENDPOINT,
            "approved_tools": ["read_document"],
            "read": tool_mapping(),
        },
    )
    profile = catalog(raw).get("org-a", "approved")
    digest = profile.metadata.profile_sha256
    first = client(profile, contract=selection(profile))
    second_contract = selection(profile, resources=({"type": "resource", "id": "doc://other"},))
    second = client(
        profile,
        envelope(profile, org="org-b", owner="owner-b"),
        org="org-b",
        owner="owner-b",
        contract=second_contract,
    )
    raw["config"]["read"]["fixed_arguments"]["scope"] = "injected"
    detached = first.config
    assert detached.read is not None
    detached.read.fixed_arguments["scope"] = "injected"
    assert first.config.approved_resource_ids == ("doc://selected",)
    assert second.config.approved_resource_ids == ("doc://other",)
    for opened in (first, second, client(profile)):
        actual = opened.config.read
        assert actual is not None and actual.fixed_arguments == {"scope": "operator-scope"}
    assert profile.metadata.profile_sha256 == digest
    with pytest.raises(FrozenInstanceError):
        profile.metadata.label = "changed"  # type: ignore[misc]


def test_digest_is_canonical_and_changes_with_approval() -> None:
    original = row()
    reordered = dict(reversed(list(original.items())))
    assert catalog(original).sha256 == catalog(reordered).sha256
    changed = copy.deepcopy(original)
    changed["allow_collection_copy"] = True
    assert catalog(original).sha256 != catalog(changed).sha256
    assert catalog(original, row("second")).sha256 == catalog(row("second"), original).sha256


def test_collection_copy_policy_is_separate_from_publication_grants() -> None:
    denied = catalog(row()).get("org-a", "approved")
    with pytest.raises(MCPProfileError):
        client(denied, contract=selection(denied, reader_policy="collection_copy"))
    approved = catalog(row(allow_collection_copy=True)).get("org-a", "approved")
    opened = client(approved, contract=selection(approved, reader_policy="collection_copy"))
    assert opened.validate_access is access_check
    assert opened.config.approved_resource_ids == ("doc://selected",)
    with pytest.raises(MCPProfileError):
        client(approved, org="denied-org")


@pytest.mark.parametrize(
    "changes",
    [
        {"org": "org-b"},
        {"owner": "owner-b"},
        {"slot": "mcp:" + "d" * 32},
        {"generation": "e" * 32 + "/0" + "f" * 31},
    ],
)
def test_authenticated_expected_tuple_is_not_inferred_from_envelope(
    changes: dict[str, Any],
) -> None:
    profile = catalog(row(allowed_orgs=["org-a", "org-b"])).get("org-a", "approved")
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        client(profile, **changes)


@pytest.mark.parametrize(
    "change",
    [
        {"unknown": "secret"},
        {"input_schema": []},
        {"output_schema": None},
        {"fixed_arguments": []},
        {"items_path": None},
        {"id_path": "id"},
        {"text_path": ["x"] * 9},
        {"cursor_argument": 1},
        {"name": "secret\n"},
    ],
)
def test_mapping_shapes_reject_without_copying_schema_semantics(change: dict[str, Any]) -> None:
    mapping = {**tool_mapping(), **change}
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        catalog(row(config={"endpoint": ENDPOINT, "read": mapping}))


def test_profile_digest_covers_the_detached_canonical_snapshot() -> None:
    profile = catalog(row()).get("org-a", "approved")
    snapshot = json.loads(profile.config_snapshot)
    assert (
        profile.metadata.profile_sha256
        == hashlib.sha256(profile.config_snapshot.encode()).hexdigest()
    )
    assert snapshot["allow_collection_copy"] is False
    assert "approved_resource_ids" not in snapshot["config"]
    assert snapshot["config"]["limits"]["max_pages"] == MCPLimits().max_pages
    snapshot["config"]["endpoint"] = "https://injected.example/mcp"
    assert client(profile).config.endpoint == ENDPOINT


def test_both_approved_tool_mappings_use_the_common_client() -> None:
    read = tool_mapping()
    search = {**tool_mapping(), "name": "find_documents", "argument_name": "query"}
    search["input_schema"] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 128},
            "scope": {"type": "string", "maxLength": 128},
        },
        "required": ["query", "scope"],
        "additionalProperties": False,
    }
    profile = catalog(
        row(
            config={
                "endpoint": ENDPOINT,
                "approved_tools": ["read_document", "find_documents"],
                "read": read,
                "search": search,
                "limits": {"max_results": 2},
            }
        )
    ).get("org-a", "approved")
    opened = client(profile)
    assert opened.config.search is not None and opened.config.read is not None
    assert opened.config.search.name == "find_documents"
    assert opened.config.read.name == "read_document"
    assert opened.config.limits.max_results == 2


def test_oversized_selection_refuses_through_real_client_limits() -> None:
    profile = catalog(row(config={"endpoint": ENDPOINT, "limits": {"max_results": 1}})).get(
        "org-a", "approved"
    )
    two = selection(
        profile,
        resources=(
            {"type": "resource", "id": "doc://first"},
            {"type": "resource", "id": "doc://second"},
        ),
    )
    with pytest.raises(MCPProfileError, match="^MCP_PROFILE_INVALID$"):
        client(profile, contract=two)
