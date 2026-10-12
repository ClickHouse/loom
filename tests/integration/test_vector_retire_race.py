"""An older 'high' write that retires vectors after a newer embedded write landed
must not retire the newer one's. Drives the store functions against live
ClickHouse in the interleaving: A draws its version and inserts its high row,
B lands a newer embedded version (vector + entity vector), then A retires.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

ORG = "itest-org"
AGENT, USER, SESSION, KEY = "itest-bot", "-", "-", "race/fact/k"


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


def _memory_row(ns: str, version: int, value: str, sensitivity: str) -> dict[str, Any]:
    from server.memory import store as M

    now = datetime.now(UTC).replace(tzinfo=None)
    return M.build_first_version_row(
        {
            "org": ORG,
            "namespace": ns,
            "agent": AGENT,
            "user_id": USER,
            "session_id": SESSION,
            "memory_key": KEY,
            "memory_type": "semantic",
            "value": json.dumps(value),
            "value_json": json.dumps({"v": value}),
            "content_sha256": hashlib.sha256(value.encode()).hexdigest(),
            "confidence": 1.0,
            "actor": "process:itest",
            "source_class": "asserted",
            "sensitivity": sensitivity,
            "version": version,
        },
        now=now,
    )


async def _embedded_write(ch: Any, ns: str, value: str, entity: str) -> None:
    """One embedded generation: its memory row, then its vector and entity vector,
    stamped the way memory.set / bulk_set stamp them."""
    from server.memory import store as M

    await M.insert_memory_versions([_memory_row(ns, M.next_version(), value, "")], ch=ch)
    emb = M.build_embedding_row(
        org=ORG,
        namespace=ns,
        agent=AGENT,
        user_id=USER,
        session_id=SESSION,
        memory_key=KEY,
        memory_type="semantic",
        scope="private",
        content_excerpt=value,
        embedding_model="fake",
        embedding_dim=3,
        embedding=[0.1, 0.2, 0.3],
        created_at=datetime.now(UTC).replace(tzinfo=None),
    )
    await M.insert_embedding_rows([emb], ch=ch)
    await ch.insert(
        "loom.entity_vectors",
        [
            {
                "org": ORG,
                "namespace": ns,
                "agent": AGENT,
                "user_id": USER,
                "entity_text": entity,
                "entity_embedding": [0.1, 0.2, 0.3],
                "embedding_model": "fake",
                "embedding_dim": 3,
                "memory_key": KEY,
                "tombstoned_at": None,
                "created_at": datetime.now(UTC).replace(tzinfo=None),
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def test_a_paused_older_high_write_keeps_the_newer_writes_vectors(ch: Any) -> None:
    from server.memory import store as M

    ns = f"itest-{uuid.uuid4().hex[:12]}"
    try:
        await _embedded_write(ch, ns, "old value", "old-entity")
        previous = SimpleNamespace(
            org=ORG,
            namespace=ns,
            agent=AGENT,
            user_id=USER,
            session_id=SESSION,
            memory_key=KEY,
            memory_type="semantic",
            sensitivity="",
        )

        # A: draws its version, inserts its high row, then pauses.
        v_a = M.next_version()
        await M.insert_memory_versions([_memory_row(ns, v_a, "sk-high", "high")], ch=ch)
        # entity_vectors.created_at is whole seconds: B lands in a later one.
        await asyncio.sleep(1.1)
        await _embedded_write(ch, ns, "newer value", "new-entity")
        # A resumes and retires what it replaced.
        await M.tombstone_vectors_of(previous, version=v_a, ch=ch)

        live = await ch.query_all(
            "SELECT value, sensitivity FROM loom.memory FINAL"
            " WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns},
        )
        assert [(json.loads(r["value"]), r["sensitivity"]) for r in live] == [("newer value", "")]
        vectors = await ch.query_all(
            "SELECT content_excerpt FROM loom.memory_embeddings FINAL"
            " WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns},
        )
        assert [r["content_excerpt"] for r in vectors] == ["newer value"]
        entities = await ch.query_all(
            "SELECT entity_text FROM loom.entity_vectors FINAL"
            " WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
            params={"o": ORG, "n": ns},
        )
        assert [r["entity_text"] for r in entities] == ["new-entity"]
    finally:
        for table in ("memory", "memory_embeddings", "entity_vectors"):
            await ch.execute(
                f"DELETE FROM loom.{table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
                params={"o": ORG, "n": ns},
            )
