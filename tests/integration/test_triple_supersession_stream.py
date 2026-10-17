"""Streaming triple supersession: a later contradicting dated fact must
close the earlier open interval — across separate writes.

GATE: two bulk_set calls seconds apart (the agent-stream shape) write
contradicting single-valued relations. As-of-now must return ONLY the
newest object; as-of a moment inside the old interval must return the
old one. Both halves are pinned: the single-valued gate matches stored
lemma forms ("work_at"), and the repair SELECT sees the previous write's
fire-and-forget async insert.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry  # noqa: E402
from server.memory.api.models_facts import FactsListInput  # noqa: E402
from server.memory.app.bulk_set import handle_memory_bulk_set  # noqa: E402
from server.memory.app.facts import handle_facts_list  # noqa: E402
from tests.integration.conftest import journal_on  # noqa: E402


@pytest.fixture(autouse=True)
async def _ch_client_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


async def _objects(org: str, ns: str, valid_as_of: datetime | None) -> set[str]:
    out = await handle_facts_list(
        FactsListInput(
            org=org,
            namespace=ns,
            subject="user",
            relation="works_at",
            # The handler is called directly: the time goes in the active mode's field.
            **({"valid_as_of": valid_as_of} if journal_on() else {"as_of": valid_as_of}),
            limit=10,
        )
    )
    return {r.object for r in out.rows}


async def test_later_contradicting_write_closes_prior_interval() -> None:
    org = "triple-stream-rt"
    ns = f"sup-{uuid.uuid4().hex[:8]}"

    async def _write(key: str, value: str, obj: str, valid_at: str) -> None:
        await handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org,
                namespace=ns,
                agent="triple-stream-test",
                entries=[
                    _BulkSetEntry(
                        memory_key=key,
                        value=value,
                        relations=[("user", "works_at", obj)],
                        valid_at=valid_at,
                    )
                ],
            )
        )

    # Two separate writes — the streaming shape.
    await _write("rel-acme", "user works_at acme", "acme", "2020-01-01T00:00:00")
    await _write("rel-ch", "user works_at clickhouse", "clickhouse", "2024-01-01T00:00:00")

    # The closure is an ALTER UPDATE mutation — poll until it lands.
    now_objs: set[str] = set()
    for _ in range(50):
        now_objs = await _objects(org, ns, valid_as_of=None)
        if now_objs == {"clickhouse"}:
            break
        await asyncio.sleep(0.3)
    assert now_objs == {"clickhouse"}, f"stale interval leaked: {now_objs}"

    # The old fact stays queryable inside its own validity window.
    hist = await _objects(org, ns, valid_as_of=datetime(2021, 6, 1))
    assert "acme" in hist, hist
