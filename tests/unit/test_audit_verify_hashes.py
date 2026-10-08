"""`verify_audit_hashes` walks the filtered window and counts rows whose
stored hash no longer matches their content; timestamps hash the same
whether the driver hands them back naive or tz-aware."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from server.observability import audit


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((sql, params or {}))
        return self.rows

    async def insert(self, table: str, rows: list[dict[str, Any]], **_kw: Any) -> None:
        self.rows.extend(rows)


@pytest.mark.asyncio
async def test_verify_counts_tampered_rows_and_applies_every_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH([])
    monkeypatch.setattr(audit, "get_client", lambda: ch)
    # A non-UTC aware ts and a naive one both land and verify.
    for ts in (
        datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone(timedelta(hours=2))),
        datetime(2026, 1, 2, 3, 4, 5, 678901),
    ):
        await audit.emit(
            "memory.set", org="o", namespace="n", actor="a", subject={"k": 1}, request_id="r", ts=ts
        )
    assert len(ch.rows) == 2 and all(audit.row_matches_hash(r) for r in ch.rows)
    naive = dict(ch.rows[0], ts=ch.rows[0]["ts"].astimezone(UTC).replace(tzinfo=None))
    assert audit.row_matches_hash(naive)
    ch.rows.append(dict(ch.rows[0], subject='{"k":2}'))  # content changed after hashing
    ch.calls.clear()
    assert await audit.verify_audit_hashes(org="o", namespace="n", partition_yyyymm=202601) == (
        3,
        1,
    )
    sql, params = ch.calls[0]
    assert "org = {org:String}" in sql and "namespace = {namespace:String}" in sql
    assert "toYYYYMM(ts) = {p:UInt32}" in sql
    assert params == {"org": "o", "namespace": "n", "p": 202601}
    ch.calls.clear()
    await audit.verify_audit_hashes()
    assert "WHERE" not in ch.calls[0][0] and ch.calls[0][1] == {}
