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
    """Runs the real `write_versions_by_ids`; records the memory versions it
    inserts and the rows it hands the embeddings status mirror."""
    rows = {"m1": _row("m1", "k1"), "m2": _row("m2", "k2"), "m3": _row("m3", "k3", ns="other")}
    spy: dict[str, Any] = {"versions": [], "mirrored": [], "overrides": [], "mirror_versions": []}

    async def fake_get_by_id(
        mid: str,
        *,
        org: str | None = None,
        namespace: str | None = None,
        ch: Any = None,
        scope: Any = None,
        strong: bool = False,
    ):
        row = rows.get(mid)
        return row if row is not None and row.namespace == namespace else None

    async def fake_insert(built: list[dict[str, Any]], *, ch: Any = None) -> None:
        for r in built:
            spy["versions"].append(str(r["memory_id"]))
            spy["overrides"].append(r)

    async def fake_mirror(built: list[dict[str, Any]], *, ch: Any = None, **_: Any) -> int:
        for r in built:
            spy["mirrored"].append(
                tuple(
                    r[c]
                    for c in (
                        "org",
                        "namespace",
                        "agent",
                        "user_id",
                        "session_id",
                        "memory_key",
                        "tombstoned_at",
                    )
                )
            )
            spy["mirror_versions"].append(r["version"])
        return 2 * len(built)

    monkeypatch.setattr(lifecycle, "get_by_id", fake_get_by_id)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", fake_insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", fake_mirror)
    return spy


@pytest.mark.asyncio
async def test_cascade_writes_tombstoned_version_and_mirrors_embeddings(
    cascade_spy: dict[str, Any],
) -> None:
    when = datetime(2026, 9, 12, 8, 0, 0)
    tombstoned = await memory_repo.tombstone_cascade_by_ids(
        "o", ["m1", "m2", "m1", "m3", "missing"], when=when, ch=object(), namespace="ns"
    )
    assert [t["memory_id"] for t in tombstoned] == ["m1", "m2"]
    # Each returned change carries the version its tombstone was written at.
    assert [t["version"] for t in tombstoned] == [ov["version"] for ov in cascade_spy["overrides"]]
    assert cascade_spy["versions"] == ["m1", "m2"]
    for ov in cascade_spy["overrides"]:
        assert ov["status"] == "tombstoned"
        assert ov["tombstoned_at"] == when
        assert ov["expires_at"] is None
    # The mirror carries the full identity tuple of each row, the embeddings table's
    # sort key; m3 lives in another namespace and is left alone.
    assert cascade_spy["mirrored"] == [
        ("o", "ns", "a", "-", "s1", "k1", when),
        ("o", "ns", "a", "-", "s1", "k2", when),
    ]
    # Each vector retire carries its memory tombstone's version.
    assert cascade_spy["mirror_versions"] == [ov["version"] for ov in cascade_spy["overrides"]]


