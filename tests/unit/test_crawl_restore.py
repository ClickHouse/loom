"""Restored crawl authority ends before the real reconciliation publishes held rows."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from server.cli import restore as cli_restore
from server.core import leases
from server.knowledge.app import connected_restore, crawl_restore
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import mirrors, source_retirements
from server.knowledge.store import sources as source_store
from server.settings import settings
from tests.unit.test_connected_account_restore import _unrelated_restore_participants
from tests.unit.test_crawl_exclusions import _World as _ExclusionWorld

pytestmark = pytest.mark.asyncio
_MIRROR = "acl-" + "a" * 24
_FIRST = "00000000-0000-4000-8000-000000000001"
_SECOND = "00000000-0000-4000-8000-000000000002"


def _sql(value: str) -> str:
    return " ".join(value.split())


class _HistoryWorld(_ExclusionWorld):
    """Raw source versions, exact leases and only the SQL used by the real stores."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(monkeypatch)
        self.sources: list[dict[str, Any]] = []
        self.retirements: list[dict[str, Any]] = []
        self.scope: tuple[str, str] | None = None
        self.seeding = False
        self.ignore_mutation = False
        self.mutation_tags: set[str] = set()
        self.executed: list[str] = []
        self.page_reads: list[tuple[str, str, str]] = []
        monkeypatch.setattr(crawl_restore, "get_client", lambda: self)
        monkeypatch.setattr(source_store, "_default_client", lambda: self)
        monkeypatch.setattr(mirrors, "_default_client", lambda: self)
        monkeypatch.setattr(source_retirements, "_default_client", lambda: self)
        monkeypatch.setattr(leases, "holding", self.source_lease)
        monkeypatch.setattr(settings, "knowledge_crawl_lease_wait_s", 0)

    @asynccontextmanager
    async def source_lease(
        self, name: str, *, ttl_s: float, wait_s: float, ch: Any = None, settle_writes: bool
    ) -> AsyncIterator[bool]:
        assert ch is None or ch is self
        assert settle_writes is True
        prefix, org, namespace = name.split(":")
        assert prefix == "knowledge-source" and org == "org" and namespace == "namespace"
        assert self.scope is None and ttl_s == 60.0 and wait_s == 5.0
        self.scope = org, namespace
        try:
            yield True
        finally:
            self.scope = None

    async def seed(self, **changes: Any) -> None:
        self.seeding = True
        try:
            source = KnowledgeSource(
                **(
                    {
                        "org": "org",
                        "namespace": "namespace",
                        "name": "documents",
                        "kind": "gdrive",
                        "source_id": _FIRST,
                        "items": [{"type": "file", "id": "file"}],
                        "credential_mode": "crawl",
                        "access": "mirrored",
                        "secret_enc": "retained-service-key",
                        "approval_enc": "historical-approval",
                        "registrant_subject": "registrant",
                        "crawl_generation": "generation",
                        "crawl_customer_id": "workspace",
                        "crawl_credential_hash": "a" * 64,
                        "crawl_options": {
                            "directory_subject": "directory@example.com",
                            "subjects": [{"type": "person", "email": "person@example.com"}],
                        },
                    }
                    | changes
                )
            )
            await source_store.insert_source(source, ch=self)
        finally:
            self.seeding = False

    def _crawl_rows(self, org: str) -> list[dict[str, Any]]:
        return [r for r in self.sources if r["org"] == org and r["credential_mode"] == "crawl"]

    async def query_one(self, sql: str, *, params: dict[str, Any] | None = None):
        compact = _sql(sql)
        if compact.startswith("SELECT count() AS n FROM loom.worker_status "):
            assert params == {"s": 2 * settings.supervisor_lease_ttl_s}
            return {"n": 0}
        if compact.startswith("SELECT count() AS n FROM loom.knowledge_sources "):
            assert params is not None
            self._count_query(compact, params)
            return {"n": len(self._mutation_rows(compact, params))}
        if compact.startswith(
            "SELECT " + source_store._COLUMNS + " FROM loom.knowledge_sources FINAL "
        ):
            assert params is not None and set(params) == {"org", "ns", "name"}
            assert compact.endswith("AND name = {name:String} LIMIT 1")
            assert self.held and self.scope == (params["org"], params["ns"])
            found = [
                r
                for r in self.sources
                if (r["org"], r["namespace"], r["name"])
                == (params["org"], params["ns"], params["name"])
            ]
            return copy.deepcopy(found[-1]) if found else None
        return await super().query_one(sql, params=params)

    async def query_all(self, sql: str, *, params: dict[str, Any]):
        compact = _sql(sql)
        if "FROM system.mutations" in compact:
            assert set(params) == {"db", "t", "pat"} and compact.endswith("LIMIT 1")
            assert params["db"] == "loom" and params["t"] == "knowledge_sources"
            assert any(tag in params["pat"] for tag in self.mutation_tags)
            return [
                {"mutation_id": "owned", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}
            ]
        if compact.startswith("SELECT DISTINCT org FROM (SELECT org FROM loom.knowledge_sources "):
            assert params == {"limit": settings.knowledge_crawl_registry_scan_limit + 1}
            assert (
                compact
                == "SELECT DISTINCT org FROM (SELECT org FROM loom.knowledge_sources WHERE credential_mode = 'crawl' UNION ALL SELECT org FROM loom.knowledge_crawl_exclusion_keys) ORDER BY org LIMIT {limit:UInt32}"
            )
            orgs = sorted(
                {r["org"] for r in self.sources if r["credential_mode"] == "crawl"} | set(self.keys)
            )
            return [{"org": org} for org in orgs[: params["limit"]]]
        if compact.startswith("SELECT DISTINCT " + source_store._COLUMNS):
            assert " FINAL " not in compact and set(params) == {"org", "limit"}
            assert params["limit"] == settings.knowledge_crawl_registry_scan_limit + 1
            assert compact.endswith(
                "AND credential_mode = 'crawl' ORDER BY namespace, name, source_id, updated_at LIMIT {limit:UInt32}"
            )
            columns = source_store._COLUMNS.split(", ")
            unique = {tuple(row[k] for k in columns) for row in self._crawl_rows(params["org"])}
            rows = [dict(zip(columns, values, strict=True)) for values in unique]
            return sorted(
                rows, key=lambda r: (r["namespace"], r["name"], r["source_id"], r["updated_at"])
            )[: params["limit"]]
        if compact.startswith("SELECT mirror, namespace FROM loom.knowledge_mirrors FINAL "):
            assert params == {"org": "org", "namespaces": ["namespace"]}
            assert compact.endswith("AND NOT deleted ORDER BY mirror")
            assert self.held and self.scope == ("org", "namespace")
            return [{"mirror": _MIRROR, "namespace": "namespace"}]
        if compact.startswith("SELECT DISTINCT org, namespace, name FROM loom.knowledge_sources "):
            return self._approval_page(compact, params)
        if compact.startswith("SELECT DISTINCT toString(source_id) AS source_id, approval_enc "):
            return self._approval_values(compact, params)
        return await super().query_all(sql, params=params)

    def _approval_page(self, sql: str, params: dict[str, Any]):
        assert (
            sql
            == "SELECT DISTINCT org, namespace, name FROM loom.knowledge_sources WHERE (org, namespace, name) > {after:Tuple(String, String, String)} AND approval_enc != '' ORDER BY org, namespace, name LIMIT {limit:UInt32}"
        )
        assert set(params) == {"after", "limit"}
        assert params["limit"] == settings.knowledge_crawl_registry_page_size
        self.page_reads.append(params["after"])
        keys = sorted(
            {
                (r["org"], r["namespace"], r["name"])
                for r in self.sources
                if r["approval_enc"] and (r["org"], r["namespace"], r["name"]) > params["after"]
            }
        )
        return [
            dict(zip(("org", "namespace", "name"), k, strict=True)) for k in keys[: params["limit"]]
        ]

    def _approval_values(self, sql: str, params: dict[str, Any]):
        assert (
            sql
            == "SELECT DISTINCT toString(source_id) AS source_id, approval_enc FROM loom.knowledge_sources WHERE org = {org:String} AND namespace = {ns:String} AND name = {name:String} AND credential_mode = 'crawl' AND approval_enc != '' LIMIT {limit:UInt32}"
        )
        assert set(params) == {"org", "ns", "name", "limit"}
        assert params["limit"] == settings.knowledge_crawl_registry_scan_limit + 1
        assert self.scope == (params["org"], params["ns"])
        pairs = sorted(
            {
                (r["source_id"], r["approval_enc"])
                for r in self._crawl_rows(params["org"])
                if r["namespace"] == params["ns"]
                and r["name"] == params["name"]
                and r["approval_enc"]
            }
        )
        return [
            {"source_id": source, "approval_enc": encoded}
            for source, encoded in pairs[: params["limit"]]
        ]

    def _mutation_rows(self, sql: str, params: dict[str, Any]):
        if set(params) >= {"org", "ns", "id", "old", "new"}:
            assert "namespace = {ns:String} AND source_id = {id:String}" in sql
            assert "credential_mode = 'crawl' AND approval_enc = {old:String}" in sql
            return [
                r
                for r in self._crawl_rows(params["org"])
                if r["namespace"] == params["ns"]
                and r["source_id"] == params["id"]
                and r["approval_enc"] == params["old"]
            ]
        assert set(params) <= {"org", "erasure_mutation_tag"} and "org" in params
        assert "org = {org:String} AND credential_mode = 'crawl'" in sql
        rows = self._crawl_rows(params["org"])
        if "(active OR approval_enc != '' OR crawl_generation != '')" in sql:
            rows = [r for r in rows if r["active"] or r["approval_enc"] or r["crawl_generation"]]
        return rows

    def _count_query(self, sql: str, params: dict[str, Any]) -> None:
        prefix = "SELECT count() AS n FROM loom.knowledge_sources WHERE "
        if "old" in params:
            assert sql == (
                prefix + "org = {org:String} AND namespace = {ns:String} "
                "AND source_id = {id:String} AND credential_mode = 'crawl' "
                "AND approval_enc = {old:String}"
            )
        else:
            assert sql == (
                prefix + "org = {org:String} AND credential_mode = 'crawl' "
                "AND (active OR approval_enc != '' OR crawl_generation != '')"
            )

    async def execute(
        self, sql: str, *, params: dict[str, Any], settings: dict[str, Any] | None = None
    ):
        compact = _sql(sql)
        assert compact.startswith("ALTER TABLE loom.knowledge_sources UPDATE ")
        self.executed.append(compact)
        rows = self._mutation_rows(compact, params)
        if "UPDATE approval_enc = {new:String}" in compact:
            assert compact == (
                "ALTER TABLE loom.knowledge_sources UPDATE approval_enc = {new:String} "
                "WHERE org = {org:String} AND namespace = {ns:String} "
                "AND source_id = {id:String} AND credential_mode = 'crawl' "
                "AND approval_enc = {old:String}"
            )
            assert settings == {"mutations_sync": 2}
            assert self.scope == (params["org"], params["ns"])
            if not self.ignore_mutation:
                for row in rows:
                    row["approval_enc"] = params["new"]
            return
        assert compact == (
            "ALTER TABLE loom.knowledge_sources UPDATE active = 0, approval_enc = '', "
            "crawl_generation = '' WHERE (org = {org:String} AND credential_mode = 'crawl') "
            "AND ({erasure_mutation_tag:String} = {erasure_mutation_tag:String}) "
            "SETTINGS mutations_sync = 0"
        )
        assert "SETTINGS mutations_sync = 0" in compact and settings is None
        assert self.held and self.scope is None
        self.mutation_tags.add(params["erasure_mutation_tag"])
        if not self.ignore_mutation:
            for row in rows:
                row.update(active=False, approval_enc="", crawl_generation="")

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if table == source_store.TABLE:
            if not self.seeding:
                assert self.held and self.scope == ("org", "namespace")
            self.sources.extend(copy.deepcopy(rows))
        elif table == source_retirements.TABLE:
            assert self.held and self.scope == ("org", "namespace")
            for row in rows:
                item = source_retirements.from_row(row)
                assert (item.org, item.namespace, item.mirror) == ("org", "namespace", _MIRROR)
                assert row["state_value"] == "pending"
            self.retirements.extend(copy.deepcopy(rows))
        else:
            await super().insert(table, rows)


