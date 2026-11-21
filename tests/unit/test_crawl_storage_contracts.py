"""Every crawl control and membership version carries its complete declared row."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from server.knowledge.domain.source_crawl import Expansion
from server.knowledge.domain.sources import KnowledgeSource
from server.knowledge.store import crawl_exclusions as exclusions
from server.knowledge.store import crawl_publication as publication
from tests.unit.schema_ddl import columns

pytestmark = pytest.mark.asyncio


class _Rows:
    def __init__(self) -> None:
        self.version = 100
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql: str, *, params: dict[str, Any]) -> dict[str, int]:
        assert sql.startswith("SELECT greatest(") and "max(version) + 1" in sql
        assert params["k0"] == "org"
        self.version += 1
        return {"v": self.version}

    async def query_all(self, sql: str, *, params: dict[str, Any]):
        assert "FROM loom.knowledge_group_members FINAL" in sql
        assert params == {"org": "org", "ns": "mirror", "id": "source", "limit": 5}
        return [{"group": "old@example.com"}]

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert all(set(row) == set(columns(table.removeprefix("loom."))) for row in rows)
        self.inserted.append((table, copy.deepcopy(rows)))


async def test_refusal_key_rewrite_preserves_full_pending_manifest() -> None:
    rows = _Rows()
    await exclusions.key_put(
        "org",
        "a" * 64,
        "sealed-key",
        generation="generation",
        pending_id="b" * 32,
        pending_digests=("c" * 64,),
        pending_exclude=True,
        ch=rows,
    )
    assert rows.inserted == [
        (
            exclusions.KEYS,
            [
                {
                    "org": "org",
                    "key_id": "a" * 64,
                    "key_enc": "sealed-key",
                    "generation": "generation",
                    "pending_id": "b" * 32,
                    "pending_digests": ["c" * 64],
                    "pending_exclude": True,
                    "version": 101,
                }
            ],
        )
    ]


async def test_refusal_and_explicit_readmission_each_write_complete_newer_rows() -> None:
    rows = _Rows()
    await exclusions.put("org", ["a" * 64, "a" * 64], exclude=True, ch=rows)
    await exclusions.put("org", ["a" * 64], exclude=False, ch=rows)
    assert rows.inserted == [
        (
            exclusions.REFUSALS,
            [{"org": "org", "digest": "a" * 64, "excluded": True, "version": 101}],
        ),
        (
            exclusions.REFUSALS,
            [{"org": "org", "digest": "a" * 64, "excluded": False, "version": 102}],
        ),
    ]


async def test_complete_group_replacement_clears_removed_members_without_losing_columns() -> None:
    rows = _Rows()
    source = KnowledgeSource("org", "namespace", "name", "gdrive", "source", mirror="mirror")
    now = datetime.now(UTC)
    expansion = Expansion("customer", (), (("new@example.com", ("person@example.com",)),), "fence")
    await publication.groups(source, expansion, now, maximum=4, ch=rows)
    assert rows.inserted == [
        (
            publication.GROUPS,
            [
                {
                    "org": "org",
                    "namespace": "mirror",
                    "source_id": "source",
                    "group": "new@example.com",
                    "name": "name",
                    "members": ["person@example.com"],
                    "read_at": now,
                    "deleted": False,
                    "version": 101,
                },
                {
                    "org": "org",
                    "namespace": "mirror",
                    "source_id": "source",
                    "group": "old@example.com",
                    "name": "name",
                    "members": [],
                    "read_at": now,
                    "deleted": True,
                    "version": 102,
                },
            ],
        )
    ]


def test_canonical_person_axis_is_explicit_and_disjoint_from_namespace_memory() -> None:
    from server.erasure.store import census

    assert census.CANONICAL_PERSON_TABLES == {
        "loom.scim_users": "crawl_person.finalize",
        "loom.knowledge_crawl_exclusion_keys": "crawl_person.exclude",
        "loom.knowledge_crawl_exclusions": "crawl_person.exclude",
    }
    for table in census.CANONICAL_PERSON_TABLES:
        assert table not in census.NO_MEMORY_CONTENT
        assert table not in census.CANONICAL_OWNER_TABLES
        assert not any(substrate.table == table for substrate in census._CENSUS)
        assert not any(table in tables for tables in census.STEP_ONLY_TABLES.values())
        assert census.classified_tables()[table] == "canonical_person"
