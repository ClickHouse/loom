"""memory.history + memory.redact through the HTTP wire.

This file pins these ops at the SDK level so the transport + auth +
canonicalization stay covered.

Covered:
  - history returns all live versions newest-first, including
    tombstoned and redacted rows (no FINAL collapse)
  - redact on the live head is refused (refused_head=True)
  - redact on a historical row scrubs the value, sets redacted_at,
    preserves the timeline entry
  - history reflects the redact (value scrubbed, redacted_at populated)
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

ORG = unique_org("history-redact")

# No flaky-rerun marker. CH Cloud's default async_insert can buffer rows for ~1s
# before they're visible to subsequent SELECTs; memory.set fires fast and
# memory.history/get read back via a separate query path. Rather than rerun the
# whole test, every read-after-write below polls via eventually() until the
# expected state (version count, then the redaction mutation) is visible.


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
async def test_history_returns_all_versions_newest_first(
    control_plane_url,
    mint_test_token,
    pause_loom_memory_merges,
) -> None:
    # Merges paused: loom.memory is RMT(version) keyed without version, so a
    # background merge can collapse the version rows this test asserts on. Once
    # collapsed they're gone — polling can't recover them — so hold them in place.
    from loom import Agent

    ns = f"history-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic")
        await a.memory.set("k", {"v": 2}, memory_type="semantic")
        await a.memory.set("k", {"v": 3}, memory_type="semantic")

        async def _all_three():
            r = await a.memory.history("k")
            return r if len(r) >= 3 else None

        rows = await eventually(_all_three, description="all 3 versions visible")
        assert len(rows) == 3, rows
        # Newest-first ordering
        versions = [int(r["version"]) for r in rows]
        assert versions == sorted(versions, reverse=True)
        # Values match insertion (newest first)
        values = [r["value"] for r in rows]
        assert values == [{"v": 3}, {"v": 2}, {"v": 1}]
        # Each row has a content_sha256
        assert all(r["content_sha256"] for r in rows)


@pytest.mark.asyncio
async def test_redact_refuses_live_head(
    control_plane_url,
    mint_test_token,
) -> None:
    """memory.redact MUST NOT scrub the row that memory.get returns —
    that would silently destroy live data. Caller must memory.delete
    first; the post-delete head (tombstoned) is then redactable."""
    from loom import Agent

    ns = f"redact-head-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        s = await a.memory.set("k", {"secret": "should not survive"}, memory_type="semantic")
        head_id = s["memory_id"]
        # Ensure the row is visible before redact reasons about which row is head.
        await eventually(lambda: a.memory.get("k"), description="row visible")
        result = await a.memory.redact(head_id, s["version"], reason="head test", redacted_by="t")
        assert result["redacted"] is False
        assert result["refused_head"] is True
        # Value should still be readable — redact didn't fire
        got = await a.memory.get("k")
        assert got is not None
        assert got["value"] == {"secret": "should not survive"}


@pytest.mark.xfail(
    strict=False,
    reason=(
        "redact-of-a-historical-row is best-effort: loom.memory is "
        "ReplacingMergeTree(version) keyed by identity+memory_key (version not "
        "in the sort key), so a background merge can collapse the non-head "
        "version away before the scrub, and pausing merges would block the "
        "redact mutation itself. Cannot be made deterministic without a schema "
        "change. See MemoryRedactInput. xpasses when no merge intervenes."
    ),
)
@pytest.mark.asyncio
async def test_redact_scrubs_historical_row(
    control_plane_url,
    mint_test_token,
) -> None:
    """Write three versions, redact the oldest; live head + history intact,
    redacted row shows '[REDACTED]' + redacted_at. Best-effort under RMT —
    see the xfail marker + MemoryRedactInput."""
    from loom import Agent

    ns = f"redact-historical-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        r1 = await a.memory.set("k", {"v": 1, "secret": "leak"}, memory_type="semantic")
        await a.memory.set("k", {"v": 2}, memory_type="semantic")
        r3 = await a.memory.set("k", {"v": 3}, memory_type="semantic")

        # All three versions must be visible before redacting a historical row —
        # redact is an ALTER UPDATE WHERE memory_id=… AND version=…; if r1 hasn't
        # flushed it matches nothing.
        async def _three():
            r = await a.memory.history("k")
            return r if len(r) >= 3 else None

        await eventually(_three, description="3 versions visible before redact")

        # Redact the FIRST (oldest) row. All versions share one memory_id, so the
        # version pins the specific historical row.
        result = await a.memory.redact(
            r1["memory_id"], r1["version"], reason="secret leak", redacted_by="t"
        )
        assert result["redacted"] is True
        assert result["refused_head"] is False
        assert result["redacted_at"]

        # Live head untouched
        head = await a.memory.get("k")
        assert head is not None
        assert head["value"] == {"v": 3}

        # History shows the redacted row with scrubbed value + marker. The redact
        # is a mutation, so poll until its effect is visible (FINAL view lag).
        async def _redaction_visible():
            rows = await a.memory.history("k")
            by_v = {r["version"]: r for r in rows}
            rr = by_v.get(r1["version"])
            return rows if (rr and rr["value"] == "[REDACTED]") else None

        rows = await eventually(_redaction_visible, description="redaction visible in history")
        assert len(rows) == 3
        # History rows share one memory_id, so key by version to tell them apart.
        by_v = {r["version"]: r for r in rows}
        redacted_row = by_v[r1["version"]]
        assert redacted_row["value"] == "[REDACTED]"
        assert redacted_row["redacted_at"] is not None
        assert "secret leak" in redacted_row["redaction_reason"]
        # The other two rows are unaffected
        assert by_v[r3["version"]]["value"] == {"v": 3}
        assert by_v[r3["version"]]["redacted_at"] is None


@pytest.mark.asyncio
async def test_a_restore_after_delete_and_redact_brings_back_no_redacted_text(
    control_plane_url,
    mint_test_token,
) -> None:
    """Touch, delete, then redact the value: the touch and the delete's tombstone carry
    it, so the redaction scrubs them too, and a restore answers that there is nothing to
    bring back."""
    from loom import Agent

    ns = f"redact-deleted-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        written = await a.memory.set("k", {"secret": "leak"}, memory_type="semantic")
        # A copy writer before the delete (a touch, as an entity pass or a lock would)
        # carries the text forward too.
        await a.memory.touch("k", ttl_seconds=86400)
        assert await a.memory.delete("k") is True

        async def _deleted():
            rows = await a.memory.history("k")
            return rows if len(rows) >= 2 else None

        await eventually(_deleted, description="the tombstone visible before redact")
        result = await a.memory.redact(written["memory_id"], written["version"], reason="pii")
        assert result["redacted"] is True

        async def _scrubbed():
            rows = await a.memory.history("k")
            return rows if all(r["value"] == "[REDACTED]" for r in rows) else None

        await eventually(_scrubbed, description="the version and its tombstone scrubbed")
        restored = await a.memory.restore("k")
        assert restored["restored"] is False and restored["redacted"] is True
        assert await a.memory.get("k") is None
