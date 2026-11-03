"""A status change on loom.memory is mirrored onto memory_embeddings,
the row memory.search reads; a reconcile pass repairs rows that diverged before."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.store import lifecycle


class _CH:
    """Records inserts; answers the embeddings read with one live row per key."""

    def __init__(self, emb_status: str = "active") -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.emb_status = emb_status

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        p = params or {}
        if "FROM loom.memory_embeddings FINAL" in sql:
            # the keyed read: one identity per position of the six arrays
            return [
                {
                    "org": o,
                    "namespace": n,
                    "agent": a,
                    "user_id": u,
                    "session_id": s_,
                    "memory_key": k,
                    "memory_type": "semantic",
                    "scope": "private",
                    "embedding": [0.1, 0.2],
                    "embedding_dim": 2,
                    "embedding_model": "fake",
                    "status": self.emb_status,
                    "version": 1,
                    "content_excerpt": "x",
                }
                for o, n, a, u, s_, k in zip(
                    p["o"], p["n"], p["a"], p["u"], p["s"], p["k"], strict=True
                )
            ]
        return []

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.inserts.append((table, rows))


def _existing(status: str = "active") -> Any:
    return SimpleNamespace(
        memory_id="m1",
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="home",
        status=status,
        conflict_with=[],
        invalid_at=None,
    )


def _wire(monkeypatch: pytest.MonkeyPatch, ch: _CH, existing: Any) -> None:
    async def _get_by_id(
        memory_id: str,
        *,
        org: str | None = None,
        ch: Any = None,
        scope: Any = None,
        strong: bool = False,
    ) -> Any:
        return existing

    def _build(row: Any, **overrides: Any) -> dict[str, Any]:
        base = {
            "org": row.org,
            "namespace": row.namespace,
            "agent": row.agent,
            "user_id": row.user_id,
            "session_id": row.session_id,
            "memory_key": row.memory_key,
            "status": row.status,
            "invalid_at": row.invalid_at,
        }
        base.update(overrides)
        return base

    monkeypatch.setattr(lifecycle, "get_by_id", _get_by_id)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", _build)
    # The stub builder returns partial rows; the funnel's full-row check is
    # pinned in test_rmt_write_is_typed.
    monkeypatch.setattr("server.memory.store.inserts.check_memory_rows", lambda rows: None)


@pytest.mark.asyncio
async def test_status_flip_is_mirrored_onto_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(emb_status="active")
    _wire(monkeypatch, ch, _existing())
    n = await memory_repo.write_versions_by_ids(
        "o",
        ["m1"],
        lambda r: {"status": "superseded", "superseded_by": "m2"},
        ch=ch,
    )
    assert n == 1
    tables = [t for t, _ in ch.inserts]
    assert tables == ["loom.memory", "loom.memory_embeddings"]
    emb_rows = ch.inserts[1][1]
    assert emb_rows[0]["status"] == "superseded" and emb_rows[0]["memory_key"] == "home"
    assert emb_rows[0]["version"] > 1  # a new RMT version, not an in-place edit


@pytest.mark.asyncio
async def test_conflict_mark_reaches_the_embeddings_row(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(emb_status="active")
    _wire(monkeypatch, ch, _existing())
    await memory_repo.write_versions_by_ids(
        "o",
        ["m1"],
        lambda r: {"status": "conflicted", "conflict_with": ["m9"]},
        ch=ch,
    )
    assert ch.inserts[1][0] == "loom.memory_embeddings"
    assert ch.inserts[1][1][0]["status"] == "conflicted"


@pytest.mark.asyncio
async def test_no_status_change_means_no_embeddings_write(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH()
    _wire(monkeypatch, ch, _existing())
    await memory_repo.write_versions_by_ids("o", ["m1"], lambda r: {"confidence": 0.5}, ch=ch)
    assert [t for t, _ in ch.inserts] == ["loom.memory"]


@pytest.mark.asyncio
async def test_already_mirrored_row_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH(emb_status="superseded")
    _wire(monkeypatch, ch, _existing())
    await memory_repo.write_versions_by_ids("o", ["m1"], lambda r: {"status": "superseded"}, ch=ch)
    assert [t for t, _ in ch.inserts] == ["loom.memory"]


@pytest.mark.asyncio
async def test_reconcile_counts_on_dry_run_and_mirrors_otherwise() -> None:
    class _RCH(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            if "WHERE m.ms != e.es" in sql:
                return [
                    {
                        "org": "o",
                        "namespace": "n",
                        "memory_key": "k1",
                        "agent": "a",
                        "user_id": "u",
                        "session_id": "s",
                        "status": "superseded",
                        "invalid_at": None,
                        "embeddings_status": "active",
                    },
                    {
                        "org": "o",
                        "namespace": "n",
                        "memory_key": "k2",
                        "agent": "a",
                        "user_id": "u",
                        "session_id": "s",
                        "status": "conflicted",
                        "invalid_at": None,
                        "embeddings_status": "active",
                    },
                ]
            return await super().query_all(sql, params)

    ch = _RCH()
    dry = await memory_repo.reconcile_embedding_status("o", "n", dry_run=True, ch=ch)
    assert dry["divergent_rows"] == 2 and dry["superseded"] == 1 and dry["conflicted"] == 1
    # dry run says what it WOULD write (one live mirror row per key) and writes nothing
    assert dry["mirrored"] == 2 and ch.inserts == []
    wet = await memory_repo.reconcile_embedding_status("o", "n", dry_run=False, ch=ch)
    assert wet["mirrored"] == 2
    assert {r["status"] for _, rows in ch.inserts for r in rows} == {"superseded", "conflicted"}
    by_status = {r["status"]: r["version"] for _, rows in ch.inserts for r in rows}
    # the copy that hides the row is a retire, stamped with the version drawn
    # before the scan; the one that serves it again is read + 1, so a
    # memory.delete racing the pass keeps its later retire
    assert by_status["superseded"] > memory_repo.reemit_version({"version": 1})
    assert by_status["conflicted"] == memory_repo.reemit_version({"version": 1})


@pytest.mark.asyncio
async def test_reconcile_reads_the_mirror_by_the_full_identity() -> None:
    """One memory_key lives under many identities,
    each with its own status. The divergence is computed per identity row and
    the mirror is read by that row's full tuple — never by key, which would
    paint every live row of one key superseded."""
    seen: list[dict[str, Any]] = []

    class _KCH(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            if "WHERE m.ms != e.es" in sql:
                assert "GROUP BY org, namespace, agent, user_id, session_id, memory_key" in sql
                assert "USING (org, namespace, agent, user_id, session_id, memory_key)" in sql
                return [
                    {
                        "org": "o",
                        "namespace": "n",
                        "memory_key": "user_task_id",
                        "agent": "writer-a",
                        "user_id": "-",
                        "session_id": "run-17",
                        "status": "superseded",
                        "invalid_at": None,
                        "embeddings_status": "active",
                    }
                ]
            if "FROM loom.memory_embeddings FINAL" in sql:
                seen.append(params or {})
                # The full identity tuple rides in PREWHERE
                assert (
                    "PREWHERE (org, namespace, agent, user_id, session_id, memory_key) IN arrayZip("
                    in sql
                )
            return await super().query_all(sql, params)

    ch = _KCH()
    out = await memory_repo.reconcile_embedding_status("o", "n", dry_run=True, ch=ch)
    assert out["divergent_rows"] == 1 and out["mirrored"] == 1
    assert seen and seen[0]["a"] == ["writer-a"] and seen[0]["s"] == ["run-17"]
    assert seen[0]["k"] == ["user_task_id"]


@pytest.mark.asyncio
async def test_reconcile_reads_the_mirror_in_one_statement_per_batch() -> None:
    """`loom reconcile-status` hands up to 5,000 divergent identities to the
    mirror: one keyed read per 500 of them, not one read per identity."""
    rows = [
        {
            "org": "o",
            "namespace": "n",
            "memory_key": f"k{i}",
            "agent": "a",
            "user_id": "u",
            "session_id": "s",
            "status": "superseded",
            "invalid_at": None,
            "embeddings_status": "active",
        }
        for i in range(1200)
    ]
    reads: list[int] = []

    class _BCH(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            if "WHERE m.ms != e.es" in sql:
                return rows
            if "FROM loom.memory_embeddings FINAL" in sql:
                reads.append(len((params or {})["k"]))
            return await super().query_all(sql, params)

    ch = _BCH()
    out = await memory_repo.reconcile_embedding_status("o", "n", dry_run=False, ch=ch)
    assert reads == [500, 500, 200]
    assert out["mirrored"] == 1200 and len(ch.inserts) == 1


@pytest.mark.asyncio
async def test_divergent_namespaces_lists_the_org_largest_first_within_a_window() -> None:
    """The org-wide sweep asks one grouped question per
    org — which namespaces carry divergent identity rows, how many — bounded
    by created_at when asked, and joins on the FULL identity tuple."""
    from server.memory.store import divergent_namespaces

    seen: list[tuple[str, dict[str, Any]]] = []

    class _NCH(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            seen.append((sql, params or {}))
            return [
                {"namespace": "big", "divergent_rows": 590},
                {"namespace": "small", "divergent_rows": 5},
            ]

    out = await divergent_namespaces("clickhouse", since_days=2, limit=100, ch=_NCH())
    assert out == [
        {"namespace": "big", "divergent_rows": 590},
        {"namespace": "small", "divergent_rows": 5},
    ]
    sql, params = seen[0]
    assert params == {"org": "clickhouse", "lim": 100, "days": 2}
    assert sql.count("INTERVAL {days:UInt32} DAY") == 2  # both scans bounded
    assert "USING (org, namespace, agent, user_id, session_id, memory_key)" in sql
    assert "GROUP BY namespace" in sql and "ORDER BY divergent_rows DESC" in sql

    seen.clear()
    await divergent_namespaces("dev", ch=_NCH())
    sql, params = seen[0]
    assert "days" not in params and "INTERVAL" not in sql


@pytest.mark.asyncio
async def test_divergence_judges_liveness_on_the_newest_version() -> None:
    """A deleted key must drop out of both divergence scans. Filtering
    tombstones in WHERE, before argMax, hands a deleted key its pre-delete
    status: it reads as divergent forever (the re-emit writes nothing for it)
    and crowds live rows out of the LIMIT. Liveness goes in HAVING, on both
    the memory and the embeddings side, per scan."""
    from server.memory.store import divergent_namespaces
    from server.memory.store.embeddings import status_divergence_sql

    seen: list[str] = []

    class _NCH(_CH):
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            seen.append(sql)
            return []

    await divergent_namespaces("o", since_days=2, ch=_NCH())
    for sql in (status_divergence_sql(), seen[0]):
        assert "tombstoned_at IS NULL AND" not in sql
        assert "AND tombstoned_at IS NULL" not in sql
        assert sql.count("HAVING argMax(tombstoned_at IS NULL, version) = 1") == 2


async def test_rows_are_read_at_once_but_written_once_each_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_flight = peak = 0

    async def get_by_id(mid: str, **kw: Any) -> Any:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        # The later ids finish first: the order kept is the order given, not the finish.
        await asyncio.sleep(delay.get(mid, 0))
        in_flight -= 1
        return None if mid == "gone" else SimpleNamespace(memory_id=mid)

    updated: list[str] = []

    def update(row: Any) -> dict[str, Any]:
        updated.append(row.memory_id)
        return {}

    async def insert(rows: list[dict[str, Any]], **kw: Any) -> None:
        return None

    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "build_memory_version_row", lambda row, **kw: {"status": ""})
    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    ids = [f"m{i}" for i in range(40)]
    delay = {mid: (len(ids) - i) / 2000 for i, mid in enumerate(ids)}
    n = await lifecycle.write_versions_by_ids("o", [*ids, "m3", "", "gone"], update)
    assert updated == ids and n == len(ids)
    assert 1 < peak <= lifecycle._READ_AT_ONCE


@pytest.mark.parametrize("failure", [TimeoutError("replica down"), asyncio.CancelledError()])
async def test_a_failed_read_stops_the_others_and_raises_as_itself(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    # A read that fails, or is cancelled on its own, while the caller is not.
    cancelled: list[str] = []

    async def get_by_id(mid: str, **kw: Any) -> Any:
        if mid == "bad":
            raise failure
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(mid)
            raise
        return SimpleNamespace(memory_id=mid)

    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    with pytest.raises(type(failure)):
        await lifecycle.write_versions_by_ids("o", ["a", "bad", "b"], lambda row: {})
    # Stopped by the time the caller sees the failure.
    assert sorted(cancelled) == ["a", "b"]


async def test_a_cancelled_caller_lets_its_reads_clean_up(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    cleaned: list[str] = []

    async def get_by_id(mid: str, **kw: Any) -> Any:
        started.append(mid)
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.01)  # cleanup that awaits, as the client's does
            cleaned.append(mid)
            raise

    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    # The queued reads stop first, so gather raises while the started ones clean up.
    ids = [f"m{i}" for i in range(lifecycle._READ_AT_ONCE + 4)]
    call = asyncio.ensure_future(lifecycle.write_versions_by_ids("o", ids, lambda row: {}))
    await asyncio.sleep(0.05)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert len(started) == lifecycle._READ_AT_ONCE and sorted(cleaned) == sorted(started)


@pytest.mark.asyncio
async def test_reject_writes_version_rows_and_updates_no_embeddings_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memory.reject re-emits the memory row and its vector row as new versions. A
    lightweight UPDATE of loom.memory_embeddings would leave a patch part, which turns
    the fact leg's codes-first read off for the whole table."""
    from server.memory.api.models_lifecycle import MemoryRejectInput
    from server.memory.app.ops import handle_memory_reject

    ch = _RejectCH()
    _wire_sessions(monkeypatch, ch)
    out = await handle_memory_reject(
        MemoryRejectInput(
            org="o", namespace="n", agent="a", user_id="u", memory_key="home", rejection_reason="r"
        )
    )
    assert out.rejected is True
    assert ch.executed == []
    assert [t for t, _ in ch.inserts] == ["loom.memory", "loom.memory_embeddings"]
    # Every session's row of the key at one version drawn before the read, and each
    # vector row at it: above the read + 1 copy a metadata write left (version 2).
    mems = ch.inserts[0][1]
    assert [(m["session_id"], m["status"]) for m in mems] == [
        ("s1", "rejected"),
        ("s2", "rejected"),
    ]
    assert {m["rejection_reason"] for m in mems} == {"r"}
    (drawn,) = {m["version"] for m in mems}
    assert drawn > 2
    embs = ch.inserts[1][1]
    assert sorted((e["session_id"], e["status"], e["version"]) for e in embs) == [
        ("s1", "rejected", drawn),
        ("s2", "rejected", drawn),
    ]
    # A vector row written just before, on another replica, is found.
    assert ch.vector_reads
    assert all("select_sequential_consistency = 1" in sql for sql in ch.vector_reads)


