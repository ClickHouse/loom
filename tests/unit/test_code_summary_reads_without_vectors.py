"""The card pass reads the live cards without their
vectors (it re-embeds every card it writes); the graph-stats pass, which
mirrors rows forward, keeps the full read."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.store.index import CodeRepo


class _CH:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sqls.append(sql)
        return []


def test_live_summary_reads_drop_the_vector_only_when_asked() -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    asyncio.run(repo.live_summary_rows("o", "n", "m"))
    asyncio.run(repo.live_summary_rows("o", "n", "m", with_embedding=False))
    asyncio.run(repo.live_summary_rows_other_models("o", "n", "m", with_embedding=False))
    assert "SELECT * FROM loom.code_summaries FINAL" in ch.sqls[0]
    assert "SELECT * EXCEPT (embedding) FROM loom.code_summaries FINAL" in ch.sqls[1]
    assert "SELECT * EXCEPT (embedding) FROM loom.code_summaries FINAL" in ch.sqls[2]
    assert "embedding_model != {model:String}" in ch.sqls[2]


def test_card_pass_reads_without_vectors_and_graph_stats_with() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    summary = (root / "server/code/workers/summary.py").read_text()
    stats = (root / "server/code/workers/graph_stats.py").read_text()
    assert summary.count("with_embedding=False") == 2
    assert "with_embedding=False" not in stats  # it mirrors the vector forward
