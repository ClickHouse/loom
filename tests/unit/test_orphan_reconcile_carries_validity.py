"""An orphan memory's reconciled embedding row keeps the memory's
`invalid_at` and current attestation: a default there would serve a
superseded fact as current and drop an attested row's trust."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.store.sweeps import find_memories_without_embedding_rows


class _CH:
    def __init__(self) -> None:
        self.sql: list[str] = []
        key = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
        self.returns: list[list[dict[str, Any]]] = [[{**key, "memory_key": "k"}], []]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.sql.append(sql)
        return self.returns.pop(0) if self.returns else []


@pytest.mark.asyncio
async def test_the_reconcile_select_carries_invalid_at_and_verified() -> None:
    ch = _CH()
    await find_memories_without_embedding_rows(batch_size=10, ch=ch)
    rows_sql = ch.sql[-1]
    assert "m.invalid_at" in rows_sql
    assert "AS verified" in rows_sql
    # verified means a CURRENT attestation, as memory.verify mirrors it
    assert "m.verified_sha256 = m.content_sha256" in rows_sql
