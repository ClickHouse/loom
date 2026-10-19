"""A delete that commits while a memory.set is in flight, against the real loom.memory table
on clickhouse-local. The set drew its version before the delete (it embeds in between), so
the tombstone's version is the higher one; the set's insert takes a new version above the
tombstone, and its row is the one read back. The derived tables (vectors, chunks) are
absent: their best-effort retires log, and the row tombstone stands."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import server.memory.app.handlers as hm
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory import store as memory_repo
from server.memory.api.models_crud import (
    MemoryDeleteInput,
    MemoryDeleteVersionInput,
    MemorySetInput,
)
from server.memory.app import set_commit
from server.memory.store import key_write_lock
from server.memory.store.rows import build_first_version_row
from tests.unit.clickhouse_local import CLICKHOUSE, ROOT, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

# A fixed version for the lift tests: the offset hashes the drawn version, so a fixed one
# keeps their expected versions the same on every run (a multiple of 1,000, as a clock that
# reports microseconds draws).
_FIXED_HEAD = 1_900_000_000_000_000_000
_ID = {"org": "o", "namespace": "n", "agent": "okf-sync", "user_id": "-", "session_id": "-"}
_MID = "00000000-0000-0000-0000-00000000000a"
_KEY = "deploy_window"


def _versions_view() -> str:
    """The materialized view that copies every loom.memory insert into memory_versions."""
    schema = (ROOT / "schemas" / "schema.sql").read_text()
    head = "CREATE MATERIALIZED VIEW IF NOT EXISTS loom.memory_versions_mv"
    return head + schema.split(head, 1)[1].split(";", 1)[0]


class _StatefulCH:
    """clickhouse-local that keeps every insert: each query runs after all of them."""

    def __init__(self) -> None:
        self.setup = [
            ddl("memory"),
            ddl("memory_versions"),
            _versions_view(),
            ddl("delete_version_checks"),
        ]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if not any(f"CREATE TABLE {table}\n" in stmt for stmt in self.setup):
            return  # a table this test does not hold, as a write the test does not read
        for r in rows:
            cols = ", ".join(r)
            data = json.dumps(r, default=str)
            self.setup.append(
                f"INSERT INTO {table} ({cols}) SELECT {cols} FROM format(JSONEachRow, $${data}$$)"
            )

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        try:
            rows = run(self.setup, sql, params)
        except AssertionError as e:
            if "UNKNOWN_TABLE" not in str(e):
                raise
            return []  # a table this test does not hold reads as empty
        return [_as_driver(r) for r in rows]

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None


_DATETIME = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")


def _as_driver(row: dict[str, Any]) -> dict[str, Any]:
    """A DateTime column as the driver answers it: a datetime, not JSON text."""
    return {
        k: datetime.fromisoformat(v) if isinstance(v, str) and _DATETIME.match(v) else v
        for k, v in row.items()
    }


def _row(value: str, version: int) -> dict[str, Any]:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    return build_first_version_row(
        {
            **_ID,
            "memory_id": _MID,
            "memory_key": _KEY,
            "memory_type": "procedural",  # not embedded: memory.set's plain insert
            "value": json.dumps(value),
            "value_json": "{}",
            "content_sha256": "",
            "actor": "process:okf-sync",
            "source_class": "imported",
            "confidence": 1.0,
            "version": version,
        },
        now=now,
    )


async def _recorded_checks(ch: _StatefulCH) -> list[dict[str, Any]]:
    """Every check delete_version recorded, done or not, oldest tombstone first."""
    return await ch.query_all(
        "SELECT checked, tombstone, done FROM loom.delete_version_checks FINAL ORDER BY tombstone"
    )


async def _read_fails(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("timeout")


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _StatefulCH:
    ch = _StatefulCH()

    @asynccontextmanager
    async def _no_lease(*_key: str) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", _no_lease)
    monkeypatch.setattr(hm, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.store.reads._default_client", lambda: ch)
    # Every other read and write the handlers make through the process's client.
    from server.core import ch as ch_mod

    for name in ("query_all", "query_one", "insert"):
        monkeypatch.setattr(ch_mod.get_client(), name, getattr(ch, name))
    return ch


def _in_flight_set(version: int) -> set_commit._SetWrite:
    """memory.set's state after its embed: the row built at the version drawn at the start."""
    w = set_commit._SetWrite(
        input=MemorySetInput(**_ID, memory_key=_KEY, value="Thursdays", memory_type="procedural"),
        version=version,
        retyped_from="",
        existing=None,
        expires_at=None,
    )
    w.memory_row = _row("Thursdays", version)
    w.memory_id = _MID
    return w


async def _read_back(ch: _StatefulCH) -> Any:
    return await memory_repo.get_by_key(
        memory_repo.Identity(**_ID), _KEY, strong=True, scope=WHOLE_NAMESPACE, ch=ch
    )


async def _delete_during_the_set(
    ch: _StatefulCH, op: str, *, head_row: dict[str, Any] | None = None
) -> set_commit._SetWrite:
    head_row = head_row or _row("Tuesdays", time.time_ns() - 10_000_000_000)
    head = int(head_row["version"])
    await ch.insert("loom.memory", [head_row])
    w = _in_flight_set(head + 5_000_000)  # drawn after the head, before the delete
    if op == "memory.delete":
        out: Any = await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))
    else:
        out = await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    assert out.tombstoned is True and out.version > w.version
    return w


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["memory.delete_version", "memory.delete"])
async def test_a_set_in_flight_during_a_delete_keeps_its_row(ch: _StatefulCH, op: str) -> None:
    w = await _delete_during_the_set(ch, op)
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and got.version == w.version


