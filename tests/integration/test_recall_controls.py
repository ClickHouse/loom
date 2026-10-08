"""Recall controls: soft metadata boost + hard-demote (ClickGap WS).

`tags` normally hard-filter (rows without the tag are excluded). tags_soft
turns that into a ranking boost — relevant rows keep surfacing, tag-matching
rows rank higher — so callers can sharpen recall without the coverage loss a
hard filter causes. `demote_tags` sinks marked rows to the bottom without
dropping them.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def _reinforce(ch_client, org, ns, memory_key, *, count, last_at):
    """Stand-in for what usage_fold writes: bump reinforcement_count +
    last_reinforced_at on the embedding row (drives the decayed rank boost)."""
    ch_client.command(
        """
        ALTER TABLE loom.memory_embeddings
        UPDATE reinforcement_count = {c:UInt32}, last_reinforced_at = {t:DateTime}
        WHERE org = {org:String} AND namespace = {ns:String} AND memory_key = {k:String}
        SETTINGS mutations_sync = 2
        """,
        parameters={"c": count, "t": last_at, "org": org, "ns": ns, "k": memory_key},
    )


def _wait_for_embedding_row(ch_client, org, ns, memory_key, *, timeout_s=4.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = ch_client.query(
            "SELECT count() FROM loom.memory_embeddings "
            "WHERE org = {org:String} AND namespace = {ns:String} "
            "AND memory_key = {k:String} AND embedding_dim > 0",
            parameters={"org": org, "ns": ns, "k": memory_key},
        ).result_rows
        if rows and rows[0][0] >= 1:
            return
        time.sleep(0.1)
    raise AssertionError(f"embedding row for {memory_key!r} not visible after {timeout_s}s")


@pytest.mark.asyncio
async def test_tags_soft_preserves_coverage(agent, ch_client, require_real_embedder):
    """Hard tag filter drops a query-relevant-but-untagged row; tags_soft keeps
    it (coverage preserved) while still favoring the tagged one."""
    await agent.memory.set(
        "weather_note",
        "Today the weather is sunny in Mumbai",
        memory_type="semantic",
    )
    await agent.memory.set(
        "cluster_fact",
        "Alice owns the staging cluster staging-01",
        memory_type="semantic",
        tags=["infra"],
    )
    _wait_for_embedding_row(ch_client, agent.org, agent.namespace, "weather_note")
    _wait_for_embedding_row(ch_client, agent.org, agent.namespace, "cluster_fact")

    # HARD filter: only infra-tagged rows are eligible → the weather row is
    # dropped even though it's the cosine match for the query.
    hard = await agent.memory.search("what is the weather", top_k=5, tags=["infra"])
    assert "weather_note" not in {h.memory_key for h in hard}, (
        "hard tag filter should exclude the untagged row"
    )

    # SOFT: same tag as a boost, not a gate → the weather row still surfaces.
    soft = await agent.memory.search(
        "what is the weather",
        top_k=5,
        tags=["infra"],
        tags_soft=True,
        tag_boost=0.5,
    )
    assert "weather_note" in {h.memory_key for h in soft}, (
        "tags_soft should preserve coverage of the untagged relevant row"
    )


@pytest.mark.asyncio
async def test_demote_tags_sinks_marked_rows(agent, ch_client, require_real_embedder):
    """A row carrying a demote tag stays in the result set but is pushed to the
    bottom (large-negative rank_score), so known-noise never reaches the top."""
    await agent.memory.set(
        "good_lesson",
        "Use COW-safe IColumn::mutate on merge output columns",
        memory_type="semantic",
    )
    await agent.memory.set(
        "noise_lesson",
        "Use COW-safe IColumn::mutate on read and build paths",
        memory_type="semantic",
        tags=["recurring_false_positive"],
    )
    _wait_for_embedding_row(ch_client, agent.org, agent.namespace, "good_lesson")
    _wait_for_embedding_row(ch_client, agent.org, agent.namespace, "noise_lesson")

    hits = await agent.memory.search(
        "COW safe column mutate",
        top_k=5,
        demote_tags=["recurring_false_positive"],
    )
    by_key = {h.memory_key: h for h in hits}
    assert "noise_lesson" in by_key, "demoted row must NOT be dropped, only sunk"
    assert by_key["noise_lesson"].rank_score < 0, (
        f"demoted row should have a negative rank_score; got {by_key['noise_lesson'].rank_score}"
    )
    if "good_lesson" in by_key:
        assert by_key["good_lesson"].rank_score > by_key["noise_lesson"].rank_score, (
            "non-demoted row should rank above the demoted one"
        )


@pytest.mark.asyncio
async def test_reinforcement_boost_raises_and_decays(agent, ch_client, require_real_embedder):
    """A reinforced memory scores higher with the boost on; a stale
    last_reinforced_at decays that boost back toward zero. Compared on the
    SAME row so only the reinforcement term varies (cosine is identical)."""
    await agent.memory.set(
        "reinf_target",
        "COW-safe IColumn::mutate prevents aliased-chunk corruption on merge output",
        memory_type="semantic",
    )
    _wait_for_embedding_row(ch_client, agent.org, agent.namespace, "reinf_target")
    q = "copy on write safe column mutation on merge output"

    def pick(res):
        for h in res:
            if h.memory_key == "reinf_target":
                return h.rank_score
        return None

    # search_mode="semantic" returns the raw additive rank_score, so the
    # reinforcement term shows as a clean delta (rrf fuses by rank, hiding it).
    async def _score(**kw):
        res = await agent.memory.search(q, top_k=10, auto_touch=False, search_mode="semantic", **kw)
        return pick(res)

    # Heavily reinforced, fresh → boost applied (capped at +0.5).
    _reinforce(
        ch_client, agent.org, agent.namespace, "reinf_target", count=200, last_at=_now_naive()
    )
    r_base = await _score(reinforcement_weight=0.0)
    r_fresh = await _score(reinforcement_weight=0.5, reinforcement_half_life_days=3650)
    assert r_base is not None and r_fresh is not None, (r_base, r_fresh)
    assert r_fresh > r_base + 0.1, f"reinforcement should lift rank_score: {r_base} -> {r_fresh}"

    # Same count, last reinforced ~10y ago → decayed back toward the base.
    _reinforce(
        ch_client,
        agent.org,
        agent.namespace,
        "reinf_target",
        count=200,
        last_at=_now_naive() - timedelta(days=3650),
    )
    r_stale = await _score(reinforcement_weight=0.5, reinforcement_half_life_days=90)
    assert r_stale is not None
    assert r_stale < r_fresh - 0.1, f"decay should shrink the boost: {r_fresh} vs {r_stale}"


@pytest.mark.asyncio
async def test_outcome_by_key_records_evidence(agent, ch_client, require_real_embedder):
    """memory.outcome resolves a memory_key server-side (agent='*'), so a
    caller can reinforce a row it just recalled without tracking memory_id."""
    await agent.memory.set(
        "outcome_target",
        "Prefer URL parameter credentials over the Authorization header",
        memory_type="semantic",
    )
    _wait_for_embedding_row(ch_client, agent.org, agent.namespace, "outcome_target")
    res = await agent.memory.outcome_by_key("outcome_target", outcome="positive", weight=3)
    # recorded=True proves key→id resolution + the evidence write landed.
    # evidence_count_after is read back from an eventually-consistent totals
    # table, so we don't assert on its exact value here.
    assert res.get("recorded") is True, res


@pytest.mark.asyncio
async def test_outcome_run_bulk_applies_to_explicit_keys(agent, ch_client, require_real_embedder):
    """Bulk outcome: one call applies a positive outcome to every key the
    caller recalled, instead of a per-key loop."""
    await agent.memory.set("run_a", "Alpha lesson about parser bounds", memory_type="semantic")
    await agent.memory.set("run_b", "Beta lesson about codec validation", memory_type="semantic")
    _wait_for_embedding_row(ch_client, agent.org, agent.namespace, "run_a")
    _wait_for_embedding_row(ch_client, agent.org, agent.namespace, "run_b")

    res = await agent.memory.outcome_run(
        ["run_a", "run_b"], outcome="positive", weight=2, run_id="pr_x"
    )
    assert res.get("applied") == 2, res
    assert res.get("keys_matched") == 2, res
