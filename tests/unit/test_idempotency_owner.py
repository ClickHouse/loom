"""A cached replay row names the caller it belongs to, erasure deletes it
by that owner.

A replay body quotes whatever the op returned, memory text included, and the
row key is a hash; without the owner columns erasure had nothing to find the
rows by. CH-free: a recording client stands in for ClickHouse.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core import idempotency
from server.erasure.app import saga as saga
from server.erasure.app import saga_steps
from server.erasure.store import census as erasure_census
from server.erasure.store import steps_ledgers as ledger_steps


class _InsertCH:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.idempotency"
        self.rows.extend(rows)


def test_store_stamps_the_owner_on_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _InsertCH()
    monkeypatch.setattr(idempotency, "get_client", lambda: ch)
    s = idempotency.scope("k", org="o", subject="alice", operation="memory.set")
    asyncio.run(
        idempotency.store(
            idempotency_key=s.key,
            org=s.org,
            subject=s.subject,
            body_hash="h",
            operation="memory.set",
            status_code=200,
            response_body={"ok": True},
        )
    )
    (row,) = ch.rows
    assert (row["idempotency_key"], row["org"], row["subject"]) == (s.key, "o", "alice")


class _StubCH:
    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.counts: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, int]:
        self.counts.append((sql, params or {}))
        return {"n": self.count}

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.commands.append((sql, params or {}))

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "startsWith(t.name, '_provider_rebuild_')" in sql:
            return []
        return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]


def test_namespace_erasure_deletes_the_orgs_replay_rows() -> None:
    ch = _StubCH()
    assert asyncio.run(ledger_steps.delete_idempotency_by_namespace("o", ["a", "b"], ch=ch)) == 1
    (sql, params), vectors = ch.commands  # then the org's cached query vectors
    assert " ".join(sql.split()).startswith("ALTER TABLE loom.idempotency DELETE WHERE")
    assert "loom.embedding_cache" in vectors[0] and vectors[1]["org"] == "o"
    assert "org = {org:String}" in sql and params["org"] == "o"
    assert "namespace" not in sql  # the table has no namespace column
    assert " FINAL" not in ch.counts[0][0]


def test_user_erasure_deletes_the_orgs_replay_rows_not_only_the_users_subject() -> None:
    # A service account acting for the user stores replays under its own
    # subject; nothing on the row names the user, so the step fails closed.
    ch = _StubCH()
    asyncio.run(ledger_steps.delete_idempotency_by_user("o", "alice", ch=ch))
    (sql, params), vectors = ch.commands  # then the org's cached query vectors
    assert "loom.embedding_cache" in vectors[0] and vectors[1]["org"] == "o"
    assert "org = {org:String}" in sql and params["org"] == "o"
    assert "subject" not in sql and "uid" not in params


def test_a_saga_step_owns_the_table_in_both_scopes() -> None:
    assert "idempotency.delete" in dict(saga_steps._LATE_STEPS_NS)
    assert "idempotency.delete" in dict(saga_steps._LATE_STEPS_USER)
    (sub,) = [s for s in erasure_census._CENSUS if s.table == "loom.idempotency"]
    assert sub.owner("namespace") == sub.owner("user_id") == "idempotency.delete"
    assert "loom.idempotency" not in erasure_census.NO_MEMORY_CONTENT


def test_the_census_counts_the_table_org_wide_in_both_scopes() -> None:
    ch = _StubCH(count=0)
    asyncio.run(erasure_census.residue_census("o", "namespace", ["a", "b"], ch=ch))
    (sql,) = [q for q, _ in ch.counts if "FROM loom.idempotency" in q]
    assert "namespace" not in sql and "org = {org:String}" in sql
    ch = _StubCH(count=0)
    asyncio.run(erasure_census.residue_census("o", "user_id", "alice", ch=ch, spellings=["alice"]))
    (sql,) = [q for q, _ in ch.counts if "FROM loom.idempotency" in q]
    assert "subject" not in sql and "org = {org:String}" in sql
