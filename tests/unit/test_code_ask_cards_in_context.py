"""The card read: `CodeRepo.cards_for_paths` serves file-card
text only under the served label (read by ingest, round reads and
code.context_pack)."""

from __future__ import annotations

import asyncio

from server.code.store.index import CodeRepo


class _CH:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.params: list[dict] = []

    async def query_all(self, sql, params=None):
        self.sql.append(sql)
        self.params.append(params or {})
        return [
            {
                "scope_path": "src/Common/MemoryTracker.cpp",
                "summary": "Tracks memory. Enforces limits.",
            }
        ]


def test_cards_for_paths_reads_text_only_under_the_served_label() -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    out = asyncio.run(
        repo.cards_for_paths(
            "o", "n", "m", ["src/b.cpp", "src/Common/MemoryTracker.cpp", "src/b.cpp"]
        )
    )
    assert out == {"src/Common/MemoryTracker.cpp": "Tracks memory. Enforces limits."}
    assert "SELECT scope_path, summary" in ch.sql[0] and "scope_type = 'file'" in ch.sql[0]
    assert ch.params[0]["paths"] == ["src/Common/MemoryTracker.cpp", "src/b.cpp"]
    assert asyncio.run(repo.cards_for_paths("o", "n", "m", [])) == {}