async def _restored(monkeypatch: pytest.MonkeyPatch) -> _HistoryWorld:
    world = _HistoryWorld(monkeypatch)
    await world.change(await world.start())
    await world.seed(approval_enc="older-approval", crawl_generation="older")
    await world.seed(approval_enc="newer-approval", crawl_generation="newer")
    await world.seed(source_id=_SECOND, approval_enc="replacement-approval")
    await world.seed(name="shared", credential_mode="shared", approval_enc="unrelated")
    return world


def _participants(monkeypatch: pytest.MonkeyPatch, before_publish: Any) -> None:
    _unrelated_restore_participants(monkeypatch, before_publish)

    async def no_accounts(*, apply: bool, ch: Any) -> None:
        assert isinstance(ch, _HistoryWorld)

    monkeypatch.setattr(connected_restore, "restore_all", no_accounts)


async def test_reconcile_invalidates_every_historical_crawl_approval_before_publication(
    monkeypatch,
):
    world = await _restored(monkeypatch)
    shared = copy.deepcopy(world.sources[-1])
    refusal_state = copy.deepcopy((world.keys, world.rows, world.keeper))
    published = []

    def before_publish():
        assert all(
            not r["active"] and not r["approval_enc"] and not r["crawl_generation"]
            for r in world._crawl_rows("org")
        )
        assert {json.loads(r["detail"])["source_id"] for r in world.retirements} == {
            _FIRST,
            _SECOND,
        }
        published.append(True)

    _participants(monkeypatch, before_publish)
    assert await cli_restore.reconcile("clickhouse", datetime.now(UTC), apply=True, ch=world) == 0
    assert published == [True, True]
    assert world.sources[3] == shared
    assert all(r["secret_enc"] == "retained-service-key" for r in world.sources)
    assert (world.keys, world.rows, world.keeper) == refusal_state


