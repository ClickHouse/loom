"""A tombstone reaches every tier or it is not a tombstone.

Three surfaces are pinned:
  * `memory_repo.tombstone_cascade_by_ids` writes a status='tombstoned'
    version per row AND mirrors it onto memory_embeddings for the row's
    identity; `tombstone_by_id` (dedup) and `conflicts_repo.tombstone_ids`
    (conflict resolution) both go through it.
  * the orphan_vector_reconcile worker's scan mirrors the probe (ANTI JOIN
    against live memory rows, grace window, bound) and heals what it finds.
  * the erasure canary's pure helpers build a predicate per column type and
    classify residue against the allow-list.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.store import lifecycle


def _row(mid: str, key: str, ns: str = "ns") -> memory_repo.MemoryRow:
    return memory_repo.MemoryRow(
        memory_id=mid,
        org="o",
        namespace=ns,
        agent="a",
        user_id="-",
        session_id="s1",
        memory_key=key,
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 1),
        updated_at=datetime(2026, 9, 1),
        expires_at=None,
        source_trace="clickgap://x",
    )


@pytest.fixture
def cascade_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rows = {"m1": _row("m1", "k1"), "m2": _row("m2", "k2", ns="other")}
    spy: dict[str, Any] = {"versions": [], "mirrored": [], "overrides": []}

    async def fake_get_by_id(
        mid: str, *, org: str | None = None, ch: Any = None, scope: Any = None
    ):
        return rows.get(mid)

    async def fake_write_versions(org: str, ids: list[str], update: Any, *, ch: Any = None) -> int:
        for mid in ids:
            spy["versions"].append(mid)
            spy["overrides"].append(update(rows[mid]))
        return len(ids)

    async def fake_mirror(
        org: str,
        ns: str,
        agent: str,
        user_id: str,
        session_id: str,
        key: str,
        *,
        ts: datetime,
        ch: Any = None,
    ) -> int:
        spy["mirrored"].append((org, ns, agent, user_id, session_id, key, ts))
        return 2

    monkeypatch.setattr(lifecycle, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(lifecycle, "write_versions_by_ids", fake_write_versions)
    monkeypatch.setattr(lifecycle, "tombstone_embeddings_for_key", fake_mirror)
    return spy


@pytest.mark.asyncio
async def test_cascade_writes_tombstoned_version_and_mirrors_embeddings(
    cascade_spy: dict[str, Any],
) -> None:
    when = datetime(2026, 9, 12, 8, 0, 0)
    n = await memory_repo.tombstone_cascade_by_ids(
        "o", ["m1", "m2", "m1", "missing"], when=when, ch=object()
    )
    assert n == 2
    assert cascade_spy["versions"] == ["m1", "m2"]
    for ov in cascade_spy["overrides"]:
        assert ov["status"] == "tombstoned"
        assert ov["tombstoned_at"] == when
        assert ov["expires_at"] is None
    # The mirror carries the full identity tuple of each row — it is the
    # embeddings table's sort key, and the second row lives in another namespace.
    assert cascade_spy["mirrored"] == [
        ("o", "ns", "a", "-", "s1", "k1", when),
        ("o", "other", "a", "-", "s1", "k2", when),
    ]


@pytest.mark.asyncio
async def test_cascade_update_hook_adds_overrides_without_dropping_the_tombstone(
    cascade_spy: dict[str, Any],
) -> None:
    when = datetime(2026, 9, 12)
    await memory_repo.tombstone_cascade_by_ids(
        "o",
        ["m1"],
        when=when,
        update=lambda r: {"source_trace": f"{r.source_trace} | note"},
        ch=object(),
    )
    ov = cascade_spy["overrides"][0]
    assert ov["source_trace"] == "clickgap://x | note"
    assert ov["status"] == "tombstoned" and ov["tombstoned_at"] == when


@pytest.mark.asyncio
async def test_mirror_failure_is_logged_not_raised(
    cascade_spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*a: Any, **kw: Any) -> int:
        raise RuntimeError("ch down")

    monkeypatch.setattr(lifecycle, "tombstone_embeddings_for_key", boom)
    n = await memory_repo.tombstone_cascade_by_ids(
        "o", ["m1"], when=datetime(2026, 9, 12), ch=object()
    )
    assert n == 1  # the memory tombstone is durable; the reconcile worker heals the mirror


@pytest.mark.asyncio
async def test_tombstone_by_id_goes_through_the_cascade(cascade_spy: dict[str, Any]) -> None:
    await memory_repo.tombstone_by_id("o", "m1", ch=object())
    assert cascade_spy["versions"] == ["m1"]
    assert len(cascade_spy["mirrored"]) == 1


@pytest.mark.asyncio
async def test_conflicts_tombstone_ids_cascades_with_audit_note(
    cascade_spy: dict[str, Any],
) -> None:
    from server.memory.store import conflicts as conflicts_repo

    when = datetime(2026, 9, 12, 9)
    await conflicts_repo.tombstone_ids(
        "o", ["m1", "m2"], "conflicts.resolve:revise by bob", when, ch=object()
    )
    assert cascade_spy["versions"] == ["m1", "m2"]
    assert len(cascade_spy["mirrored"]) == 2
    assert cascade_spy["overrides"][0]["source_trace"].endswith("| conflicts.resolve:revise by bob")
    assert cascade_spy["overrides"][0]["status"] == "tombstoned"


@pytest.mark.asyncio
async def test_conflicts_tombstone_ids_empty_is_a_noop(cascade_spy: dict[str, Any]) -> None:
    from server.memory.store import conflicts as conflicts_repo

    await conflicts_repo.tombstone_ids("o", [], "note", datetime(2026, 9, 12), ch=object())
    assert cascade_spy["versions"] == []


# ---------------------------------------------------------------------------
# reconcile worker
# ---------------------------------------------------------------------------


def test_orphan_scan_mirrors_the_probe_shape() -> None:
    from server.memory.workers.orphan_vector_reconcile import orphan_identities_sql

    sql = orphan_identities_sql()
    assert "LEFT ANTI JOIN" in sql
    assert "FROM loom.memory_embeddings FINAL" in sql and "FROM loom.memory FINAL" in sql
    assert "tombstoned_at IS NULL" in sql  # both sides judge liveness the same way
    assert "{grace:UInt32}" in sql and "{limit:UInt32}" in sql
    for col in ("agent", "user_id", "session_id", "memory_key"):
        assert f"e.{col} = m.{col}" in sql  # full identity tuple, not just the key


def test_entity_vectors_are_re_emitted_not_mutated() -> None:
    import inspect

    from server.memory.store.sweeps import entity_vectors_live_sql
    from server.memory.workers import orphan_vector_reconcile as w

    sql = entity_vectors_live_sql()
    assert "FROM loom.entity_vectors FINAL" in sql
    assert "memory_key IN {keys:Array(String)}" in sql
    assert "tombstoned_at IS NULL" in sql
    # The whole worker is mutation-free: tombstones are new RMT versions.
    assert "ALTER TABLE" not in inspect.getsource(w)


class _FakeCH:
    """Answers the orphan scan with `rows`, and the entity_vectors read with one
    live row per key; records what gets inserted."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.entity_reads: list[dict[str, Any]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "LEFT ANTI JOIN" in sql:
            assert params and params["limit"] >= 1
            return self.rows
        assert "FROM loom.entity_vectors FINAL" in sql and params
        self.entity_reads.append(dict(params))
        return [
            {
                "org": params["org"],
                "namespace": params["ns"],
                "entity_text": f"ent-{k}",
                "memory_key": k,
                "created_at": datetime(2026, 8, 1),
                "tombstoned_at": None,
            }
            for k in params["keys"]
        ]

    async def insert(self, table: str, rows: list[dict[str, Any]], *, settings: Any = None) -> None:
        self.inserted.append((table, rows))