@pytest.mark.asyncio
async def test_without_the_lift_the_tombstone_hides_the_set(ch: _StatefulCH) -> None:
    # The race the lift closes: the same insert at the version drawn before the delete.
    w = await _delete_during_the_set(ch, "memory.delete_version")
    await ch.insert("loom.memory", [w.memory_row])
    assert await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_tombstone_older_than_the_write_leaves_its_version(ch: _StatefulCH) -> None:
    # The key was deleted before this write began: the version drawn at its start stands.
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    w = _in_flight_set(time.time_ns())
    drawn = w.version
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert got is not None and got.version == drawn


@pytest.mark.asyncio
async def test_a_conditional_set_in_flight_during_a_delete_is_refused(ch: _StatefulCH) -> None:
    # Its precondition read the row the delete removed; landing would undo the delete. The
    # refusal is memory.set's own precondition answer, so the write is counted as one.
    from fastapi import HTTPException

    w = await _delete_during_the_set(ch, "memory.delete")
    w.input = w.input.model_copy(update={"if_match_sha256": "a" * 64})
    with pytest.raises(HTTPException) as refused:
        await set_commit._set_plain_insert(w, ch)
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "PRECONDITION_FAILED"
    assert await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_key_a_sweep_caught_and_restore_brought_back_deletes_as_a_delete(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The sweep's marker stays on its tombstone: the restored row does not carry it, so a
    # later delete of the key is a delete to the write fence.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    monkeypatch.setattr(restore_mod, "get_client", lambda: ch)
    head = time.time_ns() - 20_000_000_000
    swept = {
        **_row("Tuesdays", head + 1),
        "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
        "expire_reason": "blacklist.sweep:p1",
    }
    await ch.insert("loom.memory", [_row("Tuesdays", head), swept])
    out = await restore_mod.handle_memory_restore(MemoryRestoreInput(**_ID, memory_key=_KEY))
    assert out.restored is True
    restored = await _read_back(ch)
    assert restored is not None
    w = _in_flight_set(restored.version + 5_000_000)
    await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=restored.version)
    )
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"


@pytest.mark.asyncio
async def test_a_write_drawn_after_the_delete_keeps_winning(ch: _StatefulCH) -> None:
    # A set drawn after the delete lands first; the set in flight across the delete then
    # reaches its fence. The key's newest version is no tombstone, so it is not lifted.
    w = await _delete_during_the_set(ch, "memory.delete")
    later = {**_row("Fridays", time.time_ns()), "memory_id": "00000000-0000-0000-0000-00000000000b"}
    await ch.insert("loom.memory", [later])
    drawn = w.version
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert w.version == drawn and got is not None and got.value == "Fridays"


@pytest.mark.asyncio
async def test_a_blacklist_sweeps_tombstone_keeps_the_write_under_it(ch: _StatefulCH) -> None:
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(head + 5_000_000)
    swept = {
        **_row("Tuesdays", time.time_ns()),
        "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
        "expire_reason": "blacklist.sweep:p1",
    }
    await ch.insert("loom.memory", [swept])
    drawn = w.version
    await set_commit._set_plain_insert(w, ch)
    assert w.version == drawn and await _read_back(ch) is None


@pytest.mark.asyncio
async def test_two_writes_lifted_over_one_delete_differ_under_clock_skew(
    ch: _StatefulCH,
) -> None:
    # The deleting replica's clock runs ahead. Both writes read the fence before either
    # lands: each sits just above the tombstone, offset by a hash of its row. The versions
    # are fixed, so the offsets are too.
    from server.memory.app.write_fence import lift_above_deletes

    head = _FIXED_HEAD
    tombstone = {
        **_row("Tuesdays", head),
        "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
    }
    await ch.insert("loom.memory", [tombstone])
    first, second = _row("one", head - 2), _row("two", head - 1)
    await lift_above_deletes("o", "n", [first], [], ch=ch)
    await lift_above_deletes("o", "n", [second], [], ch=ch)
    assert first["version"] != second["version"]
    assert min(first["version"], second["version"]) > head
    assert max(first["version"], second["version"]) <= head + 1_000


@pytest.mark.asyncio
async def test_writes_drawn_on_a_microsecond_clock_lifted_together_differ(
    ch: _StatefulCH,
) -> None:
    # A clock that reports microseconds draws versions 1,000 ns apart: their offsets come
    # from a hash of the row, not from the clock's last digits.
    from server.memory.app.write_fence import lift_above_deletes

    head = _FIXED_HEAD
    tombstone = {
        **_row("Tuesdays", head),
        "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
    }
    await ch.insert("loom.memory", [tombstone])
    rows = [_row(str(i), head - 1_000 * (i + 1)) for i in range(4)]
    for row in rows:
        await lift_above_deletes("o", "n", [row], [], ch=ch)
    assert len({r["version"] for r in rows}) == len(rows)


@pytest.mark.asyncio
async def test_a_write_the_key_moved_past_before_the_delete_stays_deleted(
    ch: _StatefulCH,
) -> None:
    # Drawn before a later write replaced it; the delete then removed that later write.
    # Lifting it would bring back a value the key had already moved past.
    w = _in_flight_set(time.time_ns() - 10_000_000_000)
    drawn = w.version
    await ch.insert("loom.memory", [_row("Wednesdays", drawn + 1_000_000)])
    await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))
    await set_commit._set_plain_insert(w, ch)
    assert w.version == drawn and await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_write_drawn_after_the_delete_stays_above_a_lifted_one(ch: _StatefulCH) -> None:
    # The older write reaches its fence first and is lifted; the write drawn after the
    # delete lands later and still wins.
    w = await _delete_during_the_set(ch, "memory.delete")
    after = time.time_ns()
    await set_commit._set_plain_insert(w, ch)
    assert w.version < after
    await ch.insert("loom.memory", [_row("Fridays", after)])
    got = await _read_back(ch)
    assert got is not None and got.value == "Fridays"


