"""namespace.rollback planning: head-state rollback over the
FULL OUTER JOIN of live heads and heads-as-of-T. CH-free."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from server.memory.domain.rollback import plan_rollback, version_at


def _r(key: str, now: int, at: int, tomb: int = 0) -> dict:
    return {
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "version_now": now,
        "version_at": at,
        "tombstoned_now": tomb,
    }


def test_version_at_is_epoch_ns_and_naive_means_utc() -> None:
    t = datetime(2026, 9, 12, 6, 45, tzinfo=UTC)
    assert version_at(t) == int(t.timestamp()) * 1_000_000_000
    assert version_at(t.replace(tzinfo=None)) == version_at(t)


def test_reinstate_when_head_moved_after_t() -> None:
    plan = plan_rollback([_r("k", now=300, at=200)])
    assert [a.action for a in plan.reinstate] == ["reinstate"]
    a = plan.reinstate[0]
    assert (a.version_from, a.version_to, a.memory_key) == (300, 200, "k")
    assert plan.remove == [] and plan.changes == 1


def test_remove_when_key_born_after_t_and_live() -> None:
    plan = plan_rollback([_r("k", now=300, at=0)])
    assert [a.action for a in plan.remove] == ["remove"]
    assert plan.remove[0].version_from == 300 and plan.remove[0].version_to == 0


def test_born_after_t_but_already_tombstoned_is_skipped() -> None:
    plan = plan_rollback([_r("k", now=300, at=0, tomb=1)])
    assert plan.changes == 0 and plan.skipped_already_gone == 1


def test_history_without_live_head_is_never_resurrected() -> None:
    # Erasure hard-deletes both tables; a stray version row must not come back.
    plan = plan_rollback([_r("k", now=0, at=200)])
    assert plan.changes == 0 and plan.skipped_no_head == 1


def test_agreeing_versions_are_tolerated_and_ignored() -> None:
    plan = plan_rollback([_r("k", now=200, at=200)])
    assert plan.changes == 0


def test_tombstoned_head_at_t_is_still_reinstated_as_tombstone() -> None:
    # The key was deleted before T and re-created after: rolling back must
    # re-insert the tombstone version, not leave the newer live head.
    plan = plan_rollback([_r("k", now=400, at=250, tomb=0)])
    assert plan.reinstate[0].version_to == 250


def test_plan_orders_reinstate_before_remove_in_sample_order() -> None:
    plan = plan_rollback([_r("b", 300, 0), _r("a", 300, 200), _r("c", 0, 100)])
    assert [a.memory_key for a in plan.reinstate] == ["a"]
    assert [a.memory_key for a in plan.remove] == ["b"]
    assert plan.skipped_no_head == 1
    assert plan.reinstate[0].as_dict()["action"] == "reinstate"


async def test_apply_reinstates_the_historical_row_and_tombstones_the_newborn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan's writes: a reinstated key re-emits its version-as-of-T as a new head, a key
    born after T is tombstoned, and each gets a dim-0 embedding stub for the worker."""
    from types import SimpleNamespace

    import server.embedding as embedding
    import server.memory.store as store
    from server.memory.app.namespace_rollback import apply_rollback

    def _row(key: str, version: int, value: str) -> store.MemoryRow:
        return store.MemoryRow(
            memory_id="11111111-1111-1111-1111-111111111111",
            org="o",
            namespace="n",
            agent="a",
            user_id="-",
            session_id="-",
            memory_key=key,
            memory_type="semantic",
            value=value,
            confidence=0.9,
            version=version,
            pinned=False,
            created_at=datetime(2026, 1, 1),
            expires_at=None,
        )

    async def _fetch(org: str, ns: str, pairs: list[tuple[str, int]], *, ch: object) -> list:
        assert (org, ns, pairs) == ("o", "n", [("old", 200)])
        return [_row("old", 200, "as of T")]

    async def _head(identity: object, key: str, *, fresh: bool, scope: object) -> store.MemoryRow:
        assert key == "new" and fresh
        return _row("new", 300, "born later")

    stubs: list[dict] = []

    async def _stubs(rows: list[dict], *, ch: object) -> None:
        stubs.extend(rows)

    monkeypatch.setattr(store, "fetch_version_rows", _fetch)
    monkeypatch.setattr(store, "get_by_key", _head)
    monkeypatch.setattr(store, "insert_embedding_rows", _stubs)
    monkeypatch.setattr(embedding, "get_embedder", lambda: SimpleNamespace(dim=3, model_name="m"))
    inserted: list[dict] = []

    class _CH:
        async def insert(self, table: str, rows: list[dict], **_: object) -> None:
            assert table == "loom.memory"
            inserted.extend(rows)

    plan = plan_rollback([_r("old", now=300, at=200), _r("new", now=300, at=0)])
    applied = datetime(2026, 2, 1)
    await apply_rollback("o", "n", plan, applied_at=applied, ch=_CH())

    by_key = {r["memory_key"]: r for r in inserted}
    assert by_key["old"]["tombstoned_at"] is None and by_key["old"]["version"] > 200
    assert by_key["new"]["tombstoned_at"] == applied
    assert {s["memory_key"]: s["embedding_dim"] for s in stubs} == {"old": 0, "new": 0}
