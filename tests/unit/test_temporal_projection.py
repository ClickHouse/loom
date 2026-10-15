"""Catching the projections up: replay stops below a gap, vectors are copied when
the live row still holds the same version and embedded otherwise."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.memory.app import temporal_projection as tp
from server.memory.domain import temporal_model as tm
from server.memory.store import temporal as store


@pytest.fixture(autouse=True)
def _server_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    async def now(**_k: Any) -> datetime:
        return datetime.now(UTC)  # ClickHouse's clock, in step with the host here

    monkeypatch.setattr(store, "server_now", now)


def _commit(
    seq: int,
    added: list[tm.Assertion],
    retired: tuple[str, ...] = (),
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "commit_seq": seq,
        "commit_id": f"c{seq}",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "memory_id": "m",
        "scope": "private",
        "recorded_at": at or datetime.now(UTC),
        "retired_assertion_ids": list(retired),
        "assertions": json.dumps([a.to_json() for a in added]),
    }


@pytest.fixture(autouse=True)
def _journal_intact(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every commit a replay read is still there unless a test erases it."""
    deleted: list[list[str]] = []

    async def present(_org: str, _ns: str, ids: list[str], **_k: Any) -> list[dict[str, Any]]:
        return [{"commit_id": i} for i in ids]

    async def delete(_org: str, _ns: str, ids: list[str], **_k: Any) -> None:
        deleted.append(list(ids))

    async def keep(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(store, "commits_by_id", present)
    monkeypatch.setattr(store, "delete_assertions", delete)
    monkeypatch.setattr(store, "drop_unbacked_vectors", keep)
    return deleted


def test_a_replay_undoes_rows_of_a_commit_erased_under_it(
    monkeypatch: pytest.MonkeyPatch, _journal_intact: list[list[str]]
) -> None:
    a = tm.first('"private"')
    at = datetime.now(UTC)

    async def head(*_a: Any, **_k: Any) -> tuple[int, Any, int]:
        return 1, at, 0

    async def between(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [_commit(1, [a], at=at)]

    async def gone(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []  # the erasure deleted the journal after the replay read it

    async def none(*_a: Any, **_k: Any) -> Any:
        return []

    monkeypatch.setattr(store, "head", head)
    monkeypatch.setattr(store, "commits_between", between)
    monkeypatch.setattr(store, "commits_by_id", gone)
    monkeypatch.setattr(store, "assertions_by_id", none)
    monkeypatch.setattr(store, "insert_assertions", none)
    monkeypatch.setattr(store, "advance_watermark", none)
    asyncio.run(tp.replay("o", "n", 0, 1))
    assert _journal_intact == [[a.assertion_id]]


@pytest.mark.parametrize(("gap_age", "watermark"), [(timedelta(0), 2), (timedelta(minutes=5), 4)])
def test_replay_stops_below_a_fresh_gap_and_steps_over_a_settled_one(
    monkeypatch: pytest.MonkeyPatch, gap_age: timedelta, watermark: int
) -> None:
    # Sequence 3 is missing. Fresh, an insert may still land there; older than any
    # publisher lease it was withdrawn and can never fill.
    a = tm.first('"A"')
    b = tm.first('"B"')
    c = tm.first('"C"')
    at = datetime.now(UTC) - gap_age
    commits = [
        _commit(1, [a], at=at),
        _commit(2, [b], (a.assertion_id,), at=at),
        _commit(4, [c], at=at),
    ]
    inserted: list[dict[str, Any]] = []
    advanced: list[int] = []

    async def between(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return commits

    async def by_id(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []

    async def insert(rows: list[dict[str, Any]], **_k: Any) -> None:
        store._check(rows, store.ASSERTION_COLS, store.ASSERTIONS)
        inserted.extend(rows)

    async def advance(org: str, ns: str, seq: int, *_a: Any, **_k: Any) -> None:
        advanced.append(seq)

    async def head(*_a: Any, **_k: Any) -> tuple[int, Any, int]:
        return 4, at, 0

    monkeypatch.setattr(store, "head", head)
    monkeypatch.setattr(store, "commits_between", between)
    monkeypatch.setattr(store, "assertions_by_id", by_id)
    monkeypatch.setattr(store, "insert_assertions", insert)
    monkeypatch.setattr(store, "advance_watermark", advance)
    assert asyncio.run(tp.replay("o", "n", 0, 4)) == watermark
    assert advanced == [watermark]
    retired = [r for r in inserted if r["assertion_id"] == a.assertion_id and r["retired_seq"] == 2]
    assert retired
    assert any(r["assertion_id"] == c.assertion_id for r in inserted) is (watermark == 4)


def test_vectors_are_copied_when_the_version_matches_else_embedded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "org": "o",
            "namespace": "n",
            "memory_key": "k1",
            "memory_type": "semantic",
            "value": '"one"',
            "embed_input_sha256": "1" * 64,
            "source_version": 7,
        },
        {
            "org": "o",
            "namespace": "n",
            "memory_key": "k2",
            "memory_type": "semantic",
            "value": '"two"',
            "embed_input_sha256": "2" * 64,
            "source_version": 8,
        },
    ]
    written: list[dict[str, Any]] = []

    async def missing(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return rows

    async def current(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [{"memory_key": "k1", "version": 7, "embedding_dim": 2, "embedding": [0.1, 0.2]}]

    async def insert(out: list[dict[str, Any]], **_k: Any) -> None:
        written.extend(out)

    class Embedder:
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [[0.5, 0.5] for _ in texts]

    monkeypatch.setattr(store, "missing_vectors", missing)
    monkeypatch.setattr(store, "current_vectors", current)
    monkeypatch.setattr(store, "insert_vectors", insert)
    assert asyncio.run(tp.fill_vectors("m", Embedder())) == 2
    by_sha = {w["embed_input_sha256"]: w for w in written}
    assert by_sha["1" * 64]["embedding"] == [0.1, 0.2]  # loom.memory_temporal_vectors row copied
    assert by_sha["2" * 64]["embedding"] == [0.5, 0.5]
    assert all(w["embedding_model"] == "m" for w in written)


def test_replay_stops_below_an_integrity_break(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sequence 2 holds two payloads: replaying either would project a guess.
    seen: list[int] = []

    async def head(*_a: Any, **_k: Any) -> tuple[int, Any, int]:
        return 3, datetime.now(UTC), 2

    async def between(_org: str, _ns: str, lo: int, hi: int, **_k: Any) -> list[dict[str, Any]]:
        seen.append(hi)
        return [_commit(1, [tm.first('"A"')], at=datetime.now(UTC))]

    async def none(*_a: Any, **_k: Any) -> Any:
        return []

    monkeypatch.setattr(store, "head", head)
    monkeypatch.setattr(store, "commits_between", between)
    monkeypatch.setattr(store, "assertions_by_id", none)
    monkeypatch.setattr(store, "insert_assertions", none)
    monkeypatch.setattr(store, "advance_watermark", none)
    assert asyncio.run(tp.replay("o", "n", 0, 3)) == 1
    assert seen == [1]


def test_the_reconciler_leaves_a_scope_under_erasure_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # The saga removes journal and current rows in steps: in between, nothing is
    # written back, and the namespace's cursor stays where it was.
    old = datetime.now(UTC) - timedelta(hours=1)
    advanced: list[tuple[str, str, str]] = []

    async def unchecked(_cursor: str, **_k: Any) -> list[dict[str, Any]]:
        return [{"org": "o", "namespace": "n", "head": 1, "applied": 0}]

    async def after(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        ident = {"agent": "a", "user_id": "u", "session_id": "-", "memory_key": "k"}
        return [{"commit_seq": 1, "recorded_at": old, **ident}]

    async def held(*_a: Any, **_k: Any) -> bool:
        return True

    async def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("read while an erasure runs")

    async def nothing(*_a: Any, **_k: Any) -> Any:
        return []

    async def zero(*_a: Any, **_k: Any) -> int:
        return 0

    async def advance(org: str, ns: str, _seq: int, projection: str, **_k: Any) -> None:
        advanced.append((org, ns, projection))

    monkeypatch.setattr(store, "unchecked_namespaces", unchecked)
    monkeypatch.setattr(store, "commits_after", after)
    monkeypatch.setattr(store, "boundaries_after", nothing)
    monkeypatch.setattr(store, "pending_boundaries", nothing)
    monkeypatch.setattr(store, "watermark", zero)
    monkeypatch.setattr(store, "advance_watermark", advance)
    monkeypatch.setattr(tp.fence, "scope_held", held)
    monkeypatch.setattr(store, "cutoff", never)
    assert asyncio.run(tp.reconcile_current()) == 0
    assert advanced == [("", "", "current_boundary")]


def test_a_trailing_failed_allocation_is_stepped_over_once_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advanced: list[int] = []

    async def head(*_a: Any, **_k: Any) -> tuple[int, Any, int]:
        return 2, None, 0

    async def none(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def advance(_org: str, _ns: str, seq: int, *_a: Any, **_k: Any) -> None:
        advanced.append(seq)

    monkeypatch.setattr(store, "head", head)
    monkeypatch.setattr(store, "commits_between", none)
    monkeypatch.setattr(store, "advance_watermark", advance)
    fresh = datetime.now(UTC)
    assert asyncio.run(tp.replay("o", "n", 1, 2, published_at=fresh)) == 1
    old = fresh - timedelta(minutes=5)
    assert asyncio.run(tp.replay("o", "n", 1, 2, published_at=old)) == 2
    assert advanced == [2]


def test_a_held_namespace_keeps_its_own_place_and_the_cursor_moves_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Another tenant's transitions must not wait on one namespace an erasure holds.
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    cursor: list[int] = []
    held_at: list[tuple[str, str, int]] = []

    async def none(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def zero(*_a: Any, **_k: Any) -> int:
        return 0

    async def due(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        ident = {"agent": "a", "user_id": "u", "session_id": "-", "memory_key": "k"}
        return [{"org": "o", "namespace": "n", "boundary": due_at, **ident}]

    async def held(*_a: Any, **_k: Any) -> bool:
        return True

    async def advance(_org: str, _ns: str, value: int, projection: str, **_k: Any) -> None:
        cursor.append(value)

    async def hold(org: str, ns: str, since_us: int, **_k: Any) -> None:
        held_at.append((org, ns, since_us))

    monkeypatch.setattr(store, "unchecked_namespaces", none)
    monkeypatch.setattr(store, "pending_boundaries", none)
    monkeypatch.setattr(store, "watermark", zero)
    monkeypatch.setattr(store, "boundaries_after", due)
    monkeypatch.setattr(store, "advance_watermark", advance)
    monkeypatch.setattr(store, "hold_boundary", hold)
    monkeypatch.setattr(tp.fence, "scope_held", held)
    asyncio.run(tp.reconcile_current())
    assert held_at == [("o", "n", int(due_at.timestamp() * 1e6) - 1)]
    assert cursor and cursor[0] > int(due_at.timestamp() * 1e6)


def test_reconciling_waits_for_the_projection_to_cover_the_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import temporal_reads as reads
    from server.memory.domain.temporal_reads import Axes

    async def free(*_a: Any, **_k: Any) -> bool:
        return False

    async def lagging(org: str, ns: str, axes: Axes, **_k: Any) -> Any:
        return reads.Cut(namespace=ns, axes=axes, seq=5, head=5, watermark=3)

    async def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("compared against a stale projection")

    monkeypatch.setattr(tp.fence, "scope_held", free)
    monkeypatch.setattr(reads, "cutoff", lagging)
    monkeypatch.setattr(store, "divergent_rows", never)
    assert asyncio.run(tp.reconcile_identities("o", "n", [("a", "u", "-", "k")])) is None


def test_a_pending_place_catches_up_to_the_advanced_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    # Held in this very pass after the old cursor: its range runs to the new one.
    asked: list[tuple[datetime, datetime]] = []
    now = datetime.now(UTC)

    async def none(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def old_cursor(*_a: Any, **_k: Any) -> int:
        return int((now - timedelta(hours=1)).timestamp() * 1e6)

    async def pending(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [
            {
                "org": "o",
                "namespace": "n",
                "since": int((now - timedelta(minutes=5)).timestamp() * 1e6),
            }
        ]

    async def boundaries(after: datetime, upto: datetime, **k: Any) -> list[Any]:
        if k.get("org"):
            asked.append((after, upto))
        return []

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(store, "unchecked_namespaces", none)
    monkeypatch.setattr(store, "watermark", old_cursor)
    monkeypatch.setattr(store, "boundaries_after", boundaries)
    monkeypatch.setattr(store, "pending_boundaries", pending)
    monkeypatch.setattr(store, "advance_watermark", nothing)
    monkeypatch.setattr(store, "release_boundary", nothing)
    asyncio.run(tp.reconcile_current())
    assert len(asked) == 1 and asked[0][1] >= now


def test_an_attestation_time_is_part_of_the_serving_state() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from server.memory.app import temporal_projection as tp

    at = datetime(2026, 1, 1, tzinfo=UTC)
    common = dict(
        content_sha256="h", memory_type="semantic", status="active", tags=(), entities=(),
        pinned=False, lock_mode="", sensitivity="", verified_by="v", verified_sha256="h",
        valid_to=None, expires_at=None, confidence=1.0,
    )  # fmt: skip
    a = SimpleNamespace(**common, verified_at=at, valid_from=None, validity_basis="stated")
    row = SimpleNamespace(**common, verified_at=at, valid_at=None, temporal_anchor=None)
    assert tp._state_of(a) == tp._state_of_row(row)
    row.verified_at = None  # a re-verify the current row never got
    assert tp._state_of(a) != tp._state_of_row(row)


def test_one_failing_namespace_keeps_its_place_and_stops_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from server.memory.app import temporal_projection as tp

    async def _ns(org: str, ns: str, idents: list) -> int | None:
        if ns == "bad":
            raise ConnectionError("insert failed")
        return 1

    monkeypatch.setattr(tp, "_reconcile_ns", _ns)
    due = [
        {"org": "o", "namespace": n, "agent": "a", "user_id": "-", "session_id": "-",
         "memory_key": "k", "boundary": datetime(2026, 1, 1, tzinfo=UTC)}
        for n in ("bad", "good")
    ]  # fmt: skip
    held: list[str] = []

    async def _hold(org: str, ns: str, since_us: int, **_: object) -> None:
        held.append(ns)

    monkeypatch.setattr(tp.store, "hold_boundary", _hold)
    fixed, kept = asyncio.run(tp._boundaries(due))
    assert (fixed, kept, held) == (1, {("o", "bad")}, ["bad"])


def test_a_rewrite_never_overwrites_a_commit_newer_than_its_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # want was read at cut 5; an acknowledged write committed seq 6 meanwhile.
    import asyncio

    from server.memory.app import temporal_projection as tp

    ident = ("a", "-", "-", "k")
    wrote: list[Any] = []

    async def _head(*_a: Any, **_k: Any) -> Any:
        return None

    async def _later(org: str, ns: str, lo: int, hi: int, **_k: Any) -> list[dict[str, Any]]:
        wrote.append(("checked past", lo))
        return [{"agent": "a", "user_id": "-", "session_id": "-", "memory_key": "k"}]

    async def _insert(rows: Any, **_k: Any) -> int:
        wrote.extend(rows)
        return len(rows)

    monkeypatch.setattr(tp.memory_repo, "get_by_key", _head)
    monkeypatch.setattr(tp.store, "commits_between", _later)
    monkeypatch.setattr(tp.memory_repo, "insert_memory_versions", _insert)
    assert asyncio.run(tp._rewrite_current("o", "n", ident, [], cut_seq=5)) is False
    assert wrote == [("checked past", 5)]  # the journal was asked; nothing written
