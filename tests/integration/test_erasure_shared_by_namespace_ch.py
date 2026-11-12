"""A namespace erasure reaches the namespace's promoted rows on a real ClickHouse: a row
promoted from the namespace-wide row names no user, so only the namespace step and its
census row see it, and a row of another namespace stays."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.erasure.store import census, steps_memory  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


def _shared(org: str, namespace: str) -> dict[str, object]:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    return {
        "org": org,
        "namespace": namespace,
        "memory_key": "definition/arr",
        "value": '{"term": "ARR"}',
        "memory_type": "definition",
        "confidence": 1.0,
        # The namespace-wide source: no user to match by provenance.
        "promoted_from": [f"{org}/{namespace}/-/-/-/definition/arr"],
        "promotion_event": "00000000-0000-0000-0000-000000000001",
        "promoted_by": "human:admin",
        "promoted_at": now,
        "reversible_until": now + timedelta(days=7),
        "version": 1,
    }


async def test_a_namespace_erasure_tombstones_the_namespaces_shared_rows() -> None:
    from server.core.ch import get_client

    org = unique_org("erase-shared-ns")
    await get_client().insert("loom.shared", [_shared(org, "team"), _shared(org, "other")])
    before = await census.table_residue(org, "namespace", "team")
    assert before["loom.shared"] == 1

    assert await steps_memory.tombstone_shared_by_namespace(org, "team") == 1
    after = await census.table_residue(org, "namespace", "team")
    assert after["loom.shared"] == 0
    other = await census.table_residue(org, "namespace", "other")
    assert other["loom.shared"] == 1


async def test_a_namespace_list_erasure_tombstones_each_listed_namespaces_shared_rows() -> None:
    from server.core.ch import get_client

    org = unique_org("erase-shared-list")
    rows = [_shared(org, ns) for ns in ("a", "b", "kept")]
    await get_client().insert("loom.shared", rows)
    assert await steps_memory.tombstone_shared_by_namespace(org, ["a", "b"]) == 2
    listed = await census.table_residue(org, "namespace", ["a", "b"])
    assert listed["loom.shared"] == 0
    kept = await census.table_residue(org, "namespace", "kept")
    assert kept["loom.shared"] == 1
