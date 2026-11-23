"""Generic source metadata survives lifecycle writes without execution authority."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.errors import ValidationError
from server.knowledge.app import sources as source_ops
from server.knowledge.domain.sources import KnowledgeSource, mirror_synced, served
from server.knowledge.store import sources


class Registry:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.queries: list[str] = []
        self.written: list[dict[str, Any]] = []

    async def query_one(self, query: str, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(query)
        return dict(self.row)

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert table == "loom.knowledge_sources"
        self.written.extend(dict(row) for row in rows)


def generic_row(version: int, config: str, items: str) -> dict[str, Any]:
    return {
        "org": "org-one",
        "namespace": "collection-one",
        "name": "selected-documents",
        "source_id": "source-one",
        "kind": "",
        "source_kind": "mcp",
        "credential_mode": "mcp",
        "access": "mirrored",
        "owner_subject": "owner-one",
        "connection_id": "a" * 32 + "/1" + "b" * 31,
        "connection_slot": "mcp:" + "c" * 32,
        "source_contract_version": version,
        "config_json": config,
        "items_json": items,
        "active": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "mode", "version"),
    [
        ("mcp", "mcp", 1),
        ("mcp", "mcp", 65535),
        ("mcp", "shared", 0),
        ("gdrive", "mcp", 0),
        ("gdrive", "shared", 2),
    ],
)
@pytest.mark.parametrize(
    ("config", "items"),
    [
        (
            ' { "version" : 77, "future": "opaque-sensitive-metadata" }\n',
            '[ { "type":"future-resource", "id":"opaque-selected-id" } ]\n',
        ),
        ("unparseable opaque-sensitive-metadata", "unparseable opaque-selected-id"),
    ],
)
async def test_lifecycle_roundtrip_preserves_unknown_payload_without_serving(
    kind: str, mode: str, version: int, config: str, items: str
) -> None:
    row = generic_row(version, config, items)
    row.update(source_kind=kind, credential_mode=mode)
    registry = Registry(row)
    source = await sources.latest_source(
        "org-one", "collection-one", "selected-documents", ch=registry
    )
    assert source is not None
    assert source.source_contract_version == version
    assert source.connection_slot == "mcp:" + "c" * 32
    assert source.generic_config_json == config
    assert source.generic_items_json == items
    assert not served(replace(source, access="namespace"))
    assert not mirror_synced(replace(source, access="mirrored"))
    assert "opaque-sensitive-metadata" not in repr(source)
    assert "opaque-selected-id" not in repr(source)
    assert "source_contract_version" in registry.queries[0]
    assert "connection_slot" in registry.queries[0]

    await sources.insert_source(replace(source, active=False, pending_delete=True), ch=registry)
    [written] = registry.written
    assert written["config_json"] == config
    assert written["items_json"] == items
    assert written["source_contract_version"] == version
    assert written["connection_slot"] == source.connection_slot
    assert written["source_id"] == source.source_id
    assert written["connection_id"] == source.connection_id
    assert written["owner_subject"] == source.owner_subject
    assert written["source_kind"] == kind
    assert written["kind"] == ""
    assert written["credential_mode"] == mode
    assert written["credential_enc"] == written["secret_enc"] == ""
    assert not written["active"]
    assert written["pending_delete"]


@pytest.mark.asyncio
async def test_native_source_still_uses_its_existing_codec() -> None:
    row = generic_row(0, "{}", '[{"type":"page","id":"native-page"}]')
    row.update(source_kind="notion", credential_mode="connected", connection_slot="")
    registry = Registry(row)
    source = await sources.latest_source(
        "org-one", "collection-one", "selected-documents", ch=registry
    )
    assert source is not None
    assert source.items == [{"type": "page", "id": "native-page"}]
    assert mirror_synced(source)
    await sources.insert_source(replace(source, active=False), ch=registry)
    [written] = registry.written
    assert written["config_json"] == "{}"
    assert written["items_json"] == '[{"id": "native-page", "type": "page"}]'
    assert written["source_kind"] == "notion"
    assert written["credential_mode"] == "connected"


def unsupported_source(kind: str, mode: str, version: int) -> KnowledgeSource:
    return KnowledgeSource(
        org="org-one",
        namespace="collection-one",
        name="selected-documents",
        source_id="source-one",
        kind=kind,
        credential_mode=mode,
        source_contract_version=version,
        access="mirrored",
        owner_subject="owner-one",
        connection_slot="mcp:" + "c" * 32,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "version", "allowed"), [("mcp", 0, False), ("gdrive", 2, False), ("gdrive", 0, True)]
)
async def test_opaque_details_stay_hidden(kind: str, version: int, allowed: bool) -> None:
    assert await source_ops._details_allowed(unsupported_source(kind, "shared", version)) is allowed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "mode", "version"),
    [("mcp", "mcp", 1), ("mcp", "shared", 0), ("gdrive", "shared", 2)],
)
async def test_native_management_lookup_refuses_opaque_format(
    monkeypatch: pytest.MonkeyPatch, kind: str, mode: str, version: int
) -> None:
    source = unsupported_source(kind, mode, version)

    async def found(*args: Any) -> KnowledgeSource:
        return source

    monkeypatch.setattr(source_ops.store, "get_source", found)
    with pytest.raises(ValidationError) as caught:
        await source_ops._source_or_404(source.org, source.namespace, source.name)
    assert caught.value.extra["code"] == "SOURCE_NOT_SERVED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "mode", "version"),
    [("mcp", "mcp", 1), ("mcp", "shared", 0), ("gdrive", "shared", 2)],
)
async def test_native_management_remove_refuses_before_retirement(
    monkeypatch: pytest.MonkeyPatch, kind: str, mode: str, version: int
) -> None:
    source = unsupported_source(kind, mode, version)

    async def found(*args: Any) -> KnowledgeSource:
        return source

    @asynccontextmanager
    async def lease(*args: Any) -> AsyncIterator[None]:
        yield

    async def administrator(*args: Any) -> bool:
        return True

    async def forbid_retirement(*args: Any) -> None:
        pytest.fail("native management reached retirement for an opaque source")

    monkeypatch.setattr(source_ops.store, "get_source", found)
    monkeypatch.setattr(source_ops, "sources_lease", lease)
    monkeypatch.setattr(source_ops.authz, "row_in_scope", administrator)
    monkeypatch.setattr(source_ops, "_queue_retirement", forbid_retirement)
    request = SimpleNamespace(
        org=source.org,
        namespace=source.namespace,
        name=source.name,
        confirm=source.name,
        delete_documents=False,
    )
    with pytest.raises(ValidationError) as caught:
        await source_ops.handle_knowledge_source_remove(request)
    assert caught.value.extra["code"] == "SOURCE_NOT_SERVED"
