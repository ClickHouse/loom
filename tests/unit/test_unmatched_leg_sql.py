"""The unmatched-candidate leg, run on clickhouse-local against the real
`loom.memory_embeddings`: rows that tie on its signals come back in row order."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.retrieval.side_legs import _fetch_unmatched_candidates
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run
from tests.unit.test_memory_search_service import _q

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_VEC = "[" + ",".join(["0.1"] * 3072) + "]"


def _row(session: str, key: str = "k") -> str:
    return (
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id,"
        " memory_key, memory_type, scope, content_excerpt, embedding, memory_key_embedding,"
        " embedding_model, embedding_dim, version) VALUES ('clickhouse', 'ns', 'bot', 'u',"
        f" '{session}', '{key}', 'semantic', 'private', 'one value', {_VEC}, {_VEC},"
        " 'text-embedding-3-small', 3072, 1)"
    )


class _LocalCH:
    def __init__(self, *rows: str) -> None:
        self.setup = ["SET enable_quantized_codec = 1", ddl("memory_embeddings"), *rows]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        return run(self.setup, sql, params)


@pytest.mark.asyncio
async def test_tied_unmatched_rows_come_back_in_row_order() -> None:
    # one value in four sessions ties on every signal the leg orders by
    ch = _LocalCH(*(_row(s) for s in ("s4", "s2", "s3", "s1")))
    q = _q(query_vec=[0.1] * 3072, user_id="u", agent="bot")
    hits = await _fetch_unmatched_candidates(ch, q, [], limit=3)
    assert [h.session_id for h in hits] == ["s1", "s2", "s3"]
