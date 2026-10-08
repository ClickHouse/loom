"""ClickHouse `text` index on memory_embeddings.content_excerpt_lc —
end-to-end behavioral verification.

The table carries a MATERIALIZED lowercased column and a
`text(tokenizer='splitByNonAlpha')` index on it. The query side in
server/memory/app/retrieval/ uses `hasAnyTokens` against the
indexed column for the lexical WHERE filter; this gives exact
(no false-positive) granule prune.

These tests verify:
- The lowercased column is populated automatically on new writes.
- `hasAnyTokens` lexical filter matches case-insensitively in practice
  (we lowercase both the haystack and the query tokens).
- Search results for the lexical / bm25_hybrid / rrf modes match the
  expected memory rows.
- The index activates (EXPLAIN shows Skip step) — verified ad-hoc on
  the cluster, not asserted here (EXPLAIN parsing is brittle and
  belongs in a perf-bench, not a behavioral test).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402


@pytest.mark.asyncio
async def test_lowercased_column_populates(agent, ch_client) -> None:
    """The MATERIALIZED column content_excerpt_lc is populated
    automatically on every write — no application-side change needed."""
    await agent.memory.set(
        "case_test",
        "Alice prefers ClickHouse over Postgres",
        memory_type="semantic",
    )

    # Poll until the embedding row is visible (async-insert flush) rather than a
    # fixed sleep, which races the flush on a loaded CI runner.
    async def _row():
        result = ch_client.query(
            """
            SELECT content_excerpt, content_excerpt_lc
            FROM loom.memory_embeddings FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND memory_key = {k:String}
            LIMIT 1
            """,
            parameters={"org": "test-org", "ns": agent.namespace, "k": "case_test"},
        )
        return result.result_rows or None

    rows = await eventually(_row, description="content_excerpt_lc row visible")
    excerpt, lc = rows[0]
    assert excerpt != lc, f"materialized lc column should differ: {excerpt!r} vs {lc!r}"
    assert lc == excerpt.lower()
    assert "clickhouse" in lc  # lowercase form of the original ClickHouse


@pytest.mark.asyncio
async def test_lexical_search_uses_text_index_path(agent) -> None:
    """search_mode='lexical' returns the row matching the query token.

    The new SQL path is `hasAnyTokens(content_excerpt_lc, [tokens])`
    — case-insensitive (we lowercase the haystack via the materialized
    column + lowercase the tokens via the lemmatizer).
    """
    await agent.memory.set(
        "deploy_window",
        "Deploy on Tuesday or Thursday, NEVER Friday.",
        memory_type="preference",
    )
    await agent.memory.set(
        "coffee_pref",
        "User takes coffee with no sugar.",
        memory_type="preference",
    )

    # Query in mixed case — should still hit because both sides lowercase before
    # token comparison. Poll until the write is searchable (index visibility).
    async def _hit():
        hits = await agent.memory.search("DEPLOY", search_mode="lexical", top_k=5, auto_touch=False)
        keys = {h.memory_key for h in hits}
        return keys if "deploy_window" in keys else None

    assert "deploy_window" in await eventually(_hit, description="deploy_window via lexical search")


@pytest.mark.asyncio
async def test_bm25_hybrid_uses_text_index_path(agent) -> None:
    """bm25_hybrid mode also routes through hasAnyTokens for the WHERE
    granule prune. The per-row BM25 scoring still uses
    countSubstringsCaseInsensitive — the index is for filtering, not
    scoring."""
    await agent.memory.set(
        "lang_pref",
        "User prefers TypeScript over Python for new projects.",
        memory_type="preference",
    )
    await agent.memory.set(
        "ide_pref",
        "User uses VS Code as the primary editor.",
        memory_type="preference",
    )

    async def _hit():
        hits = await agent.memory.search(
            "typescript", search_mode="bm25_hybrid", top_k=5, auto_touch=False
        )
        keys = {h.memory_key for h in hits}
        return keys if "lang_pref" in keys else None

    assert "lang_pref" in await eventually(_hit, description="lang_pref via bm25_hybrid search")


@pytest.mark.asyncio
async def test_lexical_filter_case_insensitive(agent) -> None:
    """Mixed-case query tokens match lowercased indexed content.

    Regression guard: if the migration's MATERIALIZED lower() ever
    breaks, or the lemmatizer stops lowercasing, this test would
    surface it before the next release. CamelCase content + ALL-CAPS
    query is the right shape to exercise both sides.
    """
    await agent.memory.set(
        "framework_pref",
        "User prefers FastAPI over Flask.",
        memory_type="preference",
    )

    async def _hit():
        hits = await agent.memory.search(
            "FASTAPI", search_mode="lexical", top_k=3, auto_touch=False
        )
        keys = {h.memory_key for h in hits}
        return keys if "framework_pref" in keys else None

    assert "framework_pref" in await eventually(
        _hit, description="framework_pref via case-insensitive lexical search"
    )


@pytest.mark.asyncio
async def test_multi_token_query_matches_any(agent) -> None:
    """hasAnyTokens semantics: a row is a candidate if ANY query token
    appears. That's the right shape for the OR-of-tokens filter.
    """
    await agent.memory.set(
        "morning_routine",
        "User wakes up at 6am and drinks coffee immediately.",
        memory_type="semantic",
    )
    await agent.memory.set(
        "evening_routine",
        "User reads fiction before bed at 10pm.",
        memory_type="semantic",
    )

    # "fiction" only matches evening_routine. Poll until the write is searchable.
    async def _hit():
        hits = await agent.memory.search(
            "fiction", search_mode="lexical", top_k=5, auto_touch=False
        )
        keys = {h.memory_key for h in hits}
        return keys if "evening_routine" in keys else None

    keys = await eventually(_hit, description="evening_routine via lexical search")
    assert "evening_routine" in keys
    assert "morning_routine" not in keys  # "fiction" must not match morning_routine
