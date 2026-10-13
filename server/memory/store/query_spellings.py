"""How the rows a search may read spell a set of lowercase words."""

from __future__ import annotations

from typing import Any

from server.memory.domain.query_casing import WORD_PATTERN
from server.memory.domain.search_sql import _build_where
from server.memory.domain.search_types import SearchQuery


async def spelling_rows(ch: Any, q: SearchQuery, tokens: list[str]) -> dict[str, int]:
    """spelling -> how many rows use it, for each word in `tokens` written in
    any case as a whole word (`WORD_PATTERN`), over the latest version of the
    rows `q`'s fact leg may read. A row counts once per spelling."""
    if not tokens:
        return {}
    where, params = _build_where(q)
    # A cheap substring test first; the word split below decides.
    where.append("multiSearchAnyCaseInsensitive(e.content_excerpt, {tokens:Array(String)})")
    params.update({"tokens": tokens, "word_re": WORD_PATTERN})
    rows = await ch.query_all(
        f"""
        SELECT spelling, count() AS n
        FROM (
            SELECT arrayJoin(arrayDistinct(arrayFilter(
                       w -> lower(w) IN {{tokens:Array(String)}},
                       extractAll(e.content_excerpt, {{word_re:String}})))) AS spelling
            FROM loom.memory_embeddings AS e FINAL
            WHERE {" AND ".join(where)}
        )
        GROUP BY spelling
        """,
        params=params,
    )
    return {str(r["spelling"]): int(r["n"]) for r in rows}