@pytest.mark.asyncio
async def test_a_fence_that_cannot_read_keeps_the_drawn_version() -> None:
    from server.memory.app.write_fence import lift_above_deletes

    class _Down:
        async def query_all(self, *_a: Any, **_k: Any) -> list[Any]:
            raise RuntimeError("timeout")

    from server.observability import degradations

    degradations.begin()
    row = _row("one", 7)
    assert await lift_above_deletes("o", "n", [row], [], ch=_Down()) == {}
    assert row["version"] == 7
    assert any("write_fence" in d for d in degradations.current())


@pytest.mark.asyncio
async def test_a_bulk_write_in_flight_during_a_delete_lands_with_its_vectors(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.setup[:0] = ["SET enable_quantized_codec = 1", ddl("memory_embeddings")]
    for name in ("index_late_rows", "retire_vectors_for_keys"):
        monkeypatch.setattr(memory_repo, name, _noop)
    monkeypatch.setattr(bulk_set, "fence_high", _noop)
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))

    # Two entries of the key in one batch, drawn before the delete, in this order.
    state = _BulkWriteState()
    for i, value in enumerate(("Wednesdays", "Thursdays")):
        v = head + 1_000 + i
        state.memory_rows.append({**_row(value, v), "memory_type": "semantic"})
        state.embedding_rows.append(
            memory_repo.build_embedding_row(
                **_ID,
                memory_key=_KEY,
                memory_type="semantic",
                content_excerpt=value,
                version=v,
                embedding=[0.0] * 3072,
                embedding_dim=3072,
                memory_key_embedding=[0.0] * 3072,
            )
        )
        state.results.append(_BulkSetResult(memory_key=_KEY, version=v))
    await bulk_set._bulk_insert(ch, state)

    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"
    assert [r.version for r in state.results] == sorted(r.version for r in state.results)
    assert state.results[1].version == got.version
    vectors = await ch.query_all(
        "SELECT content_excerpt, version FROM loom.memory_embeddings FINAL WHERE tombstoned_at IS NULL"
    )
    assert [(v["content_excerpt"], int(v["version"])) for v in vectors] == [
        ("Thursdays", got.version)
    ]


@pytest.mark.asyncio
async def test_an_embedded_set_in_flight_during_a_delete_lands_with_its_vector(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # memory.set's embedded path builds its vector from the lifted version.

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.setup[:0] = ["SET enable_quantized_codec = 1", ddl("memory_embeddings")]
    monkeypatch.setattr(memory_repo, "index_late_rows", _noop)
    monkeypatch.setattr(set_commit._cd, "schedule_settle", lambda *_a, **_k: None)
    w = await _delete_during_the_set(ch, "memory.delete")
    w.input = w.input.model_copy(update={"memory_type": "semantic"})
    w.memory_row["memory_type"] = "semantic"
    w.embedder = SimpleNamespace(dim=3072, model_name="m")
    w.excerpt, w.embedding = "Thursdays", [0.0] * 3072
    w.key_embedding, w.key_text = [0.0] * 3072, "deploy window"
    verdict = SimpleNamespace(verdict="", peers=[], refined_peers=[])
    await set_commit._set_dual_write(w, ch, None, verdict)

    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and got.version == w.version
    vectors = await ch.query_all(
        "SELECT version FROM loom.memory_embeddings FINAL WHERE tombstoned_at IS NULL"
    )
    assert [int(v["version"]) for v in vectors] == [w.version]


async def _set_through_phases(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch, *, sha: str, after_build: Any = None
) -> Any:
    """memory.set's phases against the stateful clickhouse-local (a procedural row,
    embedded), with `if_match_sha256` when `sha` is set; `after_build` runs once the row
    is built and embedded, right before the insert."""
    from server.memory.app import write

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(write, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _none)
    monkeypatch.setattr(set_commit, "_project_set_entities_fail_open", _none)
    if after_build is not None:
        dual_write = write._set_dual_write

        async def build_then_write(w: Any, *args: Any) -> Any:
            await after_build()
            return await dual_write(w, *args)

        monkeypatch.setattr(write, "_set_dual_write", build_then_write)
    return await write._memory_set_phases(
        MemorySetInput(
            **_ID,
            memory_key=_KEY,
            value="Thursdays",
            memory_type="procedural",
            if_match_sha256=sha,
        )
    )


@pytest.mark.asyncio
async def test_a_conditional_set_whose_check_passed_lands_above_the_row_it_checked(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The row it checks was written after the set began: its version is drawn after the
    # check, so it lands above that row instead of beneath it.
    from server.memory.app import write
    from server.memory.app.write import _content_sha256_of_stored

    head = time.time_ns()
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    # The set's first draw is below the row it then checks; the draw after the check is not.
    draws = iter([head - 1, head + 1])
    monkeypatch.setattr(write, "next_version", lambda: next(draws))
    sha = _content_sha256_of_stored(json.dumps("Tuesdays"))
    out = await _set_through_phases(ch, monkeypatch, sha=sha)
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and out.version == head + 1


@pytest.mark.asyncio
async def test_a_delete_after_a_conditional_sets_check_answers_409(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from server.memory.app.write import _content_sha256_of_stored

    await ch.insert("loom.memory", [_row("Tuesdays", time.time_ns() - 1_000_000)])
    sha = _content_sha256_of_stored(json.dumps("Tuesdays"))

    async def delete() -> None:
        await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))

    with pytest.raises(HTTPException) as refused:
        await _set_through_phases(ch, monkeypatch, sha=sha, after_build=delete)
    assert refused.value.detail["code"] == "PRECONDITION_FAILED"
    assert await _read_back(ch) is None


def _refused_inputs() -> list[tuple[Any, dict[str, Any]]]:
    from server.memory.api.models_apply import MemoryApplyInput
    from server.memory.api.models_crud import MemoryTouchInput

    reason = "blacklist.sweep:x"
    keys = {"org": "o", "namespace": "n", "agent": "a", "target": {"keys": [_KEY]}}
    return [
        (MemorySetInput, {**_ID, "memory_key": _KEY, "value": "v", "expire_reason": reason}),
        (
            MemoryTouchInput,
            {**_ID, "memory_keys": [_KEY], "ttl_seconds": 60, "expire_reason": reason},
        ),
        (
            MemoryApplyInput,
            {**keys, "action": "expire", "expire": {"ttl_seconds": 60}, "reason": reason},
        ),
    ]


@pytest.mark.parametrize(("model", "body"), _refused_inputs())
def test_no_caller_writes_a_sweeps_expire_reason(model: Any, body: dict[str, Any]) -> None:
    # The fence tells a sweep's tombstone by this prefix, so only the sweep stamps it.
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="may not start with"):
        model(**body)


