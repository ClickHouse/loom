"""A service-account revoke is a new ReplacingMergeTree(version) row: it carries every
column of the live row forward, the scoped token's `ns_roles` map and its
`inherit_creator_grants` flag included, so the list view of a revoked scoped token still
says what it could reach."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.identity.app import me_service_accounts as me


class _CH:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.claimed = False  # the name's claim, as `_claim` sets it
        self.unclaimed: list[str] = []  # what ran outside it

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any]:
        if "loom.revoked_tokens" in sql:  # the version a revocation writes past
            return {"v": 0}
        assert "inherit_creator_grants" in sql and "ns_roles" in sql and "version" in sql
        assert "lineage" in sql and "saves_to" in sql
        if not self.claimed:
            self.unclaimed.append("read")
        return self.row

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        if table == "loom.service_accounts" and not self.claimed:
            self.unclaimed.append("write")
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
        "lineage": ["dev"],
        "saves_to": "b",
        "revoked_at": None,
        "version": 2**63,  # written by a host whose clock runs ahead
    }
    ch = _CH(row)
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    from contextlib import asynccontextmanager

    from server.auth import grant_claims

    @asynccontextmanager
    async def _claim(org: str, name: str) -> Any:
        # A create of the name between revoke's read and its write would be
        # replaced by the revoked copy of the row.
        ch.claimed = True
        yield
        ch.claimed = False

    monkeypatch.setattr(grant_claims, "service_account_change", _claim)

    async def _noop(*a: Any, **k: Any) -> int:
        return 0

    monkeypatch.setattr(revocation, "refresh", _noop)
    monkeypatch.setattr(me.token_grants, "revoke_token_grants", _noop)
    monkeypatch.setattr(me.settings, "auth_required", False)
    no_bearer: Any = SimpleNamespace(headers={})
    out = await me.me_service_accounts_revoke(no_bearer, {"name": "bot", "subject": "dev"})
    assert out["revoked"] is True
    [written] = [r for t, rows in ch.inserts if t == "loom.service_accounts" for r in rows]
    assert written["ns_roles"] == row["ns_roles"]
    assert written["inherit_creator_grants"] is False and written["revoked_at"] is not None
    assert written["lineage"] == ["dev"] and written["saves_to"] == "b"
    assert written["version"] > row["version"]  # it replaces the live row under FINAL
    assert ch.unclaimed == []

    # Revoked again (a retry, a second click): the first revocation stands.
    row["revoked_at"] = first = datetime(2026, 2, 1)
    again = await me.me_service_accounts_revoke(no_bearer, {"name": "bot", "subject": "dev"})
    rows_written = [t for t, _ in ch.inserts if t == "loom.service_accounts"]
    assert again["revoked_at"] == first.isoformat() and len(rows_written) == 1