@pytest.mark.asyncio
async def test_reconcile_pass_heals_every_identity_in_one_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.workers import orphan_vector_reconcile as w

    batches: list[list[tuple[tuple[str, ...], dict[str, Any]]]] = []

    async def fake_reemit(items, *, ch=None, batch=500):
        batches.append(list(items))
        return len(items) + 1  # one identity carries two model rows

    monkeypatch.setattr(memory_repo, "reemit_embedding_rows_for_keys", fake_reemit)
    ident = {"org": "o", "agent": "a", "user_id": "-", "session_id": "s"}
    ch = _FakeCH(
        [
            {**ident, "namespace": "n1", "memory_key": "k1"},
            {**ident, "namespace": "n1", "memory_key": "k2"},
            {**ident, "namespace": "n2", "memory_key": "k3"},
        ]
    )
    res = await w.run_pass(ch=ch)
    assert res.orphans_seen == 3 and not res.errors
    # one keyed re-emit for every identity, each only gaining the tombstone
    ((items),) = batches
    assert [k for k, _ in items] == [
        ("o", "n1", "a", "-", "s", "k1"),
        ("o", "n1", "a", "-", "s", "k2"),
        ("o", "n2", "a", "-", "s", "k3"),
    ]
    assert all(set(o) == {"tombstoned_at"} and o["tombstoned_at"] for _, o in items)
    assert res.identities_healed == 3 and res.embeddings_healed == 4
    # one entity_vectors read per namespace, keys bound as an array
    assert {p["ns"]: p["keys"] for p in ch.entity_reads} == {"n1": ["k1", "k2"], "n2": ["k3"]}
    assert res.entity_vectors_healed == 3
    # re-emitted rows keep created_at (the RMT version AND the partition) and
    # only gain the tombstone, so FINAL collapses onto the last-inserted row
    for table, rows in ch.inserted:
        assert table == "loom.entity_vectors"
        for row in rows:
            assert row["created_at"] == datetime(2026, 8, 1) and row["tombstoned_at"] is not None


