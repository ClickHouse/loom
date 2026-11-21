"""Collection counts read only the authorized page's current document metadata."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.identity.api.models_collections import CollectionListInput
from server.identity.app import collections
from server.knowledge.store import documents
from server.operations.base import Role
from tests.unit.ch_fake import RecordingCH


@pytest.mark.asyncio
@pytest.mark.parametrize("requested", [False, True])
async def test_counts_are_opt_in_and_scope_only_the_role_authorized_page(
    monkeypatch: pytest.MonkeyPatch, requested: bool
) -> None:
    async def tree(_org: str) -> Any:
        return SimpleNamespace(parent={"col-a": "", "col-b": "", "col-c": ""})

    async def readable(*_a: Any) -> list[str]:
        return ["col-a", "col-b", "col-c"]

    async def roles(_auth: Any, _org: str, names: list[str]) -> dict[str, Role | None]:
        assert names == ["col-a", "col-b"]
        return {"col-a": Role.VIEWER, "col-b": None}

    async def labels(_org: str, names: list[str]) -> dict[str, Any]:
        assert names == ["col-a"]
        return {}

    counted: list[tuple[str, list[str]]] = []

    async def counts(org: str, names: list[str]) -> dict[str, int]:
        counted.append((org, names))
        return {"col-a": 0}

    monkeypatch.setattr(collections, "current_auth", lambda: object())
    monkeypatch.setattr(collections.sharing, "tree", tree)
    monkeypatch.setattr(collections.sharing, "readable", readable)
    monkeypatch.setattr(collections.sharing, "roles", roles)
    monkeypatch.setattr(collections.namespaces_repo, "labels", labels)
    monkeypatch.setattr(collections, "document_counts", counts)
    out = await collections.handle_collection_list(
        CollectionListInput(org="o", limit=2, include_document_counts=requested)
    )
    assert [(r.namespace, r.document_count) for r in out.collections] == [
        ("col-a", 0 if requested else None)
    ]
    assert counted == ([("o", ["col-a"])] if requested else [])
    assert out.next_cursor == "col-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("postgres", [False, True])
async def test_counts_use_the_list_projection_on_both_authorities(
    monkeypatch: pytest.MonkeyPatch, postgres: bool
) -> None:
    from server.core import pg

    monkeypatch.setattr(pg, "enabled", lambda: postgres)
    ch = RecordingCH().on("FROM loom.documents FINAL", [{"namespace": "col-a", "n": 7}])
    counts = await documents.current_document_counts("o", ["col-a", "col-empty"], ch=ch)
    assert counts == {"col-a": 7, "col-empty": 0}
    assert len(ch.calls) == 1
    assert ch.calls[0].params == {"org": "o", "ns": ["col-a", "col-empty"], "lim": 2}
    assert "superseded_at IS NULL" in ch.calls[0].sql
    assert "GROUP BY namespace" in ch.calls[0].sql
    assert await documents.current_document_counts("o", [], ch=ch) == {}
    assert len(ch.calls) == 1
