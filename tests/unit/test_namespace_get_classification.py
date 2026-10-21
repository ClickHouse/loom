"""`namespace.get` says whether the namespace is restricted.

The dashboard hides settings that `namespace.register` would refuse; for a restricted
row it can only do that if it knows the row is restricted. The word rides the found
branch only — the transport's restricted gate has already admitted the caller to the
row — and a miss stays a bare `found=False`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.identity.api.models import NamespaceGetInput
from server.identity.app import namespace as ns_handler

_NOW = datetime(2026, 9, 28, 8, 0, 0)


def _row(classification: str) -> dict[str, Any]:
    return {
        "namespace": "n",
        "kind": "memory",
        "owner_email": "o@example.com",
        "description": "",
        "tags": [],
        "active": 1,
        "conversation_logging_enabled": 0,
        "capture_generation_io": None,
        "coverage_sweep": None,
        "consolidation_enabled": 0,
        "daily_cost_cap_usd": 0.0,
        "conflict_threshold": None,
        "retention_days": 0,
        "warm_days": 0,
        "visibility": "private",
        "requires_approval": 0,
        "join_policy": "inherit",
        "retrieval_defaults": "",
        "rerank_endpoint": "",
        "classify_endpoint": "",
        "default_conflict_policy": "",
        "proposal_policy": "",
        "classification": classification,
        "created_at": _NOW,
        "updated_at": _NOW,
    }


class _CH:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.sql = ""

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.sql = sql
        return self.row


async def _get(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any] | None) -> Any:
    ch = _CH(row)
    monkeypatch.setattr(ns_handler, "get_client", lambda: ch)

    async def _no_types(org: str, namespace: str) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("server.memory.app.namespace_types.list_type_configs", _no_types)

    async def _no_guide(org: str, namespaces: list[str]) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(ns_handler.namespace_guides, "summaries", _no_guide)
    out = await ns_handler.handle_namespace_get(NamespaceGetInput(org="o", namespace="n"))
    assert "toString(classification) AS classification" in ch.sql
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["standard", "restricted"])
async def test_the_row_classification_reaches_the_output(
    monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    out = await _get(monkeypatch, _row(word))
    assert out.found is True
    assert out.classification == word


@pytest.mark.asyncio
async def test_a_missing_namespace_says_nothing_about_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await _get(monkeypatch, None)
    assert out.found is False
    assert out.classification is None
    assert out.model_dump(exclude_none=True) == {"found": False, "namespace": "n"}
