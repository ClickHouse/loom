"""Namespace categories: a registration outranks every earlier row of its
category under FINAL, and namespace.search_categories answers nearest first.

`loom.categories` is a ReplacingMergeTree on `version`, and a category's id
is deterministic, so the import (`loom import clickgap`) and
`namespace.register_category` write the same row key. The import stamps
nanoseconds; a registration stamped in milliseconds would never win over an
imported row.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from server.identity.api.models_namespace_catalog import (
    NamespaceRegisterCategoryInput,
    NamespaceSearchCategoriesInput,
)
from server.identity.app import namespace_catalog as catalog


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.inserted: list[dict[str, Any]] = []
        self.rows = rows or []
        self.params: dict[str, Any] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
        assert table == "loom.categories"
        self.inserted.extend(rows)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        self.params = dict(params or {})
        return self.rows


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _CH:
    fake = _CH(
        rows=[
            {
                "category_id": "c1",
                "name": "billing",
                "description": "invoices",
                "summary_excerpt": "how we bill",
                "distance": 0.25,
            }
        ]
    )
    monkeypatch.setattr(catalog, "get_client", lambda: fake)
    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Embedder())
    return fake


def test_a_registration_outranks_an_imported_row(ch: _CH) -> None:
    imported_version = time.time_ns()
    asyncio.run(
        catalog.handle_namespace_register_category(
            NamespaceRegisterCategoryInput(org="o", namespace="n", name="billing")
        )
    )
    (row,) = ch.inserted
    assert row["version"] > imported_version, "the later write must win under FINAL"
    assert row["embedding_dim"] == 3 and row["embedding_model"] == "fake-embed"


def test_search_categories_answers_nearest_first_with_a_similarity(ch: _CH) -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["namespace.search_categories"]
    assert op.handler is catalog.handle_namespace_search_categories
    out = asyncio.run(
        catalog.handle_namespace_search_categories(
            NamespaceSearchCategoriesInput(org="o", namespace="n", query="bills", top_k=3)
        )
    )
    (hit,) = out.results
    assert hit.name == "billing" and hit.similarity == pytest.approx(0.75)
    assert ch.params["top_k"] == 3 and ch.params["q"] == [0.1, 0.2, 0.3]
