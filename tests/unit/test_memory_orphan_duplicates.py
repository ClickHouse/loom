"""Unit tests for the orphaned-duplicate resolver.

An "orphan duplicate" is a live active-head row stuck at status='duplicate'
whose `duplicate_of` is empty OR points at a memory that is no longer a live
active head in the namespace (tombstoned / superseded / gone). Nothing in the
existing surface re-activates these (conflicts.resolve handles 'conflicted';
knowledge.dedup_resolve is the knowledge plane; both reject an empty canonical),
so `memory.resolve_orphan_duplicates` flips them back to 'active'.

Two layers are covered:
  * the repository scan SQL shape (predicate + the argMax-nesting alias guard);
  * the handler behaviour against a faked in-memory repo that implements the
    orphan predicate, for cases (a)-(d) in the spec.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.store import find_orphan_duplicates
from server.memory.store.dedup import OrphanDuplicate

# --------------------------------------------------------------------------- #
# Repository scan — SQL shape / argMax-nesting guard
# --------------------------------------------------------------------------- #


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []


@pytest.mark.asyncio
async def test_find_orphan_duplicates_scan_predicate_and_alias_safety() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await find_orphan_duplicates("clickhouse", "ns", batch_size=250, ch=ch)
    sql, params = ch.query_all_calls[0]

    # The orphan predicate.
    assert "status = 'duplicate'" in sql
    assert "duplicate_of = ''" in sql
    assert "duplicate_of NOT IN (" in sql
    # The live-active canonical set filters on live AND active heads.
    assert "argMax(tombstoned_at IS NULL, version) = 1" in sql
    assert "argMax(status, version) IN ('active', 'conflicted')" in sql
    # Head liveness gate on the outer scan.
    assert "HAVING live = 1" in sql

    # argMax-nesting trap: the freshness input is aliased max_version, never
    # `version` — shadowing `version` would turn argMax(x, version) into the
    # illegal argMax(x, max(version)).
    assert "AS max_version" in sql
    assert "max(version) AS version" not in sql
    assert "argMax(status, version)" in sql

    assert params["org"] == "clickhouse"
    assert params["ns"] == "ns"
    assert params["lim"] == 250


# --------------------------------------------------------------------------- #
# Handler — behaviour against a faked repo implementing the orphan predicate
# --------------------------------------------------------------------------- #


class FakeMemoryTable:
    """In-memory stand-in that implements the orphan predicate the SQL encodes.

    Rows are raw version rows; the head per identity key is the max-version row.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.mark_active_calls: list[tuple[str, list[str]]] = []

    def _heads(self) -> list[dict[str, Any]]:
        by_key: dict[tuple, dict[str, Any]] = {}
        for r in self.rows:
            k = (r["agent"], r["user_id"], r["session_id"], r["memory_key"])
            cur = by_key.get(k)
            if cur is None or r["version"] > cur["version"]:
                by_key[k] = r
        return list(by_key.values())

    async def find_orphan_duplicates(
        self, org: str, namespace: str, *, batch_size: int = 500, ch: Any = None
    ) -> list[OrphanDuplicate]:
        heads = [h for h in self._heads() if h["org"] == org and h["namespace"] == namespace]
        live_active_ids = {h["memory_id"] for h in heads if h["live"] and h["status"] == "active"}
        out = [
            {"memory_id": h["memory_id"], "memory_key": h["memory_key"]}
            for h in heads
            if h["live"]
            and h["status"] == "duplicate"
            and (h["duplicate_of"] == "" or h["duplicate_of"] not in live_active_ids)
        ]
        return [OrphanDuplicate.from_row(x) for x in out[:batch_size]]

    async def mark_active_by_ids(
        self, org: str, memory_ids: list[str], *, namespace: str, ch: Any = None
    ) -> None:
        self.mark_active_calls.append((org, list(memory_ids)))
        if not memory_ids:
            return
        ids = set(memory_ids)
        # Re-emit as a new active head version (models write_versions_by_ids).
        new_version = max((r["version"] for r in self.rows), default=0) + 1
        promotions = [
            {**r, "status": "active", "version": new_version}
            for r in self._heads()
            if r["memory_id"] in ids
        ]
        self.rows.extend(promotions)