@pytest.mark.asyncio
async def test_a_failed_embeddings_reemit_leaves_the_entity_vectors_for_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.workers import orphan_vector_reconcile as w

    async def failing(items, *, ch=None, batch=500):
        raise RuntimeError("insert timed out")

    monkeypatch.setattr(memory_repo, "reemit_embedding_rows_for_keys", failing)
    ident = {"org": "o", "agent": "a", "user_id": "-", "session_id": "s"}
    ch = _FakeCH([{**ident, "namespace": "n1", "memory_key": "k1"}])
    res = await w.run_pass(ch=ch)
    assert res.identities_healed == 0 and ch.entity_reads == [] and ch.inserted == []
    assert len(res.errors) == 1 and "insert timed out" in res.errors[0]


@pytest.mark.asyncio
async def test_reconcile_pass_with_nothing_to_do_is_quiet() -> None:
    from server.memory.workers import orphan_vector_reconcile as w

    ch = _FakeCH([])
    res = await w.run_pass(ch=ch)
    assert res.orphans_seen == 0 and res.identities_healed == 0 and ch.inserted == []


def test_worker_is_registered_and_default_on() -> None:
    from server.settings import Settings

    s = Settings()
    assert s.orphan_vector_reconcile_enabled is True
    assert s.orphan_vector_reconcile_grace_seconds >= 60  # never race an in-flight write


# ---------------------------------------------------------------------------
# erasure canary helpers
# ---------------------------------------------------------------------------


def test_scan_predicate_handles_each_string_shape() -> None:
    from bench.erasure_compliance import scan_predicate

    assert (
        scan_predicate("value", "String")
        == "positionCaseInsensitive(toString(`value`), {tok:String}) > 0"
    )
    assert "arrayExists" in scan_predicate("tags", "Array(String)")
    assert "mapValues" in scan_predicate("args", "Map(LowCardinality(String), String)")
    assert "toString(`content_sha256`)" in scan_predicate("content_sha256", "FixedString(64)")


def test_scan_plan_skips_inner_mv_tables() -> None:
    from bench.erasure_compliance import scan_plan

    plan = scan_plan(
        [
            ("memory", "value", "String"),
            (".inner_id.abc", "x", "String"),
            ("_migrations", "name", "String"),
        ]
    )
    assert [(t, c) for t, c, _p in plan] == [("memory", "value")]


def test_classify_hits_separates_allowed_residue_from_leaks() -> None:
    from bench.erasure_compliance import classify_hits

    hits = classify_hits(
        [
            ("memory_embeddings", "content_excerpt", 3),
            ("audit_log", "subject", 1),
            ("memory", "value", 0),
        ]
    )
    # search_trace_log.query is a census table — a hit there is a leak
    assert classify_hits([("search_trace_log", "query", 1)])[0].allowed is False
    assert [(h.table, h.allowed) for h in hits] == [
        ("memory_embeddings", False),
        ("audit_log", True),
    ]
    assert hits[1].reason  # every allowed residue names why