@pytest.mark.parametrize("proof", ["missing-key", "mismatched-epoch"])
async def test_unproved_refusal_state_keeps_restored_publication_held(monkeypatch, proof):
    world = await _restored(monkeypatch)
    if proof == "missing-key":
        world.keys.pop("org")
    else:
        world.keeper[next(iter(world.keeper))] = "active:another-key:another-epoch"
    before = copy.deepcopy(world.sources)
    published = []
    _participants(monkeypatch, lambda: published.append(True))
    assert await cli_restore.reconcile("clickhouse", datetime.now(UTC), apply=True, ch=world) == 1
    assert world.sources == before and not world.retirements and not published


async def test_ignored_history_invalidation_cannot_report_restored_publication_safe(monkeypatch):
    world = await _restored(monkeypatch)
    world.ignore_mutation = True
    published = []
    _participants(monkeypatch, lambda: published.append(True))
    assert await cli_restore.reconcile("clickhouse", datetime.now(UTC), apply=True, ch=world) == 1
    assert world.executed and world.retirements and not published
    assert any(r["approval_enc"] for r in world._crawl_rows("org"))


async def test_crawl_restore_dry_run_preserves_history_and_refusal_proof(monkeypatch):
    world = await _restored(monkeypatch)
    before = copy.deepcopy((world.sources, world.keys, world.rows, world.keeper))
    await crawl_restore.restore_all(apply=False, ch=world)
    assert (world.sources, world.keys, world.rows, world.keeper) == before
    assert not world.retirements and not world.executed


async def test_crawl_restore_refuses_a_different_target_before_storage_access(monkeypatch):
    world = await _restored(monkeypatch)
    before = copy.deepcopy((world.sources, world.retirements, world.executed, world.queries))
    with pytest.raises(RuntimeError, match="crawl_restore_target_mismatch"):
        await crawl_restore.restore_all(apply=True, ch=object())
    assert (world.sources, world.retirements, world.executed, world.queries) == before
