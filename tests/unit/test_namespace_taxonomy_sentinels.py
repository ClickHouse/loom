"""namespace.taxonomy leaves the write-path entity sentinels out of the
most-mentioned list: the graph page seeds its walk from
that list, and "_none_" has no neighbours."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.mark.asyncio
async def test_taxonomy_filters_sentinels_in_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.identity.api.models_namespace_catalog import NamespaceTaxonomyInput
    from server.identity.app import namespace_catalog as ns_handlers
    from server.memory.domain.entity_sentinels import SENTINEL_ENTITIES

    calls: list[tuple[str, dict[str, Any]]] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            calls.append((sql, params or {}))
            return []

    monkeypatch.setattr(ns_handlers, "get_client", lambda: _CH())
    await ns_handlers.handle_namespace_taxonomy(NamespaceTaxonomyInput(org="o", namespace="n"))
    sql, params = calls[0]
    assert "arrayFilter(x -> NOT has({sent:Array(String)}, x), entities)" in sql
    assert set(params["sent"]) == set(SENTINEL_ENTITIES)
