"""Empty vectors become fixed-length zero placeholders on
insert when the setting is on; the key axis trusts the dim column."""

from __future__ import annotations

from typing import Any

import pytest

import server.core.ch as ch_mod
from server.core.vectors import placeholders as vp
from server.settings import settings


def test_pad_rows_replaces_only_empty_vectors_of_memory_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "embedding_dim", 4)
    rows = [
        {
            "embedding": [],
            "embedding_dim": 0,
            "memory_key_embedding": [],
            "memory_key_embedding_dim": 0,
        },
        {
            "embedding": [0.1, 0.2, 0.3, 0.4],
            "embedding_dim": 4,
            "memory_key_embedding": [],
            "memory_key_embedding_dim": 0,
        },
        {
            "embedding": [0.1, 0.2, 0.3, 0.4],
            "embedding_dim": 4,
            "memory_key_embedding": [0.5] * 4,
            "memory_key_embedding_dim": 4,
        },
    ]
    assert len(vp.pad_rows("loom.memory_embeddings", rows)) == 3
    assert (
        rows[0]["embedding"] == [0.0] * 4 and rows[0]["embedding_dim"] == 0
    )  # dim stays the writer's truth
    assert rows[0]["memory_key_embedding"] == [0.0] * 4
    assert rows[1]["memory_key_embedding"] == [0.0] * 4 and rows[1]["embedding"] == [
        0.1,
        0.2,
        0.3,
        0.4,
    ]
    assert rows[2]["memory_key_embedding"] == [0.5] * 4
    # other tables are untouched
    other = [{"embedding": []}]
    assert len(vp.pad_rows("loom.agent_events", other)) == 0 and other[0]["embedding"] == []


@pytest.mark.asyncio
async def test_ch_insert_pads_memory_embeddings_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import rmt_carry

    monkeypatch.setattr(settings, "embedding_dim", 3)
    monkeypatch.setattr(rmt_carry, "carries", lambda _table: False)  # its own tests
    captured: list[Any] = []

    class _Inner:
        async def insert(self, table: str, data: Any, **kw: Any) -> None:
            captured.append(data)

    c = ch_mod.CHClient()
    c._client = _Inner()  # type: ignore[assignment]

    async def _no_types(self: Any, table: str, columns: list[str]) -> None:
        return None

    monkeypatch.setattr(ch_mod.CHClient, "_column_type_names", _no_types)
    await c.insert(
        "loom.memory_embeddings",
        [{"memory_key": "k", "embedding": [], "memory_key_embedding": [1.0, 1.0, 1.0]}],
    )
    assert captured[-1][0] == ["k", [0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]


def test_key_axis_guard_trusts_the_dim_column() -> None:
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2]
        / "server"
        / "memory"
        / "app"
        / "retrieval"
        / "fact_sql.py"
    ).read_text()
    assert "e.memory_key_embedding_dim = length({q:Array(Float32)})" in src
    # the length check stays as a second guard
    assert "length(e.memory_key_embedding)" in src


def test_every_vector_table_has_a_placeholder_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The coded layout reaches every vector table."""
    monkeypatch.setattr(settings, "embedding_dim", 3)
    for table, cols in vp.PLACEHOLDER_COLUMNS.items():
        rows = [{c: [] for c in cols}]
        assert len(vp.pad_rows(table, rows)) == len(cols), table
        assert all(rows[0][c] == [0.0, 0.0, 0.0] for c in cols)
    assert (
        "loom.code_chunks" in vp.PLACEHOLDER_COLUMNS
        and "loom.entity_vectors" in vp.PLACEHOLDER_COLUMNS
    )
    # the file cards fill it
    assert vp.PLACEHOLDER_COLUMNS["loom.code_summaries"] == ("embedding",)
    # the last two plain columns follow the rule too
    assert vp.PLACEHOLDER_COLUMNS["loom.code_answers"] == ("question_embedding",)
    assert vp.PLACEHOLDER_COLUMNS["loom.categories"] == ("embedding",)
