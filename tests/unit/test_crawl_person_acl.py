"""Person erasure redacts raw ACL history while preserving other writers and text."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.erasure.store import crawl_person as store
from server.knowledge.domain.access_list import AccessList, from_json, to_json
from server.knowledge.domain.source_crawl import without_people
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR
from server.settings import settings

pytestmark = pytest.mark.asyncio
_ERASE = frozenset({"gone@example.com"})
_OLD = to_json(AccessList(emails=("gone@example.com", "kept@example.com"), anyone=True))
_NEW = to_json(AccessList(emails=("kept@example.com",), anyone=True))


def _row(**extra: Any) -> dict[str, Any]:
    return {
        "org": "org",
        "namespace": "mirror",
        "actor": SYNC_WORKER_ACTOR,
        "tags": ["source-id:source"],
        "access_json": _OLD,
        "text": "shared document",
        **extra,
    }


class _World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, postgres: bool = False) -> None:
        self.documents = [_row(version=1), _row(version=2)]
        self.heads = [_row()] if postgres else []
        self.deliveries = (
            [{"row": _row(), "lease_token": 7, "left_s": 3.0, "targets": 5}] if postgres else []
        )
        self.events: list[str] = []
        self.waits: list[list[float]] = []
        self.old_publisher = False
        self.cancel_wait = False
        self.ignore_mutation = False
        monkeypatch.setattr(store.pg, "enabled", lambda: postgres)
        monkeypatch.setattr(store.pg, "transaction", self.transaction)
        monkeypatch.setattr(store.pg, "wait_out", self.wait_out)

    @asynccontextmanager
    async def transaction(self):
        self.events.append("transaction")
        yield self
        self.events.append("commit")

    @staticmethod
    def selected(row: dict[str, Any], args: Any) -> bool:
        org, namespace, actor, tag = args[:4]
        return (row["org"], row["namespace"], row["actor"]) == (
            org,
            namespace,
            actor,
        ) and tag in row["tags"]

    async def wait_out(self, leases: list[float]) -> None:
        assert self.events[-1] == "commit"
        self.waits.append(leases)
        self.events.append("wait")
        if self.cancel_wait:
            self.cancel_wait = False
            raise RuntimeError("interrupted after durable redaction")
        if self.old_publisher:
            self.documents.append(_row(version=3))
            self.old_publisher = False

    async def fetch(self, sql: str, *args: Any):
        assert " FINAL" not in sql
        if "AS left_s FROM loom.knowledge_deliveries d" in sql:
            assert "FOR UPDATE" in sql and "d.fenced_until" in sql
            return [
                {"left_s": d["left_s"]} for d in self.deliveries if self.selected(d["row"], args)
            ]
        assert sql.startswith("SELECT DISTINCT row->>'access_json'")
        rows = [*self.heads, *(d["row"] for d in self.deliveries)]
        return [
            {"access_json": value}
            for value in sorted({r["access_json"] for r in rows if self.selected(r, args)})
        ][: args[4]]

    async def execute(self, sql: str, *args: Any, params: dict[str, Any] | None = None):
        if params is not None:
            assert "ALTER TABLE loom.documents" in sql
            assert "UPDATE access_json" in sql and "DELETE" not in sql
            assert "erasure_mutation_tag" in params
            self.events.append("mutation")
            if not self.ignore_mutation:
                for row in self.documents:
                    if (
                        self.selected(row, self.scope(params))
                        and row["access_json"] == params["before"]
                    ):
                        row["access_json"] = params["after"]
            return
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            assert args == ("org", "mirror")
            return
        if sql.startswith("SELECT 1 FROM loom.knowledge_heads"):
            assert "ORDER BY org, namespace, source_uri FOR UPDATE" in sql
            return
        assert sql.startswith("UPDATE loom.knowledge_") and "jsonb_set" in sql
        if "knowledge_heads" in sql:
            for row in self.heads:
                if self.selected(row, args) and row["access_json"] == args[4]:
                    row["access_json"] = args[5]
        else:
            assert "lease_token = lease_token + 1" in sql and "GREATEST(fenced_until" in sql
            for delivery in self.deliveries:
                row = delivery["row"]
                if self.selected(row, args) and row["access_json"] == args[4]:
                    row["access_json"] = args[5]
                    delivery["lease_token"] += 1

    @staticmethod
    def scope(params: dict[str, Any]) -> tuple[str, ...]:
        return tuple(params[k] for k in ("org", "ns", "actor", "tag"))

    async def query_all(self, sql: str, *, params: dict[str, Any]):
        if "FROM system.mutations" in sql:
            return [
                {"is_done": 1, "parts_to_do": 0, "latest_fail_reason": "", "mutation_id": "owned"}
            ]
        assert sql.startswith("SELECT DISTINCT access_json FROM loom.documents")
        assert "FINAL" not in sql
        values = sorted(
            {r["access_json"] for r in self.documents if self.selected(r, self.scope(params))}
        )
        return [{"access_json": value} for value in values[: params["limit"]]]

    async def query_one(self, sql: str, *, params: dict[str, Any]):
        assert "SELECT count() AS n FROM loom.documents" in sql and "FINAL" not in sql
        return {
            "n": sum(
                self.selected(r, self.scope(params)) and r["access_json"] == params["before"]
                for r in self.documents
            )
        }


async def test_raw_versions_redacted_without_erasing_text_other_grants_or_foreign_sources(
    monkeypatch,
):
    world = _World(monkeypatch)
    foreign = [
        _row(org="other"),
        _row(namespace="other"),
        _row(actor="someone"),
        _row(tags=["source-id:other"]),
    ]
    world.documents.extend(copy.deepcopy(foreign))
    await store.scrub("org", "mirror", "source", _ERASE, ch=world)
    assert world.documents == [
        _row(version=1, access_json=_NEW),
        _row(version=2, access_json=_NEW),
        *foreign,
    ]
    assert await store.residue("org", "mirror", "source", _ERASE, ch=world) == 0


async def test_old_postgres_publisher_waits_before_clickhouse_scrub(monkeypatch):
    world = _World(monkeypatch, postgres=True)
    world.old_publisher = True
    await store.scrub("org", "mirror", "source", _ERASE, ch=world)
    assert world.waits == [[3.0]] and world.events.index("wait") < world.events.index("mutation")
    assert all(r["access_json"] == _NEW and r["text"] == "shared document" for r in world.documents)
    assert world.heads == [_row(access_json=_NEW)]
    assert world.deliveries == [
        {"row": _row(access_json=_NEW), "lease_token": 8, "left_s": 3.0, "targets": 5}
    ]


async def test_retry_waits_retained_delivery_even_when_its_acl_was_already_redacted(monkeypatch):
    world = _World(monkeypatch, postgres=True)
    world.cancel_wait = True
    with pytest.raises(RuntimeError, match="interrupted"):
        await store.scrub("org", "mirror", "source", _ERASE, ch=world)
    assert world.deliveries[0]["row"]["access_json"] == _NEW
    assert all(r["access_json"] == _OLD for r in world.documents)
    world.old_publisher = True
    await store.scrub("org", "mirror", "source", _ERASE, ch=world)
    assert world.waits == [[3.0], [3.0]] and world.deliveries[0]["lease_token"] == 8
    assert all(r["access_json"] == _NEW for r in world.documents)


async def test_reported_mutation_completion_cannot_hide_raw_historical_residue(monkeypatch):
    world = _World(monkeypatch)
    world.ignore_mutation = True
    with pytest.raises(RuntimeError, match="history_remains"):
        await store.scrub("org", "mirror", "source", _ERASE, ch=world)
    assert await store.residue("org", "mirror", "source", _ERASE, ch=world) == 1


async def test_history_bound_refuses_without_partial_clickhouse_mutation(monkeypatch):
    world = _World(monkeypatch)
    monkeypatch.setattr(settings, "knowledge_crawl_registry_scan_limit", 1)
    world.documents.append(_row(access_json=to_json(AccessList(emails=("gone@example.com",)))))
    with pytest.raises(RuntimeError, match="history_bound"):
        await store.scrub("org", "mirror", "source", _ERASE, ch=world)
    assert not world.events


async def test_only_exact_person_entries_removed_and_unread_history_stays_closed():
    raw = to_json(
        AccessList(
            emails=("gone@example.com", "other@example.com"),
            groups=("team@example.com",),
            domains=("example.com",),
        )
    )
    result = from_json(without_people(raw, _ERASE))
    assert result == AccessList(
        emails=("other@example.com",), groups=("team@example.com",), domains=("example.com",)
    )
    assert without_people("{broken", _ERASE) == ""
    assert without_people(to_json(AccessList(owner="someone")), _ERASE) == ""
