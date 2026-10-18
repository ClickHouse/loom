"""conflicts.resolve decides only open pairs, on a real ClickHouse.

A pair is open while both rows are live and each lists the other in
`conflict_with`. A decision removes the pair from both rows' `conflict_with`, so
a resolve sent from a view taken before it writes nothing, even after a later
write flags one of the rows again.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("conflicts-open")


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client
    from server.embedding import embedder as _embedder_mod

    _embedder_mod.reset()
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


async def _wait_for_embedding(agent, query: str, expected_key: str) -> None:
    """Conflict detection reads memory_embeddings, which lands asynchronously."""

    async def probe():
        hits = await agent.memory.search(query, memory_type="semantic", top_k=5)
        return hits if any(h.memory_key == expected_key for h in hits) else None

    await eventually(probe, description=f"embedding for {expected_key!r} to be visible")


async def _rows(ns: str) -> dict[str, dict[str, object]]:
    """Every live row of the namespace by memory_key: its id, status and conflict_with."""
    from server.core.ch import get_client

    rows = await get_client().query_all(
        """
        SELECT memory_key, toString(memory_id) AS mid, status,
               arrayMap(x -> toString(x), conflict_with) AS peers
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": ORG, "ns": ns},
    )
    return {
        str(r["memory_key"]): {"id": r["mid"], "status": r["status"], "peers": set(r["peers"])}
        for r in rows
    }


@pytest.mark.asyncio
async def test_an_allow_prunes_the_pair_and_a_stale_resolve_after_a_reflag_writes_nothing(
    control_plane_url,
    mint_test_token,
) -> None:
    from loom import Agent

    ns = f"open-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        # A loose threshold so the fake embedder's distances count as conflicts.
        await a.namespaces.register(owner_email="itest@loom", conflict_threshold=0.60)
        await a.memory.set("style", "Always use 2-space indentation", memory_type="semantic")
        await _wait_for_embedding(a, "2-space indentation", "style")
        await a.memory.set(
            "style_v2",
            "Always use 2-space indentation throughout the project",
            memory_type="semantic",
        )
        before = await _rows(ns)
        old, new = before["style"]["id"], before["style_v2"]["id"]
        assert before["style"]["peers"] == {new} and before["style_v2"]["peers"] == {old}

        allowed = await a.conflicts.resolve(new, "allow", peer_id=old, reason="both hold")
        assert allowed["resolved"] is True and allowed["total"] == 0
        after = await _rows(ns)
        assert after["style"] == {"id": old, "status": "active", "peers": set()}
        assert after["style_v2"] == {"id": new, "status": "active", "peers": set()}

        # A later write conflicts with both rows; the allowlist keeps the allowed pair apart.
        await _wait_for_embedding(a, "2-space indentation throughout", "style_v2")
        await a.memory.set(
            "style_v3",
            "Always use 2-space indentation throughout the whole project",
            memory_type="semantic",
        )
        reflagged = await _rows(ns)
        later = reflagged["style_v3"]["id"]
        assert later in reflagged["style"]["peers"] and new not in reflagged["style"]["peers"]
        assert reflagged["style"]["status"] == "conflicted"

        # The queue still shows the allowed pair: the click must not tombstone the kept row.
        stale = await a.conflicts.resolve(new, "revise", peer_id=old)
        assert stale["resolved"] is False and stale["affected_memory_ids"] == []
        assert await _rows(ns) == reflagged

        # The pair the later write opened is still decided.
        fresh = await a.conflicts.resolve(later, "revise", peer_id=old)
        assert fresh["resolved"] is True and fresh["affected_memory_ids"] == [old, later]
        final = await _rows(ns)
        assert "style" not in final and old not in final["style_v3"]["peers"]
