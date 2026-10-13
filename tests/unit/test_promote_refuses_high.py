"""memory.promote refuses a row labelled 'high': loom.shared has no sensitivity
column, so the promoted copy would be embedded and searchable namespace-wide."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory import store as memory_repo
from server.memory.api.models_lifecycle import MemoryPromoteInput
from server.memory.app import promote
from server.memory.store import MemoryRow, shared


def _row(sensitivity: str) -> MemoryRow:
    return MemoryRow(
        memory_id="11111111-1111-1111-1111-111111111111",
        org="o",
        namespace="n",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value="root token sk-proj-abc",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        expires_at=None,
        sensitivity=sensitivity,
    )


@pytest.mark.asyncio
async def test_promoting_a_high_row_is_refused_before_anything_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[Any] = []

    async def _get(*_a: Any, **_k: Any) -> MemoryRow:
        return _row("high")

    async def _write(*a: Any, **_k: Any) -> None:
        writes.append(a)

    monkeypatch.setattr(memory_repo, "get_by_key", _get)
    monkeypatch.setattr(shared, "insert_shared_row", _write)
    monkeypatch.setattr(shared, "insert_shared_embedding", _write)
    with pytest.raises(HTTPException) as e:
        await promote.handle_memory_promote(
            MemoryPromoteInput(org="o", namespace="n", agent="a", memory_key="k")
        )
    assert e.value.status_code == 422
    assert e.value.detail["code"] == "HIGH_SENSITIVITY_PROMOTION_REFUSED"
    assert writes == []
