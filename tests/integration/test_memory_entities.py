"""Memory entity-filter — search-side boost over the LLM-extracted
`entities` column.

The entity_extractor worker depends on an LLM call we don't make in the
test suite (anthropic API key, network). Instead we fabricate entity
tags via direct CH ALTER UPDATE and verify the search handler honors
them — which is what we actually need to test. The worker has its own
focused unit on the JSON parser; full end-to-end is gated by env.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def _wait_for_embedding_row(
    ch_client, org: str, ns: str, memory_key: str, *, timeout_s: float = 3.0
) -> None:
    """Poll memory_embeddings until (org, ns, memory_key) lands.

    The embedding insert is async — the row
    may not be visible immediately after memory.set returns. ALTER
    UPDATE entities = … on a non-existent row would silently no-op,
    so the subsequent entity_filter probe wouldn't find the tag.
    """
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = ch_client.query(
            "SELECT count() FROM loom.memory_embeddings "
            "WHERE org = {org:String} AND namespace = {ns:String} AND memory_key = {k:String}",
            parameters={"org": org, "ns": ns, "k": memory_key},
        ).result_rows
        if rows and rows[0][0] >= 1:
            return
        time.sleep(0.1)
    raise AssertionError(
        f"embedding row for ({org!r}, {ns!r}, {memory_key!r}) not visible "
        f"after {timeout_s}s; V3 async-insert flush race?"
    )


def _tag(ch_client, org: str, ns: str, memory_key: str, entities: list[str]) -> None:
    """Stand-in for what entity_extractor.run_pass() would do."""
    _wait_for_embedding_row(ch_client, org, ns, memory_key)
    ents_literal = "[" + ",".join(f"'{e}'" for e in entities) + "]"
    ch_client.command(
        f"""
        ALTER TABLE loom.memory_embeddings
        UPDATE entities = {ents_literal}
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND memory_key = {{k:String}}
        SETTINGS mutations_sync = 2
        """,
        parameters={"org": org, "ns": ns, "k": memory_key},
    )


@pytest.mark.asyncio
async def test_entity_filter_boosts_match_past_cosine_winner(
    agent, ch_client, require_real_embedder
) -> None:
    """Two memories with different topics. With no filter, cosine-relevant
    one wins. With entity_filter on the OTHER one, it gets boosted past."""
    await agent.memory.set(
        "alice_cluster",
        "Alice owns the staging cluster staging-01",
        memory_type="semantic",
    )
    await agent.memory.set(
        "weather_note",
        "Today the weather is sunny in Mumbai",
        memory_type="semantic",
    )
    _tag(
        ch_client,
        agent.org,
        agent.namespace,
        "alice_cluster",
        ["alice", "staging-01"],
    )

    plain = await agent.memory.search("what is the weather", top_k=2)
    plain_first = plain[0].memory_key

    boosted = await agent.memory.search(
        "what is the weather",
        top_k=2,
        entity_filter=["alice"],
    )
    boosted_first = boosted[0].memory_key

    # Without filter: cosine winner depends on phrasing — assert weather wins.
    assert plain_first == "weather_note", (
        f"sanity: cosine should rank weather first; got {plain_first}"
    )
    # With entity_filter=['alice']: the Alice-tagged row beats the cosine winner.
    assert boosted_first == "alice_cluster", (
        f"entity_filter should boost alice_cluster; got {boosted_first}"
    )


@pytest.mark.asyncio
async def test_entity_filter_case_insensitive_normalization(agent, ch_client) -> None:
    """entity_filter values are lowercased before matching, so callers can
    pass user-typed casing."""
    await agent.memory.set(
        "claude_pref",
        "I prefer Claude for code review",
        memory_type="semantic",
    )
    _tag(ch_client, agent.org, agent.namespace, "claude_pref", ["claude"])

    # Unrelated cosine query + uppercase entity filter
    hits = await agent.memory.search(
        "what fruit is in season",
        top_k=3,
        entity_filter=["CLAUDE"],
    )
    keys = [h.memory_key for h in hits]
    assert "claude_pref" in keys, (
        f"case-insensitive entity match should surface claude_pref; got {keys}"
    )


def test_extractor_json_parser_tolerant() -> None:
    """The worker's _parse_entities() handles code fences, prose,
    extras — return a clean lowercased deduped list."""
    from server.memory.workers.entity_extractor import _parse_entities

    assert _parse_entities('["Alice", "Bob", "alice"]') == ["alice", "bob"]
    assert _parse_entities('Here are the entities: ["X", "Y"]') == ["x", "y"]
    assert _parse_entities('```json\n["a","b"]\n```') == ["a", "b"]
    # A reply with no JSON array is a PARSE FAILURE (None), not "no entities":
    # writing the `_none_` sentinel on it would remove the row from the
    # rescan forever on the strength of a garbled reply.
    assert _parse_entities("garbage with no array") is None
    assert _parse_entities("[]") == []
    # Cap at 8
    big = "[" + ",".join(f'"v{i}"' for i in range(20)) + "]"
    assert len(_parse_entities(big)) == 8