@pytest.mark.asyncio
async def test_reject_raises_when_the_vector_rows_are_not_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recall reads the vector row: a reject whose vector write failed is not
    answered `rejected=True`."""
    from server.memory.api.models_lifecycle import MemoryRejectInput
    from server.memory.app.ops import handle_memory_reject

    class _FailingCH(_RejectCH):
        async def insert(
            self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
        ) -> None:
            if table == "loom.memory_embeddings":
                raise TimeoutError("insert timed out")
            await super().insert(table, rows, settings)

    _wire_sessions(monkeypatch, _FailingCH())
    with pytest.raises(TimeoutError):
        await handle_memory_reject(
            MemoryRejectInput(org="o", namespace="n", agent="a", user_id="u", memory_key="home")
        )


class _RejectCH(_CH):
    """Answers the reject's id read with one row per session of the key, and each
    vector row one version above its memory row."""

    def __init__(self) -> None:
        super().__init__()
        self.executed: list[str] = []
        self.vector_reads: list[str] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "FROM loom.memory FINAL" in sql:
            assert (params or {})["key"] == "home"
            return [{"memory_id": "m1"}, {"memory_id": "m2"}]
        self.vector_reads.append(sql)
        rows = await super().query_all(sql, params)
        # Each vector row was copied at read + 1 since its memory row (version 1).
        return [{**r, "version": 2} for r in rows]

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.executed.append(sql)


def _wire_sessions(monkeypatch: pytest.MonkeyPatch, ch: _CH) -> None:
    """m1 and m2: the key's row in sessions s1 and s2, both at version 1."""
    _wire(monkeypatch, ch, _existing())
    heads = {
        mid: SimpleNamespace(
            **{**vars(_existing()), "memory_id": mid, "session_id": s, "version": 1}
        )
        for mid, s in (("m1", "s1"), ("m2", "s2"))
    }

    async def _get_by_id(memory_id: str, **_: Any) -> Any:
        return heads[memory_id]

    monkeypatch.setattr(lifecycle, "get_by_id", _get_by_id)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
