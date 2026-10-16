"""An operator's cutoff (adopt, a bot replaced or disabled) is written beside
a subject's own revocation row, never over it: an outright revoke that lands
just before it still stands, in the snapshot and in the read minting uses."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.auth import revocation


class _Table:
    """loom.revoked_tokens: the newest row of each jti wins, as under FINAL."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.rows[r["jti"]] = r

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        keys = (params or {}).get("keys")
        return [r for k, r in self.rows.items() if keys is None or k in keys]


@pytest.fixture
def table(monkeypatch) -> _Table:
    from server.core import ch

    t = _Table()
    monkeypatch.setattr(ch, "get_client", lambda: t)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_revoked_before", {})
    return t


def test_an_outright_revoke_outlives_a_cutoff_written_after_it(table) -> None:
    asyncio.run(revocation.revoke("eve", reason="offboarded"))
    later = datetime.now(UTC) + timedelta(seconds=1)
    asyncio.run(
        revocation.cut_off("eve", issued_before=later, reason="sso adopt", revoked_by="ops")
    )
    asyncio.run(revocation.refresh())
    tomorrow = int(time.time()) + 86400
    assert revocation.is_revoked("eve", issued_at=tomorrow)
    assert asyncio.run(revocation.subjects_revoked(["eve"], issued_at=tomorrow))


def test_a_cutoff_alone_ends_only_earlier_tokens(table) -> None:
    cutoff = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    asyncio.run(revocation.cut_off("sa:bot", issued_before=cutoff, reason="r", revoked_by="ops"))
    asyncio.run(revocation.refresh())
    before, after = int(cutoff.timestamp()) - 1, int(cutoff.timestamp())
    assert revocation.is_revoked("sa:bot", issued_at=before)
    assert not revocation.is_revoked("sa:bot", issued_at=after)
    assert asyncio.run(revocation.subjects_revoked(["sa:bot"], issued_at=before))
    assert not asyncio.run(revocation.subjects_revoked(["sa:bot"], issued_at=after))