def test_a_lock_reason_may_start_with_the_sweeps_prefix() -> None:
    # memory.apply stores `reason` as an expire_reason only for `expire`.
    from server.memory.api.models_apply import MemoryApplyInput

    MemoryApplyInput(
        org="o",
        namespace="n",
        agent="a",
        target={"keys": [_KEY]},
        action="lock",
        lock={"mode": "no_delete"},
        reason="blacklist.sweep:audit",
    )


def test_a_bulk_write_does_not_carry_a_sweeps_marker() -> None:
    from server.memory.app.bulk_rows import _bulk_carried, _BulkWriteState

    state = _BulkWriteState()
    state.existing_carried[_KEY] = {"expire_reason": "blacklist.sweep:p1"}
    assert _bulk_carried(state, _KEY)["expire_reason"] == ""
    state.existing_carried[_KEY] = {"expire_reason": "rotated"}
    assert _bulk_carried(state, _KEY)["expire_reason"] == "rotated"


@pytest.mark.asyncio
async def test_a_set_and_a_touch_do_not_carry_a_sweeps_marker(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row an older build restored from a sweep still holds the marker; the next touch or
    # set of the key writes a version without it.
    from server.memory.api.models_crud import MemoryTouchInput

    await ch.insert(
        "loom.memory",
        [{**_row("Tuesdays", time.time_ns() - 1_000_000), "expire_reason": "blacklist.sweep:p1"}],
    )
    await hm.handle_memory_touch(MemoryTouchInput(**_ID, memory_keys=[_KEY], ttl_seconds=600))
    touched = await _read_back(ch)
    assert touched is not None and touched.expire_reason == ""

    await ch.insert(
        "loom.memory",
        [{**_row("Tuesdays", time.time_ns()), "expire_reason": "blacklist.sweep:p1"}],
    )
    await _set_through_phases(ch, monkeypatch, sha="")
    written = await _read_back(ch)
    assert written is not None and written.value == "Thursdays" and written.expire_reason == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["memory.delete_version", "memory.delete"])
async def test_a_delete_of_a_row_still_holding_a_sweeps_marker_is_a_delete(
    ch: _StatefulCH, op: str
) -> None:
    # The row was restored from a sweep by an older build and never written since: the
    # delete's tombstone does not take the marker, so the set in flight keeps its row.
    head_row = {
        **_row("Tuesdays", time.time_ns() - 10_000_000_000),
        "expire_reason": "blacklist.sweep:p1",
    }
    w = await _delete_during_the_set(ch, op, head_row=head_row)
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"


