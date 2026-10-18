"""`revoke_created_by` reads a person's tokens' tokens in chunks: thousands of
creators in one array parameter overflow ClickHouse's HTTP field size, and
every retry of the deactivation would fail the same way."""

from __future__ import annotations

from typing import Any

import pytest

from server.auth import service_accounts

pytestmark = pytest.mark.asyncio


class _CH:
    def __init__(self, made: int) -> None:
        # The person made `made` tokens, all listed revoked already (a retry).
        self.rows = [
            {
                "org": "acme",
                "name": f"{i:064d}",  # the longest token name
                "sa_subject": f"sa:{i:064d}-abc123",
                "created_by": "lee",
                "created_at": None,
                "live": False,
            }
            for i in range(made)
        ]
        self.largest = 0

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        subs = params.get("subs") or params.get("keys") or []
        # The array as it is sent: one quoted, comma-separated string field.
        self.largest = max(self.largest, sum(len(s) + 3 for s in subs))
        if "loom.service_accounts" in sql:
            return [r for r in self.rows if r["created_by"] in subs]
        return []


async def test_thousands_of_creators_are_read_in_chunks(monkeypatch) -> None:
    import server.core.ch as ch_mod

    ch = _CH(3000)
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    await service_accounts.revoke_created_by(["lee"], revoked_by="scim")
    # 3000 creators would be about 750 KB in the revocation read.
    assert 0 < ch.largest <= 32 * 1024
