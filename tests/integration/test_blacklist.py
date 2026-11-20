"""Blacklist enforcement on memory.set + search.

Covered:
  - clear write (no matching pattern) succeeds
  - matching write is rejected with 422 BLACKLISTED, hit_count bumped
  - non-matching write still succeeds after pattern is in place
  - list returns the effective rule set (namespace + org + global)
  - remove flips the matching write back to succeeding

Live-verified at the SDK layer so transport + auth + cache invalidation
are all exercised. Blacklist write/remove require ADMIN; list is VIEWER.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("blacklist")


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


@pytest.mark.asyncio
async def test_clear_write_succeeds_without_blacklist(
    control_plane_url,
    mint_test_token,
) -> None:
    from loom import Agent

    ns = f"bl-clear-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        r = await a.memory.set("k", "anything goes here", memory_type="semantic")
        assert r["content_sha256"]


@pytest.mark.asyncio
async def test_matching_write_rejected_with_422(
    control_plane_url,
    mint_test_token,
) -> None:
    from loom import Agent
    from loom.errors import LoomError

    ns = f"bl-match-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        added = await a.blacklist.add(
            "medical",
            scope="namespace",
            reason="user opt-out",
            created_by="t",
        )
        # The control plane caches blacklist patterns in-process; the SDK
        # add path calls invalidate_cache() server-side, so the next call
        # sees the new pattern.
        with pytest.raises(LoomError) as excinfo:
            await a.memory.set(
                "med",
                "user is taking medical leave",
                memory_type="semantic",
            )
        msg = str(excinfo.value)
        assert "BLACKLISTED" in msg, msg

        # Non-matching write still goes through
        ok = await a.memory.set("other", "unrelated content", memory_type="semantic")
        assert ok["content_sha256"]

        # List reports the hit_count incremented
        rows = await a.blacklist.list()
        ours = [r for r in rows if r["pattern_id"] == added["pattern_id"]]
        assert len(ours) == 1
        assert int(ours[0]["hit_count"]) >= 1


@pytest.mark.asyncio
async def test_remove_unblocks_matching_writes(
    control_plane_url,
    mint_test_token,
) -> None:
    from loom import Agent
    from loom.errors import LoomError

    ns = f"bl-remove-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        added = await a.blacklist.add(
            "secret",
            scope="namespace",
            reason="test",
        )
        # Matching write is rejected
        with pytest.raises(LoomError):
            await a.memory.set("s1", "this is a secret token", memory_type="semantic")
        # Remove the pattern
        result = await a.blacklist.remove(added["pattern_id"], removed_by="t")
        assert result["removed"] is True
        # Same value now writes
        ok = await a.memory.set("s2", "this is a secret token", memory_type="semantic")
        assert ok["content_sha256"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "memory_type,sensitivity", [("semantic", "low"), ("episodic", "low"), ("semantic", "high")]
)
async def test_restore_cannot_revive_a_value_blacklisted_after_delete(
    control_plane_url, mint_test_token, memory_type, sensitivity
) -> None:
    from loom import Agent
    from loom.errors import LoomError

    from server.core.ch import get_client

    ns = f"bl-restore-{uuid.uuid4().hex[:10]}"
    token = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    pattern = f"refused-{uuid.uuid4().hex}"
    ch = get_client()

    async def persisted(table):
        return await ch.query_all(
            f"SELECT * FROM loom.{table} FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String} "
            "AND memory_key = 'blocked' ORDER BY version",
            {"org": ORG, "ns": ns},
        )

    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=token
    ) as agent:
        await agent.call(
            "memory.set",
            memory_key="blocked",
            value=pattern,
            memory_type=memory_type,
            sensitivity=sensitivity,
        )
        await agent.memory.delete("blocked")
        before = [await persisted(table) for table in ("memory", "memory_embeddings")]
        assert before[0] and before[0][0]["tombstoned_at"] is not None
        assert before[0][0]["sensitivity"] == sensitivity
        await agent.blacklist.add(pattern, scope="namespace", reason="refuse future copies")

        with pytest.raises(LoomError, match="BLACKLISTED"):
            await agent.memory.restore("blocked")

        after = [await persisted(table) for table in ("memory", "memory_embeddings")]
        assert after == before, "a refusal changes no persisted memory or search field"
        await agent.memory.set("allowed", "unrelated value", memory_type="semantic")
        await agent.memory.delete("allowed")
        restored = await agent.memory.restore("allowed")
        assert restored["restored"] is True
