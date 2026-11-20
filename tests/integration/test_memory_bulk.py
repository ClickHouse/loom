"""memory.bulk_set + memory.bulk_get — batched ingestion + lookup."""

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

ORG = unique_org("mbulk")


@pytest.mark.asyncio
async def test_bulk_set_writes_all_entries(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"mb-{uuid.uuid4().hex[:6]}"
    entries = [
        {
            "memory_key": f"k{i}",
            "value": {"v": i},
            "memory_type": "semantic",
            "confidence": 0.9,
        }
        for i in range(5)
    ]
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        written = (await a.memory.bulk_set(entries))["written"]
    assert len(written) == 5
    keys = [w["memory_key"] for w in written]
    assert keys == [f"k{i}" for i in range(5)]
    versions = [w["version"] for w in written]
    assert len(set(versions)) == 5, f"versions must be unique; got {versions}"


@pytest.mark.asyncio
async def test_bulk_set_then_bulk_get_round_trip(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"mb-rt-{uuid.uuid4().hex[:6]}"
    entries = [
        {"memory_key": f"x{i}", "value": {"i": i}, "memory_type": "semantic"} for i in range(3)
    ]
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.bulk_set(entries)
        result = await a.memory.bulk_get([f"x{i}" for i in range(3)] + ["nonexistent"])
    assert set(result["results"]) == {"x0", "x1", "x2"}
    assert result["missing"] == ["nonexistent"]
    for i in range(3):
        entry = result["results"][f"x{i}"]
        assert entry["found"] is True
        assert entry["value"] == {"i": i}
        assert entry["memory_type"] == "semantic"


@pytest.mark.asyncio
async def test_bulk_get_excludes_tombstoned(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"mb-tomb-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.bulk_set(
            [
                {"memory_key": "alive", "value": 1, "memory_type": "semantic"},
                {"memory_key": "dying", "value": 2, "memory_type": "semantic"},
            ]
        )
        await a.memory.delete("dying")
        result = await a.memory.bulk_get(["alive", "dying"])
    assert "alive" in result["results"]
    assert "dying" not in result["results"]
    assert "dying" in result["missing"]


@pytest.mark.asyncio
async def test_bulk_set_searchable_embeds(control_plane_url: str) -> None:
    """Searchable memories written via bulk_set are findable by search."""
    from loom import Agent

    ns = f"mb-srch-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.bulk_set(
            [
                {
                    "memory_key": "ch_fact",
                    "value": {"text": "clickhouse columnar fast"},
                    "memory_type": "semantic",
                },
                {
                    "memory_key": "py_fact",
                    "value": {"text": "python interpreter"},
                    "memory_type": "semantic",
                },
            ]
        )

        async def search_for_ch_fact():
            hits = await a.memory.search("clickhouse columnar", top_k=5)
            return hits if any(h.memory_key == "ch_fact" for h in hits) else None

        hits = await eventually(search_for_ch_fact, description="bulk-set embeddings to be flushed")
    keys = {h.memory_key for h in hits}
    assert "ch_fact" in keys


@pytest.mark.asyncio
async def test_bulk_set_mixed_searchable_and_episodic(control_plane_url: str) -> None:
    """Bulk insert mixing searchable + episodic: episodic isn't embedded."""
    from loom import Agent

    ns = f"mb-mix-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        res = await a.memory.bulk_set(
            [
                {"memory_key": "sem", "value": {"x": 1}, "memory_type": "semantic"},
                {"memory_key": "ep", "value": {"x": 2}, "memory_type": "episodic"},
                {"memory_key": "pref", "value": {"x": 3}, "memory_type": "preference"},
            ]
        )
    assert len(res["written"]) == 3
    # Both retrievable via bulk_get
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        result = await a.memory.bulk_get(["sem", "ep", "pref"])
    assert set(result["results"]) == {"sem", "ep", "pref"}


@pytest.mark.asyncio
async def test_bulk_set_with_ttl(control_plane_url: str) -> None:
    """ttl_seconds per-entry → expires_at populated."""
    from loom import Agent

    ns = f"mb-ttl-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.bulk_set(
            [
                {
                    "memory_key": "k",
                    "value": {"x": 1},
                    "memory_type": "semantic",
                    "ttl_seconds": 3600,
                },
            ]
        )
        result = await a.memory.bulk_get(["k"])
    entry = result["results"]["k"]
    assert entry["expires_at"] is not None


