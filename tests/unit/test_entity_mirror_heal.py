"""heal_divergent_entities re-emits mirrors that disagree with loom.memory,
the repair for loom.integrity.divergent_entities (apply_entities_batch's one
bounded mirror retry is tested with the batch, test_workers_poll_the_work)."""

from __future__ import annotations

import asyncio
from typing import Any

from server.core.hashing import sha256_value
from server.memory import store as mr
from tests.unit.test_memory_verify import _row


class _CH:
    """query_all answers come from a queue, one list per call; inserts are captured."""

    def __init__(self, answers: list[list[dict[str, Any]]]) -> None:
        self.answers = list(answers)
        self.heads = {}
        for pair in answers[0] if answers else []:
            source = _row(
                org="o",
                namespace="n",
                agent="a",
                memory_key=pair["memory_key"],
                value="x",
                entities=pair["entities"],
            )
            self.heads[mr.identity_key(source)] = mr.build_memory_version_row(source)
        self.queries: list[str] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql, params=None):
        self.queries.append(" ".join(sql.split())[:80])
        if "memory_refusals" in sql:
            return []
        if "FROM loom.memory FINAL" in sql and "PREWHERE" in sql:
            return [r for key, r in self.heads.items() if key in params["keys"]]
        return self.answers.pop(0) if self.answers else []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows))


def _emb_row(entities: list[str]) -> dict[str, Any]:
    return {
        **mr.EMBEDDING_PERSISTED_COLS,
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_type": "semantic",
        "content_excerpt": "x",
        "content_sha256": sha256_value("x"),
        "embedding_model": "m",
        "embedding_dim": 3,
        "embedding": [0.1, 0.2, 0.3],
        "entities": entities,
        "version": 1,
    }


def test_heal_re_emits_divergent_mirrors_with_memory_entities() -> None:
    pair = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "entities": ["throttlerconstraint"],
    }
    ch = _CH(answers=[[pair], [_emb_row(["gate b"]), _emb_row(["gate b"])]])  # two sibling rows
    healed = asyncio.run(mr.heal_divergent_entities(limit=50, ch=ch))
    assert healed == 2
    assert ch.queries[0].startswith("SELECT m.org AS org"), "the divergence join runs first"
    table, rows = ch.inserts[0]
    assert table == "loom.memory_embeddings" and len(rows) == 2
    assert all(r["entities"] == ["throttlerconstraint"] for r in rows)
    assert all(r["version"] > 1 for r in rows), "bumped version wins under FINAL"


def test_heal_is_a_no_op_when_nothing_diverges() -> None:
    ch = _CH(answers=[[]])
    assert asyncio.run(mr.heal_divergent_entities(ch=ch)) == 0 and ch.inserts == []
