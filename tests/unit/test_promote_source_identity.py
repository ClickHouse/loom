"""memory.promote names its copy's source by the row it read, the identity an erasure
finds the copy by."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.api.models_lifecycle import MemoryPromoteInput
from server.memory.app import promote
from server.memory.store import MemoryRow, shared


@pytest.mark.asyncio
async def test_the_copy_names_the_row_it_was_read_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lookup by `agent="*"` reads one row; the copy names that row's identity, by
    which an erasure finds it, not the wildcard."""
    rows: list[dict[str, Any]] = []

    async def _get(*_a: Any, **_k: Any) -> MemoryRow:
        return MemoryRow(
            memory_id="11111111-1111-1111-1111-111111111111",
            org="o",
            namespace="n",
            agent="a",
            user_id="alice",
            session_id="s1",
            memory_key="k",
            memory_type="semantic",
            value="v",
            confidence=1.0,
            version=1,
            pinned=False,
            created_at=datetime(2026, 9, 12, tzinfo=UTC),
            expires_at=None,
        )

    async def _write(row: dict[str, Any]) -> None:
        rows.append(row)

    monkeypatch.setattr(memory_repo, "get_by_key", _get)
    monkeypatch.setattr(promote, "get_embedder", lambda: None)
    monkeypatch.setattr(shared, "insert_shared_row", _write)
    await promote.handle_memory_promote(
        MemoryPromoteInput(org="o", namespace="n", agent="*", memory_key="k")
    )
    assert rows[0]["promoted_from"] == ["o/n/a/alice/s1/k"]
