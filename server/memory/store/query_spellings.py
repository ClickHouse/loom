"""How the rows a search may read spell a set of lowercase words."""

from __future__ import annotations

from typing import Any

from server.memory.domain.query_casing import WORD_PATTERN
from server.memory.domain.search_sql import _build_where
from server.memory.domain.search_types import SearchQuery

# Rows sampled per lookup: the spelling is a majority vote.
SAMPLE_ROWS = 200


async def spelling_counts(ch: Any, q: SearchQuery, tokens: list[str]) -> dict[str, int]:
    """spelling -> occurrences of each word in `tokens`, matched as a whole
    word (`WORD_PATTERN`) case-insensitively, over up to `SAMPLE_ROWS` of the
    rows `q`'s fact leg may read (its full WHERE: identity bounds, scope,
    status, TTL) at their latest version that contain one."""
    if not tokens:
        return {}
    where, params = _build_where(q)
    params.update({"tokens": tokens, "word_re": WORD_PATTERN, "lim": SAMPLE_ROWS})
    # The substring test is a cheap prefilter; the word test decides, before the LIMIT, so
    # rows holding only a longer word never fill the sample.
    where.append("multiSearchAnyCaseInsensitive(e.content_excerpt, {tokens:Array(String)})")
    where.append(
        "arrayExists(w -> lower(w) IN {tokens:Array(String)}, "
        "extractAll(e.content_excerpt, {word_re:String}))"
    )
    rows = await ch.query_all(
        f"""
        SELECT w AS spelling, count() AS n
        FROM (
            SELECT arrayJoin(arrayFilter(w -> lower(w) IN {{tokens:Array(String)}},
                                         extractAll(content_excerpt, {{word_re:String}}))) AS w
            FROM (
                SELECT e.content_excerpt AS content_excerpt
                FROM loom.memory_embeddings AS e FINAL
                WHERE {" AND ".join(where)}
                LIMIT {{lim:UInt32}}
            )
        )
        GROUP BY w
        """,
        params=params,
    )
    return {str(r["spelling"]): int(r["n"]) for r in rows}
