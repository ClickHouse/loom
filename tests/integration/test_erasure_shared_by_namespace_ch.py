"""A namespace erasure reaches every promoted row of the namespace on a real ClickHouse,
one promoted from a user's memory or from the namespace-wide row, through the namespace
step and its census row; a row of another namespace stays. A purge deletes them, demoted
ones included."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from server.core.ch import get_client
from server.erasure.store import census, mutations, steps_memory
from tests.integration._resources import unique_org


@pytest.fixture(autouse=True)
async def _ch_connected():
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


def _shared(org: str, namespace: str, key: str = "definition/arr", **kw: object) -> dict:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    return {
        "org": org,
        "namespace": namespace,
        "memory_key": key,
        "value": '{"term": "ARR"}',
        "memory_type": "definition",
        "confidence": 1.0,
        # The namespace-wide source: no user to match by provenance.
        "promoted_from": [f"{org}/{namespace}/-/-/-/{key}"],
        "promotion_event": "00000000-0000-0000-0000-000000000001",
        "promoted_by": "human:admin",
        "promoted_at": now,
        "reversible_until": now + timedelta(days=7),
        "version": 1,
        "tombstoned_at": None,
        **kw,
    }


async def _count_all(org: str, namespace: str) -> int:
    row = await get_client().query_one(
        "SELECT count() AS n FROM loom.shared WHERE org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": namespace},
    )
    return int((row or {}).get("n") or 0)


async def test_a_namespace_erasure_tombstones_the_namespaces_shared_rows() -> None:
    org = unique_org("erase-shared-ns")
    # One copy of a user's memory, one of the namespace-wide row.
    users = _shared(org, "team", "decision/a", promoted_from=[f"{org}/team/a/u1/s/decision/a"])
    await get_client().insert("loom.shared", [users, _shared(org, "team"), _shared(org, "other")])
    before = await census.table_residue(org, "namespace", "team")
    assert before["loom.shared"] == 2

    assert await steps_memory.tombstone_shared_by_namespace(org, "team") == 2
    after = await census.table_residue(org, "namespace", "team")
    assert after["loom.shared"] == 0
    other = await census.table_residue(org, "namespace", "other")
    assert other["loom.shared"] == 1


async def test_a_namespace_list_erasure_tombstones_each_listed_namespaces_shared_rows() -> None:
    org = unique_org("erase-shared-list")
    rows = [_shared(org, ns) for ns in ("a", "b", "kept")]
    await get_client().insert("loom.shared", rows)
    assert await steps_memory.tombstone_shared_by_namespace(org, ["a", "b"]) == 2
    listed = await census.table_residue(org, "namespace", ["a", "b"])
    assert listed["loom.shared"] == 0
    kept = await census.table_residue(org, "namespace", "kept")
    assert kept["loom.shared"] == 1


async def test_a_purge_deletes_the_namespaces_shared_rows_demoted_ones_included() -> None:
    org = unique_org("erase-shared-purge")
    demoted = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    rows = [
        _shared(org, "team"),
        _shared(org, "team", "definition/mrr", tombstoned_at=demoted),
        _shared(org, "other"),
    ]
    await get_client().insert("loom.shared", rows)
    with mutations.purge_scope(True):
        await steps_memory.tombstone_shared_by_namespace(org, ["team"])
        residue = await census.table_residue(org, "namespace", ["team"])
    assert residue["loom.shared"] == 0
    assert await _count_all(org, "team") == 0
    assert await _count_all(org, "other") == 1
