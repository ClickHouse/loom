"""`pending_embed_chunks` phase 1 returns a key only
when its NEWEST version is a live, embeddable stub. A heal leaves the
superseded stub version of every re-chunked row in the table; a flat
`DISTINCT … PREWHERE embedding_dim = 0` window would fill with those, phase 2
(FINAL) would drop them all, and `loom code backfill --phase embed` would stop
with most live stubs still pending."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.store.index import CodeRepo


class _CH:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.params: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.sql.append(sql)
        self.params.append(params)
        if len(self.sql) == 1:
            return [{"path": "src/a.cpp", "chunk_id": "00000000-0000-0000-0000-000000000001"}]
        return [
            {
                "path": "src/a.cpp",
                "chunk_id": "00000000-0000-0000-0000-000000000001",
                "embedding_dim": 0,
            }
        ]


def test_phase_one_judges_each_key_by_its_newest_version() -> None:
    ch = _CH()
    rows = asyncio.run(CodeRepo(ch).pending_embed_chunks("o", "n", "m", 3, 10))  # type: ignore[arg-type]
    p1, p2 = ch.sql
    # the newest version decides: stub, live, not by-design, not poisoned
    assert "GROUP BY path, chunk_id" in p1
    assert "HAVING argMax(embedding_dim, version) = 0" in p1
    assert "argMax(tombstoned_at IS NULL, version) = 1" in p1
    assert "NOT startsWith(argMax(embed_last_error, version), {unembedded:String})" in p1
    assert "argMax(embed_attempts, version) < {max_attempts:UInt16}" in p1
    assert "argMax(embed_failed_at IS NULL, version) = 1" in p1
    assert "PREWHERE" not in p1 and "FINAL" not in p1
    assert ch.params[0]["cand_lim"] == 40
    # phase 2 is the FINAL read by the candidates' keys, unchanged
    assert (
        "FROM loom.code_chunks FINAL" in p2
        and "(path, chunk_id) IN {keys:Array(Tuple(String, UUID))}" in p2
    )
    assert ch.params[1]["keys"] == [("src/a.cpp", "00000000-0000-0000-0000-000000000001")]
    assert len(rows) == 1


def test_include_poisoned_lifts_only_the_attempt_filters() -> None:
    ch = _CH()
    asyncio.run(CodeRepo(ch).pending_embed_chunks("o", "n", "m", 3, 10, include_poisoned=True))  # type: ignore[arg-type]
    p1 = ch.sql[0]
    assert "argMax(embed_attempts, version)" not in p1 and "embed_failed_at" not in p1
    assert (
        "NOT startsWith(argMax(embed_last_error, version), {unembedded:String})" in p1
    )  # by-design rows never
