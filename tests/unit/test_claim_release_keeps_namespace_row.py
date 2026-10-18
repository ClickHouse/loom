"""claim_release writes a new version of the loom.namespaces row carrying every column.

loom.namespaces is ReplacingMergeTree(updated_at): the release row wins under FINAL,
so any column it omits reads back as the schema default. A restricted namespace must
stay restricted after its claim is released. Hermetic: the ClickHouse client is faked.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

import server.identity.app.claim_release as cr
from server.settings import settings

_CREATED = datetime(2025, 1, 2, 3, 4, 5)

_ROW: dict[str, Any] = {
    "org": "acme",
    "namespace": "vault",
    "owner_email": "Alice",
    "description": "secrets",
    "tags": ["sensitive"],
    "active": True,
    "auto_promote_enabled": True,
    "created_at": _CREATED,
    "updated_at": _CREATED,
    "learnings_review_enabled": False,
    "conflict_threshold": 0.9,
    "retention_days": 30,
    "warm_days": 7,
    "conversation_seeder_enabled": False,
    "visibility": "private",
    "kind": "code",
    "password_hash": "argon2id$abc",
    "requires_approval": True,
    "daily_cost_cap_usd": 12.5,
    "contact_email": "Alice@Acme.test",
    "classification": "restricted",
    "join_policy": "closed",
    "retrieval_defaults": '{"k": 3}',
}


class _FakeCH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        if "loom.namespaces" in sql:
            return dict(_ROW)
        return {"c": 1}

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "sso_links" in sql or "machine_identities" in sql:
            return []  # no SSO links or bots to drop
        return [{"subject": "alice@acme.test", "role": "admin", "granted_by": "alice@acme.test"}]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))


def test_release_keeps_classification_and_every_other_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_required", False)
    fake = _FakeCH()
    monkeypatch.setattr(cr, "get_client", lambda: fake)

    out = asyncio.run(
        cr.claim_release(cr.ClaimReleaseInput(org="acme", namespace="vault", reason="left"), None)  # type: ignore[arg-type]
    )
    assert out.released and out.previous_owner == "Alice" and out.grants_revoked == 1

    ns_rows = [rows for table, rows in fake.inserts if table == "loom.namespaces"]
    assert len(ns_rows) == 1 and len(ns_rows[0]) == 1
    written = ns_rows[0][0]

    assert written["owner_email"] == ""
    # the old credential dies with the claim
    assert written["contact_email"] == "" and written["password_hash"] == ""
    assert written["active"] is False
    assert written["description"] == "Released by local (left)"
    assert written["updated_at"] > _CREATED
    # the person's spellings stay tied for the claim cap, and for nothing else
    assert written["released_aliases"] == ["alice", "alice@acme.test"], (
        "kept lowercase, as the cap matches them"
    )
    changed = {
        "released_aliases",
        "owner_email",
        "contact_email",
        "password_hash",
        "active",
        "description",
        "updated_at",
    }
    assert {k: v for k, v in written.items() if k not in changed} == {
        k: v for k, v in _ROW.items() if k not in changed
    }
    assert written["classification"] == "restricted"
