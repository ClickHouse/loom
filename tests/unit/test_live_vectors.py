"""`embeddings.live_vectors`, a semantic admission's gap measure, against the production
`loom.memory_embeddings` on clickhouse-local."""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


class _LocalCH:
    def __init__(self, *setup: str) -> None:
        self.setup = ["SET enable_quantized_codec = 1", ddl("memory_embeddings"), *setup]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)


def _row(key: str, model: str, value: float, *, version: int = 1, deleted: bool = False) -> str:
    return (
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
        "memory_key, memory_type, scope, content_excerpt, embedding_model, version, "
        "embedding, memory_key_embedding, tombstoned_at) VALUES "
        f"('o', 'n', 'a', 'u1', '-', '{key}', 'fact', 'private', 'x', '{model}', {version}, "
        f"arrayWithConstant(3072, toBFloat16({value})), arrayWithConstant(3072, toBFloat16(0)), "
        f"{'now()' if deleted else 'NULL'})"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_gap_is_measured_by_one_models_vectors() -> None:
    """A key keeps a live row per model, each in its own space: only the asked model's
    vector is read, and a key with none under it reads as missing."""
    from server.memory.store import embeddings

    ch = _LocalCH(_row("k1", "m1", 1), _row("k1", "m2", 2), _row("k2", "m2", 3))
    got = await embeddings.live_vectors(
        "o",
        "n",
        [("a", "u1", "-", "k1"), ("a", "u1", "-", "k2")],
        model="m1",
        ch=ch,  # type: ignore[arg-type]
    )
    assert list(got) == [("a", "u1", "-", "k1")]
    assert set(got[("a", "u1", "-", "k1")]) == {1.0}


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_gap_row_is_measured_by_its_newest_version_and_a_deleted_one_by_none() -> None:
    """A key is measured by its newest live row of the model; a key whose newest row is a
    tombstone reads as missing, though an older row is live."""
    from server.memory.store import embeddings

    ch = _LocalCH(
        _row("k1", "m1", 1),
        _row("k1", "m1", 4, version=2),
        _row("k2", "m1", 5),
        _row("k2", "m1", 6, version=2, deleted=True),
    )
    got = await embeddings.live_vectors(
        "o",
        "n",
        [("a", "u1", "-", "k1"), ("a", "u1", "-", "k2")],
        model="m1",
        ch=ch,  # type: ignore[arg-type]
    )
    assert {key: set(vector) for key, vector in got.items()} == {("a", "u1", "-", "k1"): {4.0}}