@pytest.mark.asyncio
async def test_bulk_set_per_entry_ttl_on_embedding_mirror(
    control_plane_url: str, ch_client
) -> None:
    """Each embedding-mirror row carries ITS OWN entry's expires_at, not the
    last entry's: in a mixed-TTL batch where the TTL entry is followed by a
    non-TTL entry, the TTL entry's embedding row keeps its expiry."""
    from loom import Agent

    ns = f"mb-ettl-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.bulk_set(
            [
                {
                    "memory_key": "ttl_key",
                    "value": {"text": "expiring fact"},
                    "memory_type": "semantic",
                    "ttl_seconds": 3600,
                },
                {
                    "memory_key": "perm_key",
                    "value": {"text": "permanent fact"},
                    "memory_type": "semantic",
                },
            ]
        )

    async def _read_embedding_expiries():
        rows = ch_client.query(
            """
            SELECT memory_key, argMax(expires_at, created_at) AS exp
            FROM loom.memory_embeddings
            WHERE org = {org:String} AND namespace = {ns:String}
              AND agent = 'w' AND memory_key IN ('ttl_key', 'perm_key')
            GROUP BY memory_key
            """,
            parameters={"org": ORG, "ns": ns},
        ).result_rows
        by_key = {r[0]: r[1] for r in rows}
        # Wait until both embedding rows have flushed.
        return by_key if len(by_key) == 2 else None

    by_key = await eventually(
        _read_embedding_expiries, description="both bulk_set embedding rows to flush"
    )
    assert by_key["ttl_key"] is not None, "TTL entry's embedding row lost its expires_at"
    assert by_key["perm_key"] is None, "non-TTL entry's embedding row got a stray expires_at"


@pytest.mark.asyncio
async def test_bulk_get_answers_each_row_as_memory_get_does(control_plane_url: str) -> None:
    """memory.bulk_get answers each row as memory.get does, sensitivity, valid_to,
    superseded_by and duplicate_of included."""
    from loom import Agent

    ns = f"mb-shape-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.call(
            "memory.set",
            memory_key="vault",
            value={"note": "rotated weekly"},
            memory_type="semantic",
            sensitivity="high",
        )
        await a.call(
            "memory.set",
            memory_key="window",
            value={"note": "Tue and Thu"},
            memory_type="semantic",
            valid_to="2099-01-01T00:00:00",
        )
        bulk = await a.memory.bulk_get(["vault", "window"])
        gets = {k: await a.call("memory.get", memory_key=k) for k in ("vault", "window")}
    assert bulk["results"]["vault"]["sensitivity"] == "high"
    assert bulk["results"]["window"]["valid_to"] is not None
    skip = {"conflict_verdict", "code", "recall_by_agent", "unchanged"}
    for k, got in gets.items():
        served = {f: v for f, v in bulk["results"][k].items() if f not in skip}
        assert served == {f: v for f, v in got.items() if f not in skip}, k


@pytest.mark.asyncio
@pytest.mark.parametrize("entries", [2, 3])
async def test_an_append_only_bulk_write_keeps_every_entry(
    control_plane_url: str, mint_test_token, entries: int
) -> None:
    """In an `append_only` namespace the conflict check gives each entry of a bulk write
    its own row, even when its text nearly restates a live row and the other entries:
    every key is written and read back with its own value, none merged away or marked
    duplicate or conflicted. (Identical texts are the bulk write's identity dedup, a
    check of its own.)"""
    from loom import Agent

    ns = f"mb-ao-{uuid.uuid4().hex[:6]}"
    token = mint_test_token(org=ORG, namespaces=[ns], roles=["writer", "admin"])

    def text(i: int) -> str:
        return f"the nightly ingest run {i} finished with no errors"

    async with Agent(
        org=ORG, namespace=ns, agent="w", base_url=control_plane_url, api_key=token
    ) as a:
        await a.namespaces.register(
            owner_email="t@example.com", default_conflict_policy="append_only"
        )
        await a.memory.set(memory_key="run/0", value=text(0), memory_type="semantic")
        out = await a.memory.bulk_set(
            [
                {"memory_key": f"run/{i}", "value": text(i), "memory_type": "semantic"}
                for i in range(1, entries + 1)
            ]
        )
        assert not out["merged"] and not out["duplicates"] and not out["conflicted"]
        assert all(int(w["version"]) > 0 for w in out["written"])
        got = await a.memory.bulk_get([f"run/{i}" for i in range(entries + 1)])
    assert sorted(got["results"]) == [f"run/{i}" for i in range(entries + 1)]
    written = {str(w["memory_key"]): w for w in out["written"]}
    assert sorted(written) == [f"run/{i}" for i in range(1, entries + 1)]
    for key, row in got["results"].items():
        # the live row of each key is the one its own write made, with its own value
        assert row["found"] is True and row["value"] == text(int(key.split("/")[1]))
        assert row["memory_type"] == "semantic" and row["status"] == "active"
        if key in written:
            assert row["memory_id"] == written[key]["memory_id"]
            assert int(row["version"]) == int(written[key]["version"])
