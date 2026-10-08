"""A service-account revoke is a new ReplacingMergeTree(version) row: it carries every
column of the live row forward, the scoped token's `ns_roles` map and its
`inherit_creator_grants` flag included, so the list view of a revoked scoped token still
says what it could reach."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.identity.app import me_service_accounts as me


class _CH:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any]:
        assert "inherit_creator_grants" in sql and "ns_roles" in sql
        return self.row

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))


@pytest.mark.asyncio
async def test_revoke_carries_ns_roles_and_the_inherit_flag(monkeypatch) -> None:
    import server.auth.revocation as revocation
    import server.core.ch as ch_mod

    row = {
        "sa_subject": "sa:bot",
        "namespaces": ["a", "b"],
        "created_by": "dev",
        "created_at": datetime(2026, 1, 1),
        "expires_at": None,
        "ttl_days": 0,
        "roles": ["viewer"],
        "kind": "service_account",
        "client": "",
        "inherit_creator_grants": False,
        "ns_roles": '{"a": "viewer", "b": "writer"}',
    }
    ch = _CH(row)
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)

    async def _noop(*a: Any, **k: Any) -> int:
        return 0

    monkeypatch.setattr(revocation, "refresh", _noop)
    monkeypatch.setattr(me.token_grants, "revoke_token_grants", _noop)
    monkeypatch.setattr(me.settings, "auth_required", False)
    out = await me.me_service_accounts_revoke(None, {"name": "bot", "subject": "dev"})  # type: ignore[arg-type]
    assert out["revoked"] is True
    [written] = [r for t, rows in ch.inserts if t == "loom.service_accounts" for r in rows]
    assert written["ns_roles"] == row["ns_roles"]
    assert written["inherit_creator_grants"] is False and written["revoked_at"] is not None
