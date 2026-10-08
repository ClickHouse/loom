"""Semantic conflict detection on memory.set.

Four scenarios pinned at the SDK level — exercises the full HTTP path
through transport, auth, RBAC, embedding, conflict detection, and the
conflicts.list / conflicts.resolve wire ops:

  1. Semantic conflict: two close-but-different memories → both rows
     flagged status='conflicted', cross-linked via conflict_with.
  2. Canonical merge: a write whose canonical-normalized text matches
     an existing peer's returns that peer's memory_id (no INSERT).
  3. Pinned short-circuit: a non-pinned write conflicting with a
     pinned peer is rejected with 409 PINNED_CONFLICT.
  4. Resolve via revise: the chosen memory wins, peers tombstoned,
     conflicts.list returns empty.

Each conflict-triggering write first waits until the prior write's embedding
is searchable (`_wait_for_embedding`), because conflict detection reads the
asynchronously inserted memory_embeddings rows.
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

ORG = unique_org("conflicts")


async def _wait_for_embedding(agent, query: str, expected_key: str) -> None:
    """Block until `agent.memory.search(query)` surfaces `expected_key`.

    Conflict detection runs the next memory.set against memory_embeddings
    (server/memory/app/conflicts.py); if the prior write's embedding hasn't flushed
    yet (V3 async_insert=1 on the embeddings table) conflict detection
    misses the peer and the test fails for the wrong reason. Probing
    through search uses the same query path conflict detection does.
    """

    async def probe():
        hits = await agent.memory.search(query, memory_type="semantic", top_k=5)
        return hits if any(h.memory_key == expected_key for h in hits) else None

    await eventually(probe, description=f"embedding for {expected_key!r} to be visible")


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client
    from server.embedding import embedder as _embedder_mod

    _embedder_mod.reset()
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


@pytest.mark.asyncio
async def test_semantic_conflict_flags_both_rows(
    control_plane_url,
    mint_test_token,
) -> None:
    from loom import Agent

    ns = f"conf-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        # Loosen the per-namespace conflict_threshold so this test is
        # robust to embedder choice. fake-ngram-1024 (the CI embedder)
        # produces ~0.27 cosine distance for the test strings; the
        # default 0.85 threshold (distance ≤ 0.15) excludes them.
        # 0.60 (distance ≤ 0.40) covers both fake-ngram and OpenAI.
        await a.namespaces.register(
            owner_email="itest@loom",
            conflict_threshold=0.60,
        )
        await a.memory.set("style", "Always use 2-space indentation", memory_type="semantic")
        await _wait_for_embedding(a, "2-space indentation", "style")
        await a.memory.set(
            "style_v2",
            "Always use 2-space indentation throughout the project",
            memory_type="semantic",
        )
        result = await a.conflicts.list()
    assert len(result["rows"]) == 2, result
    # Each row's conflict_with points at the other
    keys_to_peers = {r["memory_key"]: set(r["conflict_with"]) for r in result["rows"]}
    keys_to_ids = {r["memory_key"]: r["memory_id"] for r in result["rows"]}
    assert keys_to_peers["style"] == {keys_to_ids["style_v2"]}
    assert keys_to_peers["style_v2"] == {keys_to_ids["style"]}


@pytest.mark.asyncio
async def test_canonical_match_returns_existing_id(
    control_plane_url,
    mint_test_token,
) -> None:
    """A write whose canonical-normalized text matches an active peer
    returns the peer's memory_id without inserting a new row."""
    from loom import Agent

    ns = f"canon-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        first = await a.memory.set(
            "style",
            "Always use 2-space indentation",
            memory_type="semantic",
        )
        await _wait_for_embedding(a, "2-space indentation", "style")
        # Punctuation + case difference only — canonical form matches
        second = await a.memory.set(
            "style_v2",
            "always use 2-SPACE indentation.",
            memory_type="semantic",
        )
        assert second["memory_id"] == first["memory_id"], (
            f"canonical-match should return first's id "
            f"{first['memory_id']!r}, got {second['memory_id']!r}"
        )


@pytest.mark.asyncio
async def test_pinned_short_circuits_conflicting_write(
    control_plane_url,
    mint_test_token,
) -> None:
    """A non-pinned write that would conflict with a pinned peer is
    rejected with 409 PINNED_CONFLICT. Caller must explicitly
    update_by_id the pinned row to change its value."""
    from loom import Agent, LoomError

    ns = f"pinned-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        # Loosen per-namespace threshold so fake-ngram embeddings on
        # near-paraphrase test strings still trigger the conflict
        # path under test. See test_semantic_conflict_flags_both_rows
        # for the rationale.
        await a.namespaces.register(
            owner_email="itest@loom",
            conflict_threshold=0.60,
        )
        pinned = await a.memory.pin(
            "style",
            "Always use 2-space indentation",
            memory_type="semantic",
        )
        await _wait_for_embedding(a, "2-space indentation", "style")
        with pytest.raises(LoomError) as excinfo:
            await a.memory.set(
                "style_v2",
                "Always use 2-space indentation throughout the project",
                memory_type="semantic",
            )
        msg = str(excinfo.value)
        assert "PINNED_CONFLICT" in msg, msg
        # Pinned row is still readable + unmodified
        got = await a.memory.get("style")
        assert got is not None
        assert got["value"] == "Always use 2-space indentation"
        assert got["memory_type"] == "semantic"
        # And the pinned row's memory_id is what the caller saw
        assert got["pinned"] is True
        del pinned  # variable referenced for debugging if the assertion fails


@pytest.mark.asyncio
async def test_resolve_revise_tombstones_peers(
    control_plane_url,
    mint_test_token,
) -> None:
    """resolve(memory_id, decision='revise') tombstones every peer
    in the chosen memory's conflict_with set; chosen stays active;
    subsequent conflicts.list returns empty for the namespace."""
    from loom import Agent

    ns = f"resolve-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        # Loosen per-namespace threshold; see top test for rationale.
        await a.namespaces.register(
            owner_email="itest@loom",
            conflict_threshold=0.60,
        )
        await a.memory.set("style", "Always use 2-space indentation", memory_type="semantic")
        await _wait_for_embedding(a, "2-space indentation", "style")
        winner = await a.memory.set(
            "style_v2",
            "Always use 2-space indentation throughout the project",
            memory_type="semantic",
        )
        # Both flagged before resolve
        pre = await a.conflicts.list()
        assert len(pre["rows"]) == 2
        # Resolve in favor of style_v2
        resolved = await a.conflicts.resolve(
            winner["memory_id"],
            "revise",
            reason="v2 is the complete statement",
        )
        assert resolved["resolved"] is True
        # No remaining conflicts
        post = await a.conflicts.list()
        assert len(post["rows"]) == 0
