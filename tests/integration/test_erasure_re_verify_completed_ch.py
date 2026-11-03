"""`loom erasure-re-verify --completed-since` against ClickHouse: a saga a previous
build completed without erasing a table this build added (rolling-upgrades rule 14)
leaves orphan row links, and only those are deleted. Data written after the
erasure finished is not touched."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.integration._resources import unique_org

NS = "re-verify"


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


_EDGE_KEYS = ("org", "namespace", "memory_id", "user_id", "content_sha256")


def _triple_row(org: str, memory_id: str) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": NS,
        "memory_id": memory_id,
        "user_id": "u1",
        "content_sha256": "0" * 64,
        "subject": "user",
        "relation": "live_in",
        "object": "rome",
        "inferred": False,
    }


async def _edge_rows(ch: Any, org: str) -> int:
    row = await ch.query_one(
        "SELECT count() AS n FROM loom.entity_edge_rows WHERE org = {org:String}",
        params={"org": org},
    )
    return int(row["n"])


async def _triple_rows(ch: Any, org: str) -> set[str]:
    rows = await ch.query_all(
        "SELECT toString(memory_id) AS id FROM loom.entity_triple_rows WHERE org = {org:String}",
        params={"org": org},
    )
    return {str(r["id"]) for r in rows}


async def _live_memory(ch: Any, org: str, key: str) -> int:
    row = await ch.query_one(
        "SELECT count() AS n FROM loom.memory FINAL WHERE org = {org:String} "
        "AND memory_key = {k:String} AND tombstoned_at IS NULL",
        params={"org": org, "k": key},
    )
    return int(row["n"])


def _memory_row(org: str, key: str, memory_id: str, **over: Any) -> dict[str, Any]:
    row = {
        "org": org,
        "namespace": NS,
        "agent": "a",
        "user_id": "u1",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "semantic",
        "value": "user lives in rome",
        "version": 1,
        "memory_id": memory_id,
    }
    return {**row, **over}


async def _completed_saga(org: str, now: datetime) -> str:
    """A namespace saga, completed with every step checkpointed and a clean census."""
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app.saga import checkpoint, request_row_template
    from server.erasure.app.saga_steps import NAMESPACE_STEPS

    request = ErasureRequestInput(
        org=org,
        scope_type="namespace_list",
        scope_value=json.dumps({"namespaces": [NS], "purge": False, "external": False}),
        requester="itest",
        external=False,
    )
    erasure_id = str(uuid.uuid4())
    done = [name for name, _ in NAMESPACE_STEPS] + ["verify.residue"]
    await checkpoint(
        row=request_row_template(
            erasure_id=erasure_id, input=request, actor="itest", requested_at=now, attempts=1
        ),
        status="completed",
        steps_completed=done,
        current_step=len(done),
        completed_at=now,
        extra={"residue_count": 0, "residue": "{}", "verified_at": now},
    )
    return erasure_id


def _args(org: str, erasure_id: str, since: datetime) -> argparse.Namespace:
    return argparse.Namespace(
        org=org,
        id=[erasure_id],
        resume=False,
        skip_external=False,
        orphan_graph=False,
        erase=False,
        batch=200,
        show=20,
        requester="itest",
        completed_since=since,
    )


async def test_only_orphan_row_links_go_and_later_writes_stay(ch: Any) -> None:
    from server.cli import hygiene

    org = unique_org("re-verify")
    now = datetime.now(UTC).replace(tzinfo=None)
    erasure_id = await _completed_saga(org, now)
    # The link an older build's saga did not know to delete: its memory is gone.
    orphan_id = str(uuid.uuid4())
    # A memory written after the erasure finished, with its own link.
    later_id = str(uuid.uuid4())
    await ch.insert("loom.entity_triple_rows", [_triple_row(org, orphan_id)])
    edge = {k: v for k, v in _triple_row(org, orphan_id).items() if k in _EDGE_KEYS}
    await ch.insert("loom.entity_edge_rows", [{**edge, "entity_a": "rome", "entity_b": "user"}])
    await ch.insert("loom.memory", [_memory_row(org, "later", later_id)])
    await ch.insert("loom.entity_triple_rows", [_triple_row(org, later_id)])
    assert await _triple_rows(ch, org) == {orphan_id, later_id}
    assert await _edge_rows(ch, org) == 1

    args = _args(org, erasure_id, now - timedelta(minutes=1))
    # Read-only: the orphan is found, and nothing is erased.
    assert await hygiene._re_verify_run(ch, args) == 1
    assert await _triple_rows(ch, org) == {orphan_id, later_id}

    args.resume = True
    assert await hygiene._re_verify_run(ch, args) == 0
    # Only the orphan went; the later memory and its link stay.
    assert await _triple_rows(ch, org) == {later_id}
    assert await _edge_rows(ch, org) == 0
    assert await _live_memory(ch, org, "later") == 1

    # Nothing orphan is left, so a second read-only run is clean.
    args.resume = False
    assert await hygiene._re_verify_run(ch, args) == 0

    # A saga verified before the cut is not taken.
    args.completed_since = now + timedelta(minutes=1)
    assert await hygiene._re_verify_run(ch, args) == 0


async def test_a_later_memory_soft_deleted_keeps_its_link(ch: Any) -> None:
    # memory.delete tombstones a row that can still be restored. One written after
    # the erasure finished is not the saga's, so its link is not an orphan. A memory
    # the saga itself tombstoned has no row since the census, so its link goes.
    from server.cli import hygiene

    org = unique_org("re-verify")
    now = datetime.now(UTC).replace(tzinfo=None)
    erasure_id = await _completed_saga(org, now)
    erased_id = str(uuid.uuid4())
    before = now - timedelta(hours=1)
    await ch.insert(
        "loom.memory",
        [_memory_row(org, "erased", erased_id, updated_at=before, tombstoned_at=now)],
    )
    later_id = str(uuid.uuid4())
    await ch.insert("loom.memory", [_memory_row(org, "later", later_id)])
    await ch.insert(
        "loom.memory",
        [_memory_row(org, "later", later_id, version=2, status="tombstoned", tombstoned_at=now)],
    )
    await ch.insert(
        "loom.entity_triple_rows", [_triple_row(org, erased_id), _triple_row(org, later_id)]
    )
    assert await _live_memory(ch, org, "later") == 0

    args = _args(org, erasure_id, now - timedelta(minutes=1))
    args.resume = True
    assert await hygiene._re_verify_run(ch, args) == 0
    assert await _triple_rows(ch, org) == {later_id}