def _row(
    memory_key: str, memory_id: str, *, status: str, duplicate_of: str = "", live: bool = True
) -> dict[str, Any]:
    return {
        "org": "clickhouse",
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": memory_key,
        "memory_id": memory_id,
        "status": status,
        "duplicate_of": duplicate_of,
        "live": live,
        "version": 1,
    }


def _seed() -> FakeMemoryTable:
    return FakeMemoryTable(
        [
            # (a) duplicate with empty duplicate_of -> orphan
            _row("a_empty", "id-a", status="duplicate", duplicate_of=""),
            # (b) duplicate pointing at a LIVE ACTIVE canonical -> real duplicate
            _row("b_dup", "id-b", status="duplicate", duplicate_of="id-canon"),
            _row("b_canon", "id-canon", status="active"),
            # (c1) duplicate pointing at a TOMBSTONED canonical -> orphan
            _row("c_dup", "id-c", status="duplicate", duplicate_of="id-tomb"),
            _row("c_canon", "id-tomb", status="active", live=False),
            # (c2) duplicate pointing at a NONEXISTENT id -> orphan
            _row("c_missing", "id-d", status="duplicate", duplicate_of="id-ghost"),
        ]
    )


def _patch(monkeypatch: pytest.MonkeyPatch, fake: FakeMemoryTable) -> None:
    from server.memory import store as memory_repo
    from server.memory.store import conflicts as conflicts_repo

    monkeypatch.setattr(memory_repo, "find_orphan_duplicates", fake.find_orphan_duplicates)
    monkeypatch.setattr(conflicts_repo, "mark_active_by_ids", fake.mark_active_by_ids)


async def _run(dry_run: bool):
    from server.memory.api.models_admin import MemoryResolveOrphanDuplicatesInput
    from server.memory.app.ops import handle_memory_resolve_orphan_duplicates

    return await handle_memory_resolve_orphan_duplicates(
        MemoryResolveOrphanDuplicatesInput(org="clickhouse", namespace="ns", dry_run=dry_run)
    )


@pytest.mark.asyncio
async def test_orphans_reactivated_real_duplicate_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(a) empty duplicate_of, (c1) tombstoned canonical, (c2) missing canonical
    are all re-activated; (b) a duplicate of a live active canonical is left."""
    fake = _seed()
    _patch(monkeypatch, fake)

    out = await _run(dry_run=False)

    assert set(out.memory_ids) == {"id-a", "id-c", "id-d"}
    assert "id-b" not in out.memory_ids  # real duplicate untouched
    assert out.candidates == 3
    assert out.reactivated == 3
    assert out.dry_run is False
    # The write path was invoked with exactly the orphan ids.
    assert fake.mark_active_calls == [("clickhouse", ["id-a", "id-c", "id-d"])]


@pytest.mark.asyncio
async def test_reactivation_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-activated row is no longer status='duplicate', so a second pass
    finds nothing and writes nothing."""
    fake = _seed()
    _patch(monkeypatch, fake)

    first = await _run(dry_run=False)
    assert first.reactivated == 3

    second = await _run(dry_run=False)
    assert second.candidates == 0
    assert second.reactivated == 0
    # Second pass still calls mark_active (with an empty list) — a harmless
    # short-circuit — but promotes nothing new.
    assert fake.mark_active_calls[-1] == ("clickhouse", [])


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d) dry_run returns the candidates but never touches the write path."""
    fake = _seed()
    _patch(monkeypatch, fake)

    out = await _run(dry_run=True)

    assert out.dry_run is True
    assert out.candidates == 3
    assert out.reactivated == 0
    assert set(out.memory_ids) == {"id-a", "id-c", "id-d"}
    # No write path invocation at all under dry_run.
    assert fake.mark_active_calls == []