@pytest.mark.asyncio
async def test_a_cascade_draws_one_tombstone_version_and_goes_above_a_newer_head(
    cascade_spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tombstones of a call share one version, drawn once every row is read; a head
    read above it (written on a replica whose clock is ahead) is tombstoned just above it."""
    draws: list[int] = []

    def draw() -> int:
        draws.append(100 + len(draws))
        return draws[-1]

    async def read(mid: str, **_: Any) -> memory_repo.MemoryRow:
        assert draws == []  # drawn once every row is read, so a write before it sorts below
        row = _row(mid, f"k-{mid}")
        if mid == "newer":
            return row.model_copy(update={"version": 500})
        return row.model_copy(update={"memory_type": "decision"}) if mid == "m2" else row

    records: list[Any] = []

    async def retire_records(org: str, ns: str, keys: Any, *, version: int, ch: Any) -> int:
        records.append((sorted(keys), version))
        return len(keys)

    monkeypatch.setattr(lifecycle, "next_version", draw)
    monkeypatch.setattr(lifecycle, "get_by_id", read)
    monkeypatch.setattr(lifecycle, "tombstone_decision_records", retire_records)
    tombstoned = await memory_repo.tombstone_cascade_by_ids(
        "o", ["m1", "newer", "m2"], when=datetime(2026, 9, 12), ch=object(), namespace="ns"
    )
    assert draws == [100]
    assert [(t["memory_id"], t["version"]) for t in tombstoned] == [
        ("m1", 100),
        ("newer", 502),
        ("m2", 100),
    ]
    assert cascade_spy["versions"] == ["m1", "newer", "m2"]
    assert records == [(["k-m2"], 100)]  # the decision's records retire at the rows' version


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
        namespace="ns",
    )
    ov = cascade_spy["overrides"][0]
    assert ov["source_trace"] == "clickgap://x | note"
    assert ov["status"] == "tombstoned" and ov["tombstoned_at"] == when


@pytest.mark.asyncio
async def test_mirror_failure_is_logged_not_raised(
    cascade_spy: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Down:
        async def query_all(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
            raise RuntimeError("ch down")

    # the real mirror: its embeddings read fails and is logged
    monkeypatch.setattr(
        lifecycle, "mirror_status_to_embeddings", memory_repo.mirror_status_to_embeddings
    )
    tombstoned = await memory_repo.tombstone_cascade_by_ids(
        "o", ["m1"], when=datetime(2026, 9, 12), ch=_Down(), namespace="ns"
    )
    # The memory tombstone is durable; the reconcile worker heals the mirror.
    assert len(tombstoned) == 1


@pytest.mark.asyncio
async def test_tombstone_by_id_goes_through_the_cascade(cascade_spy: dict[str, Any]) -> None:
    await memory_repo.tombstone_by_id("o", "m1", namespace="ns", ch=object())
    assert cascade_spy["versions"] == ["m1"]
    assert len(cascade_spy["mirrored"]) == 1


@pytest.mark.asyncio
async def test_conflicts_tombstone_ids_cascades_with_audit_note(
    cascade_spy: dict[str, Any],
) -> None:
    from server.memory.store import conflicts as conflicts_repo

    when = datetime(2026, 9, 12, 9)
    await conflicts_repo.tombstone_ids(
        "o", ["m1", "m2"], "conflicts.resolve:revise by bob", when, namespace="ns", ch=object()
    )
    assert cascade_spy["versions"] == ["m1", "m2"]
    assert len(cascade_spy["mirrored"]) == 2
    assert cascade_spy["overrides"][0]["source_trace"].endswith("| conflicts.resolve:revise by bob")
    assert cascade_spy["overrides"][0]["status"] == "tombstoned"


@pytest.mark.asyncio
async def test_conflicts_tombstone_ids_empty_is_a_noop(cascade_spy: dict[str, Any]) -> None:
    from server.memory.store import conflicts as conflicts_repo

    await conflicts_repo.tombstone_ids(
        "o", [], "note", datetime(2026, 9, 12), namespace="ns", ch=object()
    )
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

    def __init__(
        self, rows: list[dict[str, Any]], drift: list[dict[str, Any]] | None = None
    ) -> None:
        self.rows = rows
        self.drift = drift or []
        self.drift_params: list[dict[str, Any]] = []
        self.entity_reads: list[dict[str, Any]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "LEFT ANTI JOIN" in sql:
            assert params and params["limit"] >= 1
            return self.rows
        if "e.vmin != m.attested" in sql:
            self.drift_params.append(dict(params or {}))
            return self.drift
        assert "FROM loom.entity_vectors FINAL" in sql and params
        self.entity_reads.append(dict(params))
        return [
            {
                "org": params["org"],
                "namespace": params["ns"],
                "agent": "a",
                "user_id": "-",
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
    # one version for the pass, drawn before its scan
    assert all(set(o) == {"tombstoned_at", "version"} and o["tombstoned_at"] for _, o in items)
    assert len({o["version"] for _, o in items}) == 1
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
async def test_reconcile_pass_keeps_entity_vectors_a_newer_write_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass retires with the cutoff every retire uses: an entity_vectors row
    created after the second its version was drawn in is a newer write's."""
    from server.memory.workers import orphan_vector_reconcile as w

    async def fake_reemit(items, *, ch=None, batch=500):
        return len(items)

    monkeypatch.setattr(memory_repo, "reemit_embedding_rows_for_keys", fake_reemit)
    monkeypatch.setattr(memory_repo, "next_version", lambda: 1_785_542_400 * 1_000_000_000)
    ident = {"org": "o", "agent": "a", "user_id": "-", "session_id": "s"}
    ch = _FakeCH([{**ident, "namespace": "n1", "memory_key": "k1"}])
    later = datetime(2026, 8, 1, 0, 0, 1)

    async def read(sql, params=None):
        if "LEFT ANTI JOIN" in sql:
            return ch.rows
        if "e.vmin != m.attested" in sql:
            return []
        base = {"org": "o", "namespace": "n1", "agent": "a", "user_id": "-", "memory_key": "k1"}
        return [
            {
                **base,
                "entity_text": "old",
                "created_at": datetime(2026, 8, 1),
                "tombstoned_at": None,
            },
            {**base, "entity_text": "new", "created_at": later, "tombstoned_at": None},
        ]

    ch.query_all = read  # type: ignore[method-assign]
    res = await w.run_pass(ch=ch)
    assert not res.errors and res.entity_vectors_healed == 1
    ((table, rows),) = ch.inserted
    assert table == "loom.entity_vectors" and [r["entity_text"] for r in rows] == ["old"]


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
async def test_a_lost_verify_mirror_is_healed_at_the_pass_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory.verify mirrors `verified` after its answer; a replica that dies in
    between leaves the ranking signal wrong until this pass re-emits it."""
    from server.memory.workers import orphan_vector_reconcile as w
    from server.settings import settings

    batches: list[list[tuple[tuple[str, ...], dict[str, Any]]]] = []

    async def fake_reemit(items, *, ch=None, batch=500):
        batches.append(list(items))
        return len(items)

    monkeypatch.setattr(memory_repo, "reemit_embedding_rows_for_keys", fake_reemit)
    monkeypatch.setattr(memory_repo, "next_version", lambda: 1_785_542_400 * 1_000_000_000)
    ident = {"org": "o", "agent": "a", "user_id": "-", "session_id": "s", "namespace": "n"}
    ch = _FakeCH(
        [],
        drift=[
            {**ident, "memory_key": "k1", "attested": 1},
            {**ident, "memory_key": "k2", "attested": 0},
        ],
    )
    res = await w.run_pass(ch=ch)
    assert not res.errors and res.verified_healed == 2
    ((items),) = [b for b in batches if b]
    v = 1_785_542_400 * 1_000_000_000
    assert items == [
        (("o", "n", "a", "-", "s", "k1"), {"verified": 1, "version": v}),
        (("o", "n", "a", "-", "s", "k2"), {"verified": 0, "version": v}),
    ]
    # Memory rows younger than the grace may have a mirror in flight.
    grace = settings.orphan_vector_reconcile_grace_seconds
    assert ch.drift_params[0]["cutoff"] == v - grace * 1_000_000_000


def test_verified_drift_scan_compares_the_mirror_to_the_attestation() -> None:
    from server.memory.store.sql import attested_sql
    from server.memory.workers.orphan_vector_reconcile import verified_drift_sql

    sql = verified_drift_sql()
    assert attested_sql() in sql
    assert "loom.memory_embeddings FINAL" in sql and "loom.memory FINAL" in sql
    assert "version < {cutoff:UInt64}" in sql and "{limit:UInt32}" in sql


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


async def test_a_reject_goes_above_a_head_written_since_its_draw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On ClickHouse one version is drawn before the read: a head read newer than it is
    rejected two above it, an older one at the draw."""
    rows = {"m1": _row("m1", "k").model_copy(update={"version": 40})}
    rows["m2"] = _row("m2", "k").model_copy(update={"version": 250, "session_id": "s2"})
    read: list[Any] = []
    written: list[dict[str, Any]] = []

    class _CH:
        async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return [{"memory_id": "m1"}, {"memory_id": "m2"}]

    async def _get_by_id(mid: str, *, namespace: str | None = None, **_: Any) -> Any:
        read.append(namespace)
        return rows[mid]

    async def _insert(built: list[dict[str, Any]], *, ch: Any = None) -> None:
        written.extend(built)

    async def _mirror(built: list[dict[str, Any]], **_: Any) -> int:
        return len(built)

    monkeypatch.setattr("server.core.pg.enabled", lambda: False)
    monkeypatch.setattr(lifecycle, "next_version", lambda: 100)
    monkeypatch.setattr(lifecycle, "get_by_id", _get_by_id)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", _insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", _mirror)
    assert len(await lifecycle.reject_by_identity("o", "ns", "a", "-", "k", "wrong", ch=_CH())) == 2
    assert read == ["ns", "ns"]
    assert [(r["memory_id"], r["status"], r["version"]) for r in written] == [
        ("m1", "rejected", 100),
        ("m2", "rejected", 252),
    ]
