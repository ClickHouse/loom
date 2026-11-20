"""The replicated erasure fixture supplies an isolated write-lease store."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.core import leases
from server.settings import settings
from tests.integration import test_erasure_replicated_ch as replicated
from tests.unit.claims_fake import FakeLockTable


class Schema:
    def __init__(self):
        self.tables = {}
        self.commands = []

    def command(self, sql):
        self.commands.append(sql)
        if sql.startswith("CREATE TABLE "):
            self.tables[sql.split()[2]] = sql

    def query(self, sql, parameters=None):
        if "engine_full" in sql:
            assert parameters["db"] == "source_schema"
            return SimpleNamespace(result_rows=[("ReplacingMergeTree(version) ORDER BY tuple()",)])
        assert "SELECT DISTINCT engine" in sql
        flags = {"ENGINE = Replicated" in ddl for ddl in self.tables.values()}
        return SimpleNamespace(result_rows=[(flag,) for flag in sorted(flags)])


class FixtureLocks(FakeLockTable):
    def __init__(self, schema):
        super().__init__()
        self.schema = schema

    async def query_one(self, sql, params=None):
        if "loom._migration_lock" in sql:
            if f"{settings.ch_database}._migration_lock" not in self.schema.tables:
                raise RuntimeError("Unknown table _migration_lock")
        return await super().query_one(sql, params)


@pytest.fixture
def isolated_schema(monkeypatch):
    monkeypatch.setattr(settings, "ch_database", "source_schema")
    schema = Schema()
    stream = replicated.db.__wrapped__(schema, monkeypatch)
    name = next(stream)
    try:
        yield schema, name
    finally:
        stream.close()
    assert schema.commands[-1] == f"DROP DATABASE IF EXISTS {name} SYNC"


async def test_replicated_erasure_fixture_can_take_and_release_a_write_lease(isolated_schema):
    schema, name = isolated_schema
    keeper = FixtureLocks(schema)
    lease = await leases.take("memory_insert/fixture", ttl_s=60, ch=keeper)
    assert lease is not None and lease.held
    assert settings.ch_database == name
    await lease.release()
    assert keeper.keys == {}


async def test_a_missing_fixture_lease_table_still_refuses(isolated_schema):
    schema, name = isolated_schema
    schema.tables.pop(f"{name}._migration_lock", None)
    with pytest.raises(leases.LeaseUnavailable):
        await leases.take("memory_insert/fixture", ttl_s=60, ch=FixtureLocks(schema))


def test_fixture_lease_paths_are_separate_from_source_and_each_other(monkeypatch):
    paths = []
    for _ in range(2):
        with monkeypatch.context() as patch:
            patch.setattr(settings, "ch_database", "source_schema")
            schema = Schema()
            stream = replicated.db.__wrapped__(schema, patch)
            name = next(stream)
            try:
                ddl = schema.tables[f"{name}._migration_lock"]
                assert "AS source_schema._migration_lock" in ddl
                assert f"KeeperMap('/loom/tests/{name}/migration_lock')" in ddl
                assert "PRIMARY KEY name" in ddl
                assert all(
                    "ENGINE = Replicated" in schema.tables[f"{name}.{table}"]
                    for table in replicated._TABLES
                )
                paths.append(name)
            finally:
                stream.close()
    assert paths[0] != paths[1]
