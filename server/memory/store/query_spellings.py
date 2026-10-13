"""How a namespace's live memory excerpts spell a set of lowercase words."""

from __future__ import annotations

from typing import Any

# Rows read per lookup: the spelling is a majority vote, and a bound keeps a
# word in every row from scanning the namespace.
SAMPLE_ROWS = 200


async def spelling_counts(
    ch: Any, *, org: str, namespace: str, tokens: list[str]
) -> dict[str, int]:
    """Each spelling of a word in `tokens` (matched whole-word,
    case-insensitively) in up to `SAMPLE_ROWS` of the namespace's live
    excerpts that contain one, with its occurrences. `tokens` are lowercase
    letters only (regex-safe)."""
    if not tokens:
        return {}
    rows = await ch.query_all(
        """
        SELECT w AS spelling, count() AS n
        FROM (
            SELECT arrayJoin(extractAll(content_excerpt, {pattern:String})) AS w
            FROM (
                SELECT content_excerpt
                FROM loom.memory_embeddings
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tombstoned_at IS NULL
                  AND multiSearchAnyCaseInsensitive(content_excerpt, {tokens:Array(String)})
                LIMIT {lim:UInt32}
            )
        )
        GROUP BY w
        """,
        params={
            "org": org,
            "ns": namespace,
            "tokens": tokens,
            "pattern": "(?i)\\b(" + "|".join(tokens) + ")\\b",
            "lim": SAMPLE_ROWS,
        },
    )
    return {str(r["spelling"]): int(r["n"]) for r in rows}
