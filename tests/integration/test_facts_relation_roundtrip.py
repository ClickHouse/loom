"""facts.list must find triples by the predicate the caller ASSERTED.

GATE: the write path canonicalizes relation verbs (canonicalize_verb ->
canonical_relation: "works_at" -> "work_at"); a read that only lowercases
can never match an inflected predicate, so a facts.list filtered by the
caller's own relation string would silently return [] while the triples sit
in entity_triples.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry  # noqa: E402
from server.memory.api.models_facts import FactsListInput  # noqa: E402
from server.memory.app.bulk_set import handle_memory_bulk_set  # noqa: E402
from server.memory.app.facts import handle_facts_list  # noqa: E402


@pytest.fixture(autouse=True)
async def _ch_client_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


async def test_facts_list_matches_inflected_relation() -> None:
    org = "facts-rt"
    ns = f"rel-{uuid.uuid4().hex[:8]}"

    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=ns,
            agent="facts-roundtrip-test",
            entries=[
                _BulkSetEntry(
                    memory_key="rel-1",
                    value="alice works_at clickhouse",
                    relations=[("alice", "works_at", "clickhouse")],
                )
            ],
        )
    )

    # Query with the SAME inflected predicate the caller asserted. The
    # triple projection is written with async_insert (fire-and-forget by
    # design), so poll briefly before judging.
    objs: set[str] = set()
    for _ in range(25):
        out = await handle_facts_list(
            FactsListInput(org=org, namespace=ns, subject="alice", relation="works_at", limit=10)
        )
        objs = {r.object for r in out.rows}
        if objs:
            break
        await asyncio.sleep(0.2)
    assert "clickhouse" in objs, out.rows

    # The canonical form keeps working too.
    out2 = await handle_facts_list(
        FactsListInput(org=org, namespace=ns, subject="alice", relation="work_at", limit=10)
    )
    assert {r.object for r in out2.rows} == objs