@pytest.mark.asyncio
async def test_a_write_committed_between_the_check_and_the_tombstone_is_put_back(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain set takes no key lock: it commits after delete_version read the old head and
    # before its tombstone. The delete then finds it and writes it back above the tombstone,
    # as if the write had come first (the delete refuses).
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(time.time_ns() - 1_000_000)
    write_row = hm._write_tombstone_row

    async def set_lands_first(existing: Any, **kw: Any) -> None:
        await set_commit._set_plain_insert(w, ch)
        await write_row(existing, **kw)

    monkeypatch.setattr(hm, "_write_tombstone_row", set_lands_first)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is False and out.reason == "version_changed"
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"


@pytest.mark.asyncio
async def test_a_write_whose_fence_read_missed_a_conditional_delete_lifts_after_its_insert(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The set reads its fence, then the whole delete_version runs (the set is not in yet,
    # so nothing is put back), then the set inserts under the tombstone. Its read after the
    # insert finds a tombstone aimed at an older version and writes the row again above it,
    # as if the delete had come first.
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(head + 5_000_000)
    insert = set_commit.insert_memory_versions
    answers: list[Any] = []

    async def delete_then_insert(rows: list[dict[str, Any]], **kw: Any) -> int:
        if not answers:
            answers.append(
                await hm.handle_memory_delete_version(
                    MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
                )
            )
        return await insert(rows, **kw)

    monkeypatch.setattr(set_commit, "insert_memory_versions", delete_then_insert)
    await set_commit._set_plain_insert(w, ch)
    assert answers[0].tombstoned is True
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and got.version == w.version
    assert w.version > answers[0].version


@pytest.mark.asyncio
async def test_a_batch_keeps_a_row_already_above_the_tombstone_where_it_is(
    ch: _StatefulCH,
) -> None:
    # Two entries of one key straddle a delete: the earlier moves just above the
    # tombstone, the later keeps its version, so a delayed write drawn between them
    # stays under the batch's newest value.
    from server.memory.app.write_fence import lift_above_deletes

    delete_at = _FIXED_HEAD
    await ch.insert(
        "loom.memory",
        [
            {
                **_row("Tuesdays", delete_at),
                "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
            }
        ],
    )
    earlier, later = (
        _row("Wednesdays", delete_at - 1_000_000),
        _row("Fridays", delete_at + 1_000_000),
    )
    vectors = [{**_ID, "memory_key": _KEY, "version": int(r["version"])} for r in (earlier, later)]
    lifted = await lift_above_deletes("o", "n", [earlier, later], vectors, ch=ch)
    assert delete_at < earlier["version"] < delete_at + 1_000 + 1
    assert later["version"] == delete_at + 1_000_000 and list(lifted) == [
        (_KEY, delete_at - 1_000_000)
    ]
    assert [v["version"] for v in vectors] == [earlier["version"], later["version"]]
    await ch.insert("loom.memory", [earlier, later, _row("Thursdays", delete_at + 500_000)])
    got = await _read_back(ch)
    assert got is not None and got.value == "Fridays"


@pytest.mark.asyncio
async def test_a_plain_delete_drawn_above_the_tombstone_is_not_undone(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A set lands, then a plain memory.delete removes it, both after delete_version's check
    # and after it drew its tombstone's version: the plain delete removed the key, not this
    # one (found=False), and the key ends deleted.
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(time.time_ns() - 1_000_000)
    write_row = hm._write_tombstone_row

    landed: list[bool] = []

    async def set_then_delete_land_first(existing: Any, **kw: Any) -> None:
        # memory.delete writes its tombstone through this function too: only the first call,
        # delete_version's, lets the set and the plain delete land.
        if not landed:
            landed.append(True)
            await set_commit._set_plain_insert(w, ch)
            await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))
        await write_row(existing, **kw)

    monkeypatch.setattr(hm, "_write_tombstone_row", set_then_delete_land_first)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is False and out.found is False
    assert await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_plain_delete_below_the_tombstone_answers_not_found_and_restores_its_row(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A set lands and a plain memory.delete removes it after delete_version's check and
    # before it draws its tombstone's version: the plain delete came first, so the key was
    # already gone when this delete ran (found=False). Its tombstone is written back on top,
    # so a restore brings back the set's row.
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(time.time_ns() - 1_000_000)
    landed: list[bool] = []

    async def check_then_land(identity: Any, key: str, **kw: Any) -> Any:
        found = await get_by_key(identity, key, **kw)
        if not landed:
            landed.append(True)
            await set_commit._set_plain_insert(w, ch)
            await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))
        return found

    get_by_key = memory_repo.get_by_key
    monkeypatch.setattr(memory_repo, "get_by_key", check_then_land)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is False and out.found is False
    assert await _read_back(ch) is None
    # The key's head is the plain delete's tombstone again, so a restore brings back the row
    # it removed, not the one delete_version checked.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    monkeypatch.setattr(restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    restored = await restore_mod.handle_memory_restore(MemoryRestoreInput(**_ID, memory_key=_KEY))
    assert restored.restored is True
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"


@pytest.mark.asyncio
async def test_a_restored_key_does_not_carry_the_delete_version_marker(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The marker stays on delete_version's tombstone; the row memory.restore brings back,
    # and every version after it, drops it.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    monkeypatch.setattr(restore_mod, "get_client", lambda: ch)
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    out = await restore_mod.handle_memory_restore(MemoryRestoreInput(**_ID, memory_key=_KEY))
    assert out.restored is True
    got = await _read_back(ch)
    assert got is not None and not got.source_trace.startswith("memory.delete_version:")


@pytest.mark.asyncio
async def test_a_later_write_listed_but_not_read_leaves_the_check_owed(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The version list saw a later write that the row read then missed: the delete cannot
    # tell what the write was, so it answers 503 and retires nothing, and the check stays
    # owed for the delete_version_check worker.
    from server.core.errors import BackendUnavailableError

    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(time.time_ns() - 1_000_000)
    write_row = hm._write_tombstone_row

    async def set_lands_first(existing: Any, **kw: Any) -> None:
        await set_commit._set_plain_insert(w, ch)
        await write_row(existing, **kw)

    async def nothing(*_a: Any, **_k: Any) -> list[Any]:
        return []

    async def no_retire(*_a: Any, **_k: Any) -> None:
        raise AssertionError("nothing retires before the check runs")

    monkeypatch.setattr(hm, "_write_tombstone_row", set_lands_first)
    monkeypatch.setattr(hm, "_retire_derived", no_retire)
    monkeypatch.setattr(memory_repo, "fetch_version_rows", nothing)
    with pytest.raises(BackendUnavailableError):
        await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    assert [c["done"] for c in await _recorded_checks(ch)] == [0]


@pytest.mark.asyncio
async def test_a_batch_whose_fence_read_missed_a_conditional_delete_lands_with_its_vectors(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two entries of the key, a delete_version landing between the batch's fence read and
    # its insert: after the insert both entries move above the tombstone, in order, with
    # their vectors and result versions.
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.setup[:0] = ["SET enable_quantized_codec = 1", ddl("memory_embeddings")]
    for name in ("index_late_rows", "retire_vectors_for_keys"):
        monkeypatch.setattr(memory_repo, name, _noop)
    monkeypatch.setattr(bulk_set, "fence_high", _noop)
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    state = _BulkWriteState()
    for i, value in enumerate(("Wednesdays", "Thursdays")):
        v = head + 1_000 + i
        state.memory_rows.append({**_row(value, v), "memory_type": "semantic"})
        state.embedding_rows.append(
            memory_repo.build_embedding_row(
                **_ID,
                memory_key=_KEY,
                memory_type="semantic",
                content_excerpt=value,
                version=v,
                embedding=[0.0] * 3072,
                embedding_dim=3072,
                memory_key_embedding=[0.0] * 3072,
            )
        )
        state.results.append(_BulkSetResult(memory_key=_KEY, version=v))
    insert = memory_repo.insert_memory_versions
    answers: list[Any] = []

    async def delete_then_insert(rows: list[dict[str, Any]], **kw: Any) -> int:
        if not answers:
            answers.append(
                await hm.handle_memory_delete_version(
                    MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
                )
            )
        return await insert(rows, **kw)

    monkeypatch.setattr(memory_repo, "insert_memory_versions", delete_then_insert)
    await bulk_set._bulk_insert(ch, state)
    assert answers[0].tombstoned is True
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"
    first, second = (r.version for r in state.results)
    assert answers[0].version < first < second == got.version
    vectors = await ch.query_all(
        "SELECT content_excerpt, version FROM loom.memory_embeddings FINAL "
        "WHERE tombstoned_at IS NULL"
    )
    assert [(v["content_excerpt"], int(v["version"])) for v in vectors] == [("Thursdays", second)]


@pytest.mark.asyncio
async def test_an_embedded_set_whose_fence_read_missed_a_conditional_delete_keeps_its_vector(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.setup[:0] = ["SET enable_quantized_codec = 1", ddl("memory_embeddings")]
    monkeypatch.setattr(memory_repo, "index_late_rows", _noop)
    monkeypatch.setattr(set_commit._cd, "schedule_settle", lambda *_a, **_k: None)
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(head + 5_000_000)
    w.input = w.input.model_copy(update={"memory_type": "semantic"})
    w.memory_row["memory_type"] = "semantic"
    w.embedder = SimpleNamespace(dim=3072, model_name="m")
    w.excerpt, w.embedding = "Thursdays", [0.0] * 3072
    w.key_embedding, w.key_text = [0.0] * 3072, "deploy window"
    insert = set_commit.insert_memory_versions
    answers: list[Any] = []

    async def delete_then_insert(rows: list[dict[str, Any]], **kw: Any) -> int:
        if not answers:
            answers.append(
                await hm.handle_memory_delete_version(
                    MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
                )
            )
        return await insert(rows, **kw)

    monkeypatch.setattr(set_commit, "insert_memory_versions", delete_then_insert)
    verdict = SimpleNamespace(verdict="", peers=[], refined_peers=[])
    await set_commit._set_dual_write(w, ch, None, verdict)
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and got.version == w.version
    assert w.version > answers[0].version
    vectors = await ch.query_all(
        "SELECT version FROM loom.memory_embeddings FINAL WHERE tombstoned_at IS NULL"
    )
    assert w.version in [int(v["version"]) for v in vectors]


def test_no_caller_writes_a_delete_version_trace() -> None:
    import pydantic

    from server.memory.api.models_bulk import _BulkSetEntry

    with pytest.raises(pydantic.ValidationError, match="may not start with"):
        MemorySetInput(**_ID, memory_key=_KEY, value="v", source_trace="memory.delete_version:1")
    with pytest.raises(pydantic.ValidationError, match="may not start with"):
        _BulkSetEntry(memory_key=_KEY, value="v", source_trace="memory.delete_version:1")


@pytest.mark.asyncio
@pytest.mark.parametrize("other", ["memory.delete", "sweep"])
async def test_a_write_after_a_written_back_tombstone_lifts_over_it(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch, other: str
) -> None:
    # Another delete (or a sweep) removes the key after delete_version's check; a set that
    # began after it reads its fence before the tombstone lands and inserts after the
    # write back. The copy is delete_version's own tombstone, so the set lifts over it:
    # the key ends with the set's row (the other delete, delete_version, then the set).
    from server.memory.app.write_fence import lift_after_insert

    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    write: dict[str, Any] = {}
    get_by_key = memory_repo.get_by_key

    async def check_then_other_delete(identity: Any, key: str, **kw: Any) -> Any:
        found = await get_by_key(identity, key, **kw)
        if not write:
            write.update(_row("Fridays", 0))  # the plain delete reads through here too
            if other == "memory.delete":
                await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))
            else:
                swept = {
                    **_row("Tuesdays", time.time_ns()),
                    "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
                    "expire_reason": "blacklist.sweep:p1",
                }
                await ch.insert("loom.memory", [swept])
            write["version"] = time.time_ns()  # drawn now; its fence read sees nothing yet
        return found

    monkeypatch.setattr(memory_repo, "get_by_key", check_then_other_delete)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.found is False
    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    await ch.insert("loom.memory", [write])
    lifted = await lift_after_insert("o", "n", [write], [], ch=ch)
    assert lifted
    got = await _read_back(ch)
    assert got is not None and got.value == "Fridays"


@pytest.mark.asyncio
async def test_a_sweep_above_the_tombstone_answers_not_found_and_retires_nothing(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    write_row = hm._write_tombstone_row
    retired: list[Any] = []

    async def sweep_lands_above(existing: Any, **kw: Any) -> None:
        await write_row(existing, **kw)
        swept = {
            **_row("Tuesdays", time.time_ns()),
            "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
            "expire_reason": "blacklist.sweep:p1",
        }
        await ch.insert("loom.memory", [swept])

    async def retire(*_a: Any, **_k: Any) -> tuple[int, int]:
        retired.append(True)
        return 0, 0

    monkeypatch.setattr(hm, "_write_tombstone_row", sweep_lands_above)
    monkeypatch.setattr(hm, "_retire_derived", retire)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is False and out.found is False and retired == []


@pytest.mark.asyncio
async def test_a_write_the_sweep_caught_stays_under_it_after_the_copy(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sweep removes the key after delete_version's check; a write drawn before the sweep's
    # tombstone (one the sweep caught) lands after the copy is written back above the
    # tombstone. The sweep's own tombstone still sits above it, so neither fence lifts it.
    from server.memory.app.write_fence import lift_above_deletes, lift_after_insert

    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    caught: dict[str, Any] = {}
    get_by_key = memory_repo.get_by_key

    async def check_then_sweep(identity: Any, key: str, **kw: Any) -> Any:
        found = await get_by_key(identity, key, **kw)
        if not caught:
            swept_at = time.time_ns()
            caught.update(_row("leaked token", swept_at - 1))  # drawn before the sweep
            swept = {
                **_row("Tuesdays", swept_at),
                "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
                "expire_reason": "blacklist.sweep:p1",
            }
            await ch.insert("loom.memory", [swept])
        return found

    monkeypatch.setattr(memory_repo, "get_by_key", check_then_sweep)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.found is False
    # Above the sweep: delete_version's tombstone and the copy of the sweep's written above
    # it, both delete_version's own, with no sweep marker.
    above_sweep = await ch.query_all(
        "SELECT source_trace, expire_reason FROM loom.memory_versions "
        "WHERE tombstoned_at IS NOT NULL AND version > {v:UInt64} ORDER BY version",
        {"v": caught["version"] + 1},
    )
    expected = {"source_trace": f"memory.delete_version:{head}", "expire_reason": ""}
    assert above_sweep == [expected, expected]
    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    drawn = caught["version"]
    assert await lift_above_deletes("o", "n", [caught], [], ch=ch) == {}
    await ch.insert("loom.memory", [caught])
    assert await lift_after_insert("o", "n", [caught], [], ch=ch) == {}
    assert caught["version"] == drawn and await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_check_that_fails_after_the_tombstone_is_finished_by_the_worker(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain set commits between delete_version's check and its tombstone, and the read
    # after the tombstone fails. The delete answers 503 and retires nothing; its check
    # stays owed in loom.delete_version_checks, and the delete_version_check worker's pass
    # later writes the set's row back above the tombstone.
    from server.core.errors import BackendUnavailableError
    from server.memory.workers import delete_version_check
    from server.settings import settings

    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(time.time_ns() - 1_000_000)
    write_row = hm._write_tombstone_row
    retired: list[Any] = []

    async def set_lands_first(existing: Any, **kw: Any) -> None:
        await set_commit._set_plain_insert(w, ch)
        await write_row(existing, **kw)

    retire = hm._retire_derived

    async def count_retire(*a: Any, **k: Any) -> Any:
        retired.append(True)
        return await retire(*a, **k)

    monkeypatch.setattr(hm, "_write_tombstone_row", set_lands_first)
    monkeypatch.setattr(hm, "_retire_derived", count_retire)
    key_moved_on = hm._key_moved_on
    monkeypatch.setattr(hm, "_key_moved_on", _read_fails)
    with pytest.raises(BackendUnavailableError):
        await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    assert retired == [] and await _read_back(ch) is None
    assert [c["done"] for c in await _recorded_checks(ch)] == [0]

    monkeypatch.setattr(hm, "_key_moved_on", key_moved_on)
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    assert await delete_version_check.run_pass() == 1
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"
    assert [c["done"] for c in await _recorded_checks(ch)] == [1] and retired == []


@pytest.mark.asyncio
async def test_a_delete_that_cannot_record_its_check_deletes_nothing(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])

    async def record_fails(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("timeout")

    monkeypatch.setattr(memory_repo, "record_delete_check", record_fails)
    with pytest.raises(RuntimeError):
        await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    got = await _read_back(ch)
    assert got is not None and got.value == "Tuesdays"


@pytest.mark.asyncio
async def test_a_delete_that_stands_closes_its_check(ch: _StatefulCH) -> None:
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is True
    assert [(int(c["checked"]), c["done"]) for c in await _recorded_checks(ch)] == [(head, 1)]


def _chunk_rows(value: Any) -> list[dict[str, Any]]:
    """The chunk rows memory.set writes for a long semantic `value` of the key."""
    from server.memory.app import set_chunks

    chunks, _ = set_chunks.plan_set_chunks(value, "semantic")
    assert chunks, "the value is long enough to chunk"
    return [
        {
            "org": "o",
            "namespace": "n",
            "agent": _ID["agent"],
            "user_id": "-",
            "session_id": set_chunks.set_chunk_session_id(_KEY),
            "run_id": "-",
            "chunk_index": i,
            "content_sha256": set_chunks._chunk_sha(text),
            "content": text,
            "embedding": [0.0] * 3072,
            "embedding_model": "m",
            "embedding_dim": 3072,
            "parent_memory_key": _KEY,
            "created_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
            "tombstoned_at": None,
        }
        for i, (text, _s, _e) in enumerate(chunks)
    ]


async def _seed_long_value_with_chunks(
    ch: _StatefulCH,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """A long semantic head of the key with its chunks, and a newer value's chunks of the
    key beside them: the head's version, the head's chunk rows and the newer ones."""
    ch.setup[:0] = ["SET enable_quantized_codec = 1", ddl("memory_chunks")]
    head = time.time_ns() - 10_000_000_000
    removed_value = " ".join(f"old passage {i} about the deploy window." for i in range(400))
    newer_value = " ".join(f"new passage {i} about the release train." for i in range(400))
    await ch.insert("loom.memory", [{**_row(removed_value, head), "memory_type": "semantic"}])
    existing = await _read_back(ch)
    removed, newer = _chunk_rows(existing.value), _chunk_rows(newer_value)
    await ch.insert("loom.memory_chunks", removed + newer)
    return head, removed, newer


@pytest.mark.asyncio
async def test_delete_version_retires_only_the_removed_versions_chunks(
    ch: _StatefulCH,
) -> None:
    # A newer value's chunks of the key stay when a delete_version that stands retires
    # what the version it removed derived.
    head, _removed, newer = await _seed_long_value_with_chunks(ch)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is True
    live = await ch.query_all(
        "SELECT content FROM loom.memory_chunks FINAL WHERE tombstoned_at IS NULL"
    )
    assert sorted(r["content"] for r in live) == sorted(r["content"] for r in newer)


async def _owed_after_a_failed_check(ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch) -> int:
    """A delete_version of the key's head whose check fails after the tombstone, with a
    plain set committing between its version check and its tombstone: the 503, and the
    version it named."""
    from server.core.errors import BackendUnavailableError

    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(time.time_ns() - 1_000_000)
    write_row = hm._write_tombstone_row
    key_moved_on = hm._key_moved_on

    async def set_lands_first(existing: Any, **kw: Any) -> None:
        await set_commit._set_plain_insert(w, ch)
        await write_row(existing, **kw)

    with monkeypatch.context() as m:
        m.setattr(hm, "_write_tombstone_row", set_lands_first)
        m.setattr(hm, "_key_moved_on", _read_fails)
        with pytest.raises(BackendUnavailableError):
            await hm.handle_memory_delete_version(
                MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
            )
    assert hm._key_moved_on is key_moved_on
    assert await _read_back(ch) is None
    return head


@pytest.mark.asyncio
async def test_a_retry_after_the_503_runs_the_owed_check(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An SDK retries the 503: the retry finds the key deleted and the check owed, runs it,
    # and answers as the delete would have (the set after the version check moved the key
    # on), not found=false.
    head = await _owed_after_a_failed_check(ch, monkeypatch)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is False and out.reason == "version_changed"
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"
    assert [c["done"] for c in await _recorded_checks(ch)] == [1]


@pytest.mark.asyncio
async def test_running_a_finished_check_again_changes_nothing(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The check ran (the set's row is written back) but was not marked done: the worker
    # runs it again, which answers the same and writes no second copy.
    head = await _owed_after_a_failed_check(ch, monkeypatch)
    (check,) = await memory_repo.pending_delete_checks(older_than_seconds=0, limit=10)
    first = await hm.rerun_delete_check(check)
    await memory_repo.record_delete_check(check, done=False)  # the mark was lost
    rows_before = await ch.query_all("SELECT version FROM loom.memory_versions ORDER BY version")
    again = await hm.rerun_delete_check(check)
    assert first is not None and again is not None
    assert (first.tombstoned, first.reason) == (again.tombstoned, again.reason)
    assert (
        await ch.query_all("SELECT version FROM loom.memory_versions ORDER BY version")
        == rows_before
    )
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and head < got.version


@pytest.mark.asyncio
async def test_the_worker_closes_a_check_whose_tombstone_never_landed(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.workers import delete_version_check
    from server.settings import settings

    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])

    async def insert_fails(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("timeout")

    monkeypatch.setattr(hm, "_write_tombstone_row", insert_fails)
    with pytest.raises(RuntimeError):
        await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    assert await delete_version_check.run_pass() == 1
    assert [c["done"] for c in await _recorded_checks(ch)] == [1]
    got = await _read_back(ch)
    assert got is not None and got.value == "Tuesdays" and got.version == head


@pytest.mark.asyncio
async def test_the_worker_leaves_a_check_to_its_delete_during_the_grace(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.workers import delete_version_check
    from server.settings import settings

    await _owed_after_a_failed_check(ch, monkeypatch)
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 3600)
    assert await delete_version_check.run_pass() == 0
    assert [c["done"] for c in await _recorded_checks(ch)] == [0]
    assert await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_retry_after_the_503_of_a_delete_that_stands_retires_its_version(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The common 503: the check's read failed and no write raced the delete. The retry
    # answers tombstoned at the delete's own tombstone, retires the removed version's
    # chunks (not a newer value's) and closes the check.
    from server.core.errors import BackendUnavailableError

    head, removed, newer = await _seed_long_value_with_chunks(ch)

    with monkeypatch.context() as m:
        m.setattr(hm, "_key_moved_on", _read_fails)
        with pytest.raises(BackendUnavailableError):
            await hm.handle_memory_delete_version(
                MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
            )
    live = await ch.query_all(
        "SELECT content FROM loom.memory_chunks FINAL WHERE tombstoned_at IS NULL"
    )
    assert len(live) == len(removed) + len(newer)  # nothing retires before the check runs
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    (check,) = await _recorded_checks(ch)
    assert out.tombstoned is True and out.version == int(check["tombstone"])
    assert check["done"] == 1
    live = await ch.query_all(
        "SELECT content FROM loom.memory_chunks FINAL WHERE tombstoned_at IS NULL"
    )
    assert sorted(r["content"] for r in live) == sorted(r["content"] for r in newer)


@pytest.mark.asyncio
async def test_a_retry_whose_check_fails_again_answers_503_and_leaves_it_owed(
    ch: _StatefulCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import BackendUnavailableError

    head = await _owed_after_a_failed_check(ch, monkeypatch)

    monkeypatch.setattr(hm, "_key_moved_on", _read_fails)
    with pytest.raises(BackendUnavailableError):
        await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    assert [c["done"] for c in await _recorded_checks(ch)] == [0]
