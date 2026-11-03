"""A delete that commits while a memory.set is in flight, against the real loom.memory table
on clickhouse-local. The set drew its version before the delete (it embeds in between), so
the tombstone's version is the higher one; the set's insert takes a new version above the
tombstone, and its row is the one read back. The derived tables (vectors, chunks) are
absent: their best-effort retires log, and the row tombstone stands."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
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
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl, view

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

# A fixed version for the lift tests: the offset hashes the drawn version, so a fixed one
# keeps their expected versions the same on every run (a multiple of 1,000, as a clock that
# reports microseconds draws).
_FIXED_HEAD = 1_900_000_000_000_000_000
_ID = {"org": "o", "namespace": "n", "agent": "okf-sync", "user_id": "-", "session_id": "-"}
_MID = "00000000-0000-0000-0000-00000000000a"
_KEY = "deploy_window"


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


async def _recorded_checks(ch: KeptCH) -> list[dict[str, Any]]:
    """Every check delete_version recorded, done or not, oldest tombstone first."""
    return await ch.query_all(
        "SELECT checked, tombstone, done FROM loom.delete_version_checks FINAL ORDER BY tombstone"
    )


async def _times_out(*_a: Any, **_k: Any) -> Any:
    raise RuntimeError("timeout")


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> KeptCH:
    ch = KeptCH(
        tmp_path,
        ddl("memory"),
        ddl("memory_versions"),
        view("memory_versions_mv"),
        ddl("delete_version_checks"),
        ddl("namespaces"),
    )

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


async def _read_back(ch: KeptCH) -> Any:
    return await memory_repo.get_by_key(
        memory_repo.Identity(**_ID), _KEY, strong=True, scope=WHOLE_NAMESPACE, ch=ch
    )


async def _delete_during_the_set(
    ch: KeptCH, op: str, *, head_row: dict[str, Any] | None = None
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
async def test_a_set_in_flight_during_a_delete_keeps_its_row(ch: KeptCH, op: str) -> None:
    w = await _delete_during_the_set(ch, op)
    await set_commit._set_plain_insert(w, ch)
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and got.version == w.version


@pytest.mark.asyncio
async def test_without_the_lift_the_tombstone_hides_the_set(ch: KeptCH) -> None:
    # The race the lift closes: the same insert at the version drawn before the delete.
    w = await _delete_during_the_set(ch, "memory.delete_version")
    await ch.insert("loom.memory", [w.memory_row])
    assert await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_tombstone_older_than_the_write_leaves_its_version(ch: KeptCH) -> None:
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
async def test_a_conditional_set_in_flight_during_a_delete_is_refused(ch: KeptCH) -> None:
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
        "status": "tombstoned",  # as the sweep writes it
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
async def test_a_write_drawn_after_the_delete_keeps_winning(ch: KeptCH) -> None:
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
async def test_a_blacklist_sweeps_tombstone_keeps_the_write_under_it(ch: KeptCH) -> None:
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(head + 5_000_000)
    swept = {
        **_row("Tuesdays", time.time_ns()),
        "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
        "expire_reason": "blacklist.sweep:p1",
        "status": "tombstoned",  # as the sweep writes it
    }
    await ch.insert("loom.memory", [swept])
    drawn = w.version
    await set_commit._set_plain_insert(w, ch)
    assert w.version == drawn and await _read_back(ch) is None


@pytest.mark.asyncio
async def test_two_writes_lifted_over_one_delete_differ_under_clock_skew(
    ch: KeptCH,
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
    ch: KeptCH,
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
    ch: KeptCH,
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
async def test_a_write_drawn_after_the_delete_stays_above_a_lifted_one(ch: KeptCH) -> None:
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.create("SET enable_quantized_codec = 1", ddl("memory_embeddings"))
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # memory.set's embedded path builds its vector from the lifted version.

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.create("SET enable_quantized_codec = 1", ddl("memory_embeddings"))
    monkeypatch.setattr(memory_repo, "index_late_rows", _noop)
    w = await _delete_during_the_set(ch, "memory.delete")
    w.input = w.input.model_copy(update={"memory_type": "semantic"})
    w.memory_row["memory_type"] = "semantic"
    w.embedder = SimpleNamespace(dim=3072, model_name="m")
    w.excerpt, w.embedding = "Thursdays", [0.0] * 3072
    w.key_embedding, w.key_text = [0.0] * 3072, "deploy window"
    verdict = SimpleNamespace(verdict="", peers=[], refined_peers=[])
    await set_commit._set_dual_write(w, ch, SimpleNamespace(policy="manual"), verdict)

    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and got.version == w.version
    vectors = await ch.query_all(
        "SELECT version FROM loom.memory_embeddings FINAL WHERE tombstoned_at IS NULL"
    )
    assert [int(v["version"]) for v in vectors] == [w.version]


async def _set_through_phases(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch, *, sha: str, after_build: Any = None
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The row it checks was written after the set began: its version is drawn after the
    # check, so it lands above that row instead of beneath it.
    from server.core.hashing import served_content_sha256
    from server.memory.app import write

    ch.create("SET enable_quantized_codec = 1", ddl("memory_embeddings"))

    head = time.time_ns()
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    # The set's first draw is below the row it then checks; the draw after the check is not.
    draws = iter([head - 1, head + 1])
    monkeypatch.setattr(write, "next_version", lambda: next(draws))
    sha = served_content_sha256("Tuesdays")
    out = await _set_through_phases(ch, monkeypatch, sha=sha)
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and out.version == head + 1


@pytest.mark.asyncio
async def test_a_delete_after_a_conditional_sets_check_answers_409(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from server.core.hashing import served_content_sha256

    ch.create("SET enable_quantized_codec = 1", ddl("memory_embeddings"))

    await ch.insert("loom.memory", [_row("Tuesdays", time.time_ns() - 1_000_000)])
    sha = served_content_sha256("Tuesdays")

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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row an older build restored from a sweep still holds the marker; the next touch or
    # set of the key writes a version without it.
    from server.memory.api.models_crud import MemoryTouchInput

    ch.create("SET enable_quantized_codec = 1", ddl("memory_embeddings"))

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
    ch: KeptCH, op: str
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
    ch: KeptCH,
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
    # Run again (its done mark lost), the check answers the same and copies nothing twice.
    rows_before = await ch.query_all("SELECT version FROM loom.memory_versions ORDER BY version")
    (done,) = await _recorded_checks(ch)
    check = {**_ID, "memory_key": _KEY, "checked": done["checked"], "tombstone": done["tombstone"]}
    again = await hm.rerun_delete_check(check)
    assert again is not None and (again.tombstoned, again.found) == (False, False)
    assert (
        await ch.query_all("SELECT version FROM loom.memory_versions ORDER BY version")
        == rows_before
    )
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
@pytest.mark.parametrize(
    ("column", "changed"), [("tags", ["reviewed"]), ("expire_reason", "contract ends")]
)
async def test_a_plain_delete_of_a_same_value_write_is_copied_with_its_metadata(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch, column: str, changed: Any
) -> None:
    # After delete_version's check a set writes the same value with other metadata, and a
    # plain memory.delete removes it. The delete's own tombstone has the value but not the
    # metadata, so the plain delete's tombstone is copied on top: a restore brings it back.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(time.time_ns() - 1_000_000)
    w.memory_row = {**_row("Tuesdays", w.version), column: changed}
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
    monkeypatch.setattr(restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    restored = await restore_mod.handle_memory_restore(MemoryRestoreInput(**_ID, memory_key=_KEY))
    assert restored.restored is True
    got = await _read_back(ch)
    assert got is not None and got.value == "Tuesdays"
    assert (list(got.tags) if column == "tags" else got.expire_reason) == changed


@pytest.mark.asyncio
async def test_a_restored_key_does_not_carry_the_delete_version_marker(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two entries of the key, a delete_version landing between the batch's fence read and
    # its insert: after the insert both entries move above the tombstone, in order, with
    # their vectors and result versions.
    from server.memory.api.models_bulk import _BulkSetResult
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.create("SET enable_quantized_codec = 1", ddl("memory_embeddings"))
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:

    async def _noop(*_a: Any, **_k: Any) -> int:
        return 0

    ch.create("SET enable_quantized_codec = 1", ddl("memory_embeddings"))
    monkeypatch.setattr(memory_repo, "index_late_rows", _noop)
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
    await set_commit._set_dual_write(w, ch, SimpleNamespace(policy="manual"), verdict)
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch, other: str
) -> None:
    # A newer value lands and another delete (or a sweep) removes it after delete_version's
    # check; a set that began after it reads its fence before the tombstone lands and
    # inserts after the write back. The copy of the other tombstone is delete_version's
    # own, so the set lifts over it: the key ends with the set's row.
    from server.memory.app.write_fence import lift_after_insert

    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    write: dict[str, Any] = {}
    get_by_key = memory_repo.get_by_key

    async def check_then_other_delete(identity: Any, key: str, **kw: Any) -> Any:
        found = await get_by_key(identity, key, **kw)
        if not write:
            write.update(_row("Fridays", 0))  # the plain delete reads through here too
            await ch.insert("loom.memory", [_row("Wednesdays", time.time_ns())])
            if other == "memory.delete":
                await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))
            else:
                swept = {
                    **_row("Wednesdays", time.time_ns()),
                    "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
                    "expire_reason": "blacklist.sweep:p1",
                    "status": "tombstoned",  # as the sweep writes it
                }
                await ch.insert("loom.memory", [swept])
            write["version"] = time.time_ns()  # drawn now; its fence read sees nothing yet
        return found

    monkeypatch.setattr(memory_repo, "get_by_key", check_then_other_delete)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.found is False
    copies = await ch.query_all(
        "SELECT value FROM loom.memory_versions WHERE source_trace = {t:String} "
        "AND value != {v:String}",
        {"t": f"memory.delete_version:{head}", "v": json.dumps("Tuesdays")},
    )
    assert [json.loads(c["value"]) for c in copies] == ["Wednesdays"]
    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    await ch.insert("loom.memory", [write])
    lifted = await lift_after_insert("o", "n", [write], [], ch=ch)
    assert lifted
    got = await _read_back(ch)
    assert got is not None and got.value == "Fridays"


@pytest.mark.asyncio
async def test_a_sweep_above_the_tombstone_answers_not_found_and_retires_nothing(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
            "status": "tombstoned",  # as the sweep writes it
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
@pytest.mark.parametrize("remover", ["sweep", "dedup"])
async def test_a_by_id_tombstone_of_the_checked_row_is_not_copied(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch, remover: str
) -> None:
    # A sweep or a dedup tombstones the very row delete_version checked, after its check:
    # the delete's own tombstone already carries that row, its status and (under a sweep's
    # marker) its own expiry reason being the remover's stamps. Nothing is copied.
    head = time.time_ns() - 10_000_000_000
    checked_row = {**_row("Tuesdays", head), "expire_reason": "contract ends"}
    await ch.insert("loom.memory", [checked_row])
    get_by_key = memory_repo.get_by_key
    removed: list[bool] = []

    async def check_then_remove(identity: Any, key: str, **kw: Any) -> Any:
        found = await get_by_key(identity, key, **kw)
        if not removed:
            removed.append(True)
            by_id = {  # what tombstone_cascade_by_ids writes
                **checked_row,
                "version": time.time_ns(),
                "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
                "status": "tombstoned",
            }
            if remover == "sweep":
                by_id["expire_reason"] = "blacklist.sweep:p1"
            await ch.insert("loom.memory", [by_id])
        return found

    monkeypatch.setattr(memory_repo, "get_by_key", check_then_remove)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.found is False
    marked = await ch.query_all(
        "SELECT version FROM loom.memory_versions WHERE source_trace = {t:String}",
        {"t": f"memory.delete_version:{head}"},
    )
    assert len(marked) == 1  # the delete's own tombstone, no copy


@pytest.mark.asyncio
async def test_a_write_the_sweep_caught_stays_under_it(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A sweep removes the key after delete_version's check; a write drawn before the sweep's
    # tombstone (one the sweep caught) lands after the delete's tombstone. The sweep's own
    # tombstone still sits above it, so neither fence lifts it.
    from server.memory.app.write_fence import lift_above_deletes, lift_after_insert

    head = time.time_ns() - 10_000_000_000
    checked_row = _row("Tuesdays", head)
    await ch.insert("loom.memory", [checked_row])
    caught: dict[str, Any] = {}
    get_by_key = memory_repo.get_by_key

    async def check_then_sweep(identity: Any, key: str, **kw: Any) -> Any:
        found = await get_by_key(identity, key, **kw)
        if not caught:
            swept_at = time.time_ns()
            caught.update(_row("leaked token", swept_at - 1))  # drawn before the sweep
            swept = {  # the sweep's tombstone carries the row it removes
                **checked_row,
                "version": swept_at,
                "tombstoned_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
                "expire_reason": "blacklist.sweep:p1",
                "status": "tombstoned",  # as the sweep writes it
            }
            await ch.insert("loom.memory", [swept])
        return found

    monkeypatch.setattr(memory_repo, "get_by_key", check_then_sweep)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.found is False
    # Above the sweep: delete_version's own tombstone, with no sweep marker. The sweep
    # removed the row this delete checked, so that tombstone already carries it: no copy.
    above_sweep = await ch.query_all(
        "SELECT source_trace, expire_reason FROM loom.memory_versions "
        "WHERE tombstoned_at IS NOT NULL AND version > {v:UInt64} ORDER BY version",
        {"v": caught["version"] + 1},
    )
    expected = {"source_trace": f"memory.delete_version:{head}", "expire_reason": ""}
    assert above_sweep == [expected]
    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    drawn = caught["version"]
    assert await lift_above_deletes("o", "n", [caught], [], ch=ch) == {}
    await ch.insert("loom.memory", [caught])
    assert await lift_after_insert("o", "n", [caught], [], ch=ch) == {}
    assert caught["version"] == drawn and await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_check_that_fails_after_the_tombstone_is_finished_by_the_worker(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
    monkeypatch.setattr(hm, "_key_moved_on", _times_out)
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])

    from server.core.errors import BackendUnavailableError

    monkeypatch.setattr(memory_repo, "record_delete_check", _times_out)
    with pytest.raises(BackendUnavailableError):
        await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    got = await _read_back(ch)
    assert got is not None and got.value == "Tuesdays"


@pytest.mark.asyncio
async def test_a_delete_that_stands_closes_its_check(ch: KeptCH) -> None:
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is True
    assert [(int(c["checked"]), c["done"]) for c in await _recorded_checks(ch)] == [(head, 1)]


def _chunk_row_base() -> dict[str, Any]:
    from server.memory.app import set_chunks

    return {
        "org": "o",
        "namespace": "n",
        "agent": _ID["agent"],
        "user_id": "-",
        "session_id": set_chunks.set_chunk_session_id(_KEY),
        "run_id": "-",
        "embedding": [0.0] * 3072,
        "embedding_model": "m",
        "embedding_dim": 3072,
        "parent_memory_key": _KEY,
        "created_at": datetime.now(UTC).replace(tzinfo=None, microsecond=0),
        "tombstoned_at": None,
    }


def _chunk_rows(value: Any) -> list[dict[str, Any]]:
    """The chunk rows memory.set writes for a long semantic `value` of the key."""
    from server.memory.app import set_chunks

    chunks, _ = set_chunks.plan_set_chunks(value, "semantic", _KEY)
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
    ch: KeptCH,
) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """A long semantic head of the key with its chunks, and a newer value's chunks of the
    key beside them: the head's version, the head's chunk rows and the newer ones."""
    ch.create("SET enable_quantized_codec = 1", ddl("memory_chunks"))
    head = time.time_ns() - 10_000_000_000
    removed_value = " ".join(f"old passage {i} about the deploy window." for i in range(400))
    newer_value = " ".join(f"new passage {i} about the release train." for i in range(400))
    await ch.insert("loom.memory", [{**_row(removed_value, head), "memory_type": "semantic"}])
    existing = await _read_back(ch)
    removed, newer = _chunk_rows(existing.value), _chunk_rows(newer_value)
    await ch.insert("loom.memory_chunks", removed + newer)
    return head, removed, newer


@pytest.mark.asyncio
async def test_delete_version_retires_chunks_the_excerpt_rule_no_longer_writes(
    ch: KeptCH,
) -> None:
    # A dict whose compact JSON fits the excerpt is not chunked, but chunks of its indented
    # text written under another rule are still live: shared-scope search would serve them
    # after the delete, so the delete retires every chunk the value's passages hold.
    from server.memory.app import set_chunks
    from server.memory.app.chunk_projection import chunk_text

    ch.create("SET enable_quantized_codec = 1", ddl("memory_chunks"))
    head = time.time_ns() - 10_000_000_000
    value = {"steps": ["ok"] * 180}
    assert len(json.dumps(value)) <= 1100
    assert set_chunks.plan_set_chunks(value, "semantic", _KEY) == ([], 0)
    await ch.insert("loom.memory", [{**_row(value, head), "memory_type": "semantic"}])
    passages = chunk_text(set_chunks.human_text(value, "semantic"))
    assert len(passages) >= 2
    rows = [
        {
            **_chunk_row_base(),
            "chunk_index": i,
            "content": text,
            "content_sha256": set_chunks._chunk_sha(text),
        }
        for i, (text, _s, _e) in enumerate(passages)
    ]
    await ch.insert("loom.memory_chunks", rows)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is True
    live = await ch.query_all(
        "SELECT content FROM loom.memory_chunks FINAL WHERE tombstoned_at IS NULL"
    )
    assert live == []


_OPENING = " ".join(f"opening {i} of the deploy note." for i in range(18))


def _head_with_opening() -> str:
    """A long value whose first chunk is `_OPENING` whole: a paragraph break ends it."""
    tail = " ".join(f"later passage {i} about the release train." for i in range(300))
    return _OPENING + "\n\n" + tail


async def _live_chunk_contents(ch: KeptCH) -> list[str]:
    rows = await ch.query_all(
        "SELECT content FROM loom.memory_chunks FINAL WHERE tombstoned_at IS NULL"
    )
    return sorted(r["content"] for r in rows)


@pytest.mark.asyncio
async def test_a_short_versions_passage_shared_with_the_head_stays_live(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The removed version is too short to chunk, but its one passage is chunk 0 of the head
    # that lands after the delete's check: the delete retires that row with the version's
    # passages and puts it back for the head.
    from server.memory.app import set_chunks
    from server.memory.app.chunk_projection import chunk_text

    ch.create("SET enable_quantized_codec = 1", ddl("memory_chunks"))
    head_value = _head_with_opening()
    assert set_chunks.plan_set_chunks(_OPENING, "semantic", _KEY) == ([], 0)
    assert chunk_text(_OPENING)[0][0] == chunk_text(head_value)[0][0]
    old = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [{**_row(_OPENING, old), "memory_type": "semantic"}])
    rows = _chunk_rows(head_value)
    key_moved_on = hm._key_moved_on

    async def head_lands_after_the_check(existing: Any, **kw: Any) -> Any:
        answer = await key_moved_on(existing, **kw)
        row = {**_row(head_value, kw["tombstone"] + 1_000), "memory_type": "semantic"}
        await ch.insert("loom.memory", [row])
        await ch.insert("loom.memory_chunks", rows)
        return answer

    monkeypatch.setattr(hm, "_key_moved_on", head_lands_after_the_check)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=old)
    )
    assert out.tombstoned is True
    assert await _live_chunk_contents(ch) == sorted(r["content"] for r in rows)


_PAST = datetime(2020, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("other", "served"),
    [
        ({"session_id": "s2"}, True),
        ({"user_id": "u2"}, True),
        ({"user_id": ""}, True),
        ({"session_id": "s2", "expires_at": _PAST}, False),
        ({"session_id": "s2", "status": "superseded"}, False),
    ],
    ids=["other session", "other user", "empty user", "expired", "superseded"],
)
async def test_a_passage_another_row_of_the_key_serves_stays_live(
    ch: KeptCH, other: dict[str, Any], served: bool
) -> None:
    # A key's chunk rows are shared by every user, agent and session holding it: another
    # row search serves keeps a shared passage live when this row's version is deleted; an
    # expired or superseded one does not.
    ch.create("SET enable_quantized_codec = 1", ddl("memory_chunks"))
    head = time.time_ns() - 10_000_000_000
    removed_value = _OPENING + "\n\n" + " ".join(f"old tail {i} of the note." for i in range(300))
    other_value = _head_with_opening()
    await ch.insert("loom.memory", [{**_row(removed_value, head), "memory_type": "semantic"}])
    row = {
        **_row(other_value, head + 1_000_000_000),
        "memory_type": "semantic",
        "memory_id": "00000000-0000-0000-0000-00000000000b",
        **other,
    }
    await ch.insert("loom.memory", [row])
    removed, kept = _chunk_rows(removed_value), _chunk_rows(other_value)
    await ch.insert("loom.memory_chunks", removed + kept)
    shared = {r["content"] for r in removed} & {r["content"] for r in kept}
    assert shared, "the values share their opening chunk"
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is True
    expected = [r["content"] for r in kept]
    if not served:
        expected = [c for c in expected if c not in shared]
    assert await _live_chunk_contents(ch) == sorted(expected)


@pytest.mark.asyncio
async def test_delete_version_retires_only_the_removed_versions_chunks(
    ch: KeptCH,
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


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_state", ["live", "high", "deleted again"])
async def test_a_set_after_the_check_keeps_the_chunks_it_shares_with_the_removed_version(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch, newer_state: str
) -> None:
    # A newer value that keeps the removed value's opening passages lands after the
    # delete's check, before its chunks retire: its shared chunks stay live with the rest.
    # They stay retired when search serves none of it: a high row, or a key deleted again.
    ch.create("SET enable_quantized_codec = 1", ddl("memory_chunks"))
    head = time.time_ns() - 10_000_000_000
    opening = " ".join(f"old passage {i} about the deploy window." for i in range(60))
    removed_value = opening + " " + " ".join(f"old tail {i} of the note." for i in range(300))
    newer_value = opening + " " + " ".join(f"new tail {i} of the note." for i in range(300))
    await ch.insert("loom.memory", [{**_row(removed_value, head), "memory_type": "semantic"}])
    removed = _chunk_rows((await _read_back(ch)).value)
    await ch.insert("loom.memory_chunks", removed)
    key_moved_on = hm._key_moved_on
    newer: list[dict[str, Any]] = []

    async def set_lands_after_the_check(existing: Any, **kw: Any) -> Any:
        answer = await key_moved_on(existing, **kw)
        row = {**_row(newer_value, kw["tombstone"] + 1_000), "memory_type": "semantic"}
        if newer_state == "high":
            row["sensitivity"] = "high"
        await ch.insert("loom.memory", [row])
        newer.extend(_chunk_rows((await _read_back(ch)).value))
        await ch.insert("loom.memory_chunks", newer)
        if newer_state == "deleted again":
            gone = {**row, "version": row["version"] + 1_000, "tombstoned_at": row["updated_at"]}
            await ch.insert("loom.memory", [gone])
        return answer

    monkeypatch.setattr(hm, "_key_moved_on", set_lands_after_the_check)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is True
    shared = {r["content"] for r in removed} & {r["content"] for r in newer}
    assert shared, "the values share chunks"
    live = await ch.query_all(
        "SELECT content FROM loom.memory_chunks FINAL WHERE tombstoned_at IS NULL"
    )
    expected = [r["content"] for r in newer]
    if newer_state != "live":
        expected = [c for c in expected if c not in shared]
    assert sorted(r["content"] for r in live) == sorted(expected)


@pytest.mark.asyncio
async def test_a_delete_racing_the_chunk_restore_leaves_no_chunk_live(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A memory.delete of the newer value commits after the restore read the key and before
    # it put the shared chunks back: the read after the restore retires them again.
    from server.memory.app import set_chunks

    ch.create("SET enable_quantized_codec = 1", ddl("memory_chunks"))
    head = time.time_ns() - 10_000_000_000
    opening = " ".join(f"old passage {i} about the deploy window." for i in range(60))
    removed_value = opening + " " + " ".join(f"old tail {i} of the note." for i in range(300))
    newer_value = opening + " " + " ".join(f"new tail {i} of the note." for i in range(300))
    await ch.insert("loom.memory", [{**_row(removed_value, head), "memory_type": "semantic"}])
    await ch.insert("loom.memory_chunks", _chunk_rows((await _read_back(ch)).value))
    key_moved_on = hm._key_moved_on

    async def set_lands_after_the_check(existing: Any, **kw: Any) -> Any:
        answer = await key_moved_on(existing, **kw)
        row = {**_row(newer_value, kw["tombstone"] + 1_000), "memory_type": "semantic"}
        await ch.insert("loom.memory", [row])
        await ch.insert("loom.memory_chunks", _chunk_rows((await _read_back(ch)).value))
        return answer

    restore = set_chunks.restore_set_chunks

    async def delete_lands_first(**kw: Any) -> int:
        out = await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))
        assert out.tombstoned is True
        return await restore(**kw)

    monkeypatch.setattr(hm, "_key_moved_on", set_lands_after_the_check)
    monkeypatch.setattr(set_chunks, "restore_set_chunks", delete_lands_first)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is True
    live = await ch.query_all(
        "SELECT content FROM loom.memory_chunks FINAL WHERE tombstoned_at IS NULL"
    )
    assert live == []


@pytest.mark.asyncio
async def test_a_restore_whose_head_read_fails_retires_the_chunks_again(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The read after the restore fails: the restored chunks retire again rather than stay
    # live on a head nobody checked.
    ch.create("SET enable_quantized_codec = 1", ddl("memory_chunks"))
    head = time.time_ns() - 10_000_000_000
    opening = " ".join(f"old passage {i} about the deploy window." for i in range(60))
    removed_value = opening + " " + " ".join(f"old tail {i} of the note." for i in range(300))
    newer_value = opening + " " + " ".join(f"new tail {i} of the note." for i in range(300))
    await ch.insert("loom.memory", [{**_row(removed_value, head), "memory_type": "semantic"}])
    removed = _chunk_rows((await _read_back(ch)).value)
    await ch.insert("loom.memory_chunks", removed)
    key_moved_on = hm._key_moved_on
    newer: list[dict[str, Any]] = []

    async def set_lands_after_the_check(existing: Any, **kw: Any) -> Any:
        answer = await key_moved_on(existing, **kw)
        row = {**_row(newer_value, kw["tombstone"] + 1_000), "memory_type": "semantic"}
        await ch.insert("loom.memory", [row])
        newer.extend(_chunk_rows((await _read_back(ch)).value))
        await ch.insert("loom.memory_chunks", newer)
        return answer

    served = hm._served_chunks
    calls: list[bool] = []

    async def second_read_fails(*a: Any, **k: Any) -> Any:
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("timeout")
        return await served(*a, **k)

    monkeypatch.setattr(hm, "_key_moved_on", set_lands_after_the_check)
    monkeypatch.setattr(hm, "_served_chunks", second_read_fails)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is True and len(calls) == 2
    shared = {r["content"] for r in removed} & {r["content"] for r in newer}
    live = await ch.query_all(
        "SELECT content FROM loom.memory_chunks FINAL WHERE tombstoned_at IS NULL"
    )
    assert shared and not shared & {r["content"] for r in live}


async def _owed_after_a_failed_check(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch, *, clock_ahead_ns: int = 0
) -> int:
    """A delete_version of the key's head whose check fails after the tombstone, with a
    plain set committing between its version check and its tombstone: the 503, and the
    version it named. The delete draws its tombstone on a clock `clock_ahead_ns` ahead."""
    from server.core.errors import BackendUnavailableError

    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    w = _in_flight_set(time.time_ns() - 1_000_000)
    write_row = hm._write_tombstone_row

    async def set_lands_first(existing: Any, **kw: Any) -> None:
        await set_commit._set_plain_insert(w, ch)
        await write_row(existing, **kw)

    with monkeypatch.context() as m:
        m.setattr(hm, "_write_tombstone_row", set_lands_first)
        m.setattr(hm, "_key_moved_on", _times_out)
        m.setattr(hm, "next_version", lambda: time.time_ns() + clock_ahead_ns)
        with pytest.raises(BackendUnavailableError):
            await hm.handle_memory_delete_version(
                MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
            )
    assert await _read_back(ch) is None
    return head


@pytest.mark.asyncio
async def test_a_retry_after_the_503_runs_the_owed_check(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
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
async def test_recovery_on_a_clock_behind_the_tombstone_writes_the_set_above_it(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The delete ran on a host whose clock is a minute ahead; the worker's is not. The
    # set's row still goes back above the tombstone, not at the worker's clock below it.
    from server.memory.workers import delete_version_check
    from server.settings import settings

    await _owed_after_a_failed_check(ch, monkeypatch, clock_ahead_ns=60_000_000_000)
    (check,) = await _recorded_checks(ch)
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    assert await delete_version_check.run_pass() == 1
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"
    # Above the band the fence lifts writes into, so it cannot tie with a lifted write.
    from server.memory.app.write_fence import LIFT_BAND_NS

    assert got.version > int(check["tombstone"]) + LIFT_BAND_NS


async def _tombstone_held_back(ch: KeptCH, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Any]:
    """A delete_version of the key's head whose tombstone insert times out but is still
    queued: the version it named, and the call that commits the insert late."""
    head = time.time_ns() - 10_000_000_000
    await ch.insert("loom.memory", [_row("Tuesdays", head)])
    write_row = hm._write_tombstone_row
    queued: list[tuple[Any, dict[str, Any]]] = []

    async def times_out_queued(existing: Any, **kw: Any) -> None:
        queued.append((existing, kw))
        raise RuntimeError("timeout")

    async def commit_late() -> None:
        ((existing, kw),) = queued
        await write_row(existing, **kw)

    from server.core.errors import BackendUnavailableError

    with monkeypatch.context() as m:
        m.setattr(hm, "_write_tombstone_row", times_out_queued)
        with pytest.raises(BackendUnavailableError):
            await hm.handle_memory_delete_version(
                MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
            )
    return head, commit_late


@pytest.mark.asyncio
async def test_a_check_whose_tombstone_is_missing_finishes_the_delete(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The tombstone insert timed out (503). The worker finds no tombstone, writes one at a
    # new version (the namespace's watermark moves past the write it draws after) and runs
    # the check: the delete stands, and the original insert committing later changes
    # nothing.
    from server.memory.workers import delete_version_check
    from server.settings import settings

    _head, commit_late = await _tombstone_held_back(ch, monkeypatch)
    (check,) = await _recorded_checks(ch)
    await ch.insert("loom.memory", [{**_row("other", time.time_ns()), "memory_key": "other"}])
    before = await memory_repo.namespace_version("o", "n", ch=ch)
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    assert await delete_version_check.run_pass() == 1
    assert [c["done"] for c in await _recorded_checks(ch)] == [1]
    assert await _read_back(ch) is None
    assert await memory_repo.namespace_version("o", "n", ch=ch) > before
    await commit_late()
    assert await _read_back(ch) is None
    versions = await ch.query_all(
        "SELECT version FROM loom.memory_versions WHERE memory_key = {k:String} ORDER BY version",
        {"k": _KEY},
    )
    assert int(check["tombstone"]) in [int(v["version"]) for v in versions]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["the check record", "the tombstone insert", "the check after it", "a retry"]
)
async def test_a_503_writes_its_audit_row_and_the_worker_none(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    # The transport audits only a call that succeeds. A call that answers the deferred 503
    # writes its own row, outcome `deferred`, as the transport would name it; a retry that
    # answers it again is its own call with its own row. The worker that later finishes
    # the delete writes none.
    from server.core.errors import BackendUnavailableError
    from server.core.logging_config import request_id_var
    from server.memory.workers import delete_version_check
    from server.observability import audit
    from server.settings import settings

    emitted: list[tuple[str, dict[str, Any]]] = []

    async def record(event_type: str, **kw: Any) -> str:
        emitted.append((event_type, kw))
        return ""

    monkeypatch.setattr(audit, "emit", record)
    monkeypatch.setattr(hm, "current_auth", lambda: SimpleNamespace(subject="okf"))
    rid = request_id_var.set("rid-1")
    try:
        if failure == "the check record":
            head = time.time_ns() - 10_000_000_000
            await ch.insert("loom.memory", [_row("Tuesdays", head)])
            with monkeypatch.context() as m:
                m.setattr(memory_repo, "record_delete_check", _times_out)
                with pytest.raises(BackendUnavailableError):
                    await hm.handle_memory_delete_version(
                        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
                    )
        elif failure == "the tombstone insert":
            head, _commit_late = await _tombstone_held_back(ch, monkeypatch)
        else:
            head = await _owed_after_a_failed_check(ch, monkeypatch)
        if failure == "a retry":
            with monkeypatch.context() as m:
                m.setattr(hm, "_key_moved_on", _times_out)
                with pytest.raises(BackendUnavailableError):
                    await hm.handle_memory_delete_version(
                        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
                    )
    finally:
        request_id_var.reset(rid)
    calls = 2 if failure == "a retry" else 1
    assert len(emitted) == calls
    expected = MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head).model_dump(
        mode="json"
    )
    for event, row in emitted:
        assert event == "memory.delete_version"
        assert (row["actor"], row["request_id"]) == ("okf", "rid-1")
        assert row["subject"] == {**expected, "outcome": "deferred"}
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    await delete_version_check.run_pass()
    assert len(emitted) == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["a locked row", "an unread head"])
async def test_a_refusal_or_another_503_writes_no_deferred_audit_row(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    # Only a delete that may still finish writes the deferred row: a 423 on a locked row,
    # or a 503 before anything is recorded (the head unread), writes none.
    from fastapi import HTTPException

    from server.core.errors import BackendUnavailableError
    from server.observability import audit

    emitted: list[str] = []

    async def record(event_type: str, **_kw: Any) -> str:
        emitted.append(event_type)
        return ""

    monkeypatch.setattr(audit, "emit", record)
    head = time.time_ns() - 10_000_000_000
    row = _row("Tuesdays", head)
    if refusal == "a locked row":
        row = {**row, "lock_mode": "read_only", "lock_reason": "frozen"}
        await ch.insert("loom.memory", [row])
        with pytest.raises(HTTPException) as refused:
            await hm.handle_memory_delete_version(
                MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
            )
        assert refused.value.status_code == 423
    else:
        await ch.insert("loom.memory", [row])

        async def unread(*_a: Any, **_k: Any) -> Any:
            raise BackendUnavailableError("ClickHouse is unreachable")

        monkeypatch.setattr(memory_repo, "get_by_key", unread)
        with pytest.raises(BackendUnavailableError):
            await hm.handle_memory_delete_version(
                MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
            )
    assert emitted == []


@pytest.mark.asyncio
async def test_a_check_whose_tombstone_is_missing_leaves_a_later_set_alone(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The caller sets the key after the 503 of a failed tombstone insert. The worker finds
    # the key moved on: it writes no tombstone, and the set stays the head.
    from server.memory.workers import delete_version_check
    from server.settings import settings

    _head, _commit_late = await _tombstone_held_back(ch, monkeypatch)
    w = _in_flight_set(time.time_ns())
    await set_commit._set_plain_insert(w, ch)
    rows_before = await ch.query_all("SELECT version FROM loom.memory_versions ORDER BY version")
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    assert await delete_version_check.run_pass() == 1
    assert [c["done"] for c in await _recorded_checks(ch)] == [1]
    assert (
        await ch.query_all("SELECT version FROM loom.memory_versions ORDER BY version")
        == rows_before
    )
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays" and got.version == w.version


@pytest.mark.asyncio
async def test_a_set_under_a_late_tombstone_of_the_same_delete_is_written_back(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The SDK retries the 503 of a timed-out tombstone insert. While the retry writes its
    # own tombstone, a plain set drawn before the first commits, and then the first
    # tombstone commits above it. The retry's check looks past both of its tombstones: the
    # set is written back above them.
    head, commit_late = await _tombstone_held_back(ch, monkeypatch)
    (check,) = await _recorded_checks(ch)
    w = _in_flight_set(int(check["tombstone"]) - 1_000)
    write_row = hm._write_tombstone_row

    async def set_then_late_commit(existing: Any, **kw: Any) -> None:
        await set_commit._set_plain_insert(w, ch)
        await commit_late()
        await write_row(existing, **kw)

    monkeypatch.setattr(hm, "_write_tombstone_row", set_then_late_commit)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is False and out.reason == "version_changed"
    got = await _read_back(ch)
    tombstones = [int(c["tombstone"]) for c in await _recorded_checks(ch)]
    assert got is not None and got.value == "Thursdays" and got.version > max(tombstones)


@pytest.mark.asyncio
async def test_a_plain_delete_above_a_late_tombstone_of_the_same_delete_is_copied(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The SDK retries the 503 of a timed-out tombstone insert. After the retry's version
    # check the first tombstone commits, then a set and a plain memory.delete of it, all
    # below the retry's tombstone. The plain delete removed the key (found=False), and its
    # tombstone is copied on top, so a restore brings back the set's value.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore as restore_mod

    head, commit_late = await _tombstone_held_back(ch, monkeypatch)
    get_by_key = memory_repo.get_by_key
    landed: list[bool] = []

    async def check_then_land(identity: Any, key: str, **kw: Any) -> Any:
        found = await get_by_key(identity, key, **kw)
        if not landed:
            landed.append(True)
            await commit_late()
            await set_commit._set_plain_insert(_in_flight_set(time.time_ns()), ch)
            await hm.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key=_KEY))
        return found

    monkeypatch.setattr(memory_repo, "get_by_key", check_then_land)
    out = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert out.tombstoned is False and out.found is False
    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(restore_mod, "get_client", lambda: ch)
    restored = await restore_mod.handle_memory_restore(MemoryRestoreInput(**_ID, memory_key=_KEY))
    assert restored.restored is True
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"


@pytest.mark.asyncio
async def test_a_recovered_check_run_again_stands_on_the_tombstone_it_wrote(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The worker wrote the missing tombstone, but its done mark was lost: the next run
    # stands on that tombstone, answers the same and writes nothing.
    head, _commit_late = await _tombstone_held_back(ch, monkeypatch)
    (owed,) = await _recorded_checks(ch)
    check = {**_ID, "memory_key": _KEY, "checked": head, "tombstone": owed["tombstone"]}
    first = await hm.rerun_delete_check(check)
    assert first is not None and first.tombstoned is True
    assert first.version is not None and first.version > int(owed["tombstone"])
    await memory_repo.record_delete_check(check, done=False)  # the mark was lost
    rows_before = await ch.query_all("SELECT version FROM loom.memory_versions ORDER BY version")
    again = await hm.rerun_delete_check(check)
    assert again is not None and (again.tombstoned, again.version) == (True, first.version)
    assert (
        await ch.query_all("SELECT version FROM loom.memory_versions ORDER BY version")
        == rows_before
    )


@pytest.mark.asyncio
async def test_a_check_whose_tombstone_is_missing_finishes_on_the_retrys(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first tombstone insert failed for good; the retry deleted the key again. The first
    # check, run later, stands on the retry's tombstone, the one that committed.
    head, _commit_late = await _tombstone_held_back(ch, monkeypatch)
    retry = await hm.handle_memory_delete_version(
        MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
    )
    assert retry.tombstoned is True
    first = next(c for c in await _recorded_checks(ch) if c["done"] == 0)
    check = {**_ID, "memory_key": _KEY, "checked": head, "tombstone": first["tombstone"]}
    out = await hm.rerun_delete_check(check)
    assert out is not None and out.tombstoned is True and out.version == retry.version
    assert [c["done"] for c in await _recorded_checks(ch)] == [1, 1]


@pytest.mark.asyncio
async def test_a_retry_whose_first_tombstone_commits_late_still_retires(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The SDK retries the 503 of a timed-out tombstone insert; the original commits while
    # the retry runs its check. It is a delete of the same version, not another delete:
    # the retry and the worker's later run of the first check both stand and retire.
    from server.memory.workers import delete_version_check
    from server.settings import settings

    head, commit_late = await _tombstone_held_back(ch, monkeypatch)
    key_moved_on = hm._key_moved_on
    retire = hm._retire_derived
    retired: list[int] = []

    async def original_commits_first(existing: Any, **kw: Any) -> Any:
        await commit_late()
        return await key_moved_on(existing, **kw)

    async def count_retire(existing: Any, **kw: Any) -> Any:
        retired.append(kw["version"])
        return await retire(existing, **kw)

    monkeypatch.setattr(hm, "_retire_derived", count_retire)
    with monkeypatch.context() as m:
        m.setattr(hm, "_key_moved_on", original_commits_first)
        out = await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    assert out.tombstoned is True and retired == [out.version]
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    assert await delete_version_check.run_pass() == 1
    assert [c["done"] for c in await _recorded_checks(ch)] == [1, 1]
    assert len(retired) == 2 and await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_set_landing_while_a_missing_tombstone_is_written_stays(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain set commits between the worker's read and its tombstone write, below the
    # tombstone: the check after the tombstone writes the set back above it.
    from server.memory.workers import delete_version_check
    from server.settings import settings

    _head, _commit_late = await _tombstone_held_back(ch, monkeypatch)
    (check,) = await _recorded_checks(ch)
    w = _in_flight_set(int(check["tombstone"]) - 1_000)
    write_row = hm._write_tombstone_row

    async def set_lands_first(existing: Any, **kw: Any) -> None:
        await set_commit._set_plain_insert(w, ch)
        await write_row(existing, **kw)

    monkeypatch.setattr(hm, "_write_tombstone_row", set_lands_first)
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    assert await delete_version_check.run_pass() == 1
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"


@pytest.mark.asyncio
async def test_a_late_tombstone_does_not_hide_a_set_written_before_its_check_ran(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain set lands while the tombstone insert is still queued, at a version below it;
    # the worker writes the set above the tombstone before the insert commits.
    from server.memory.workers import delete_version_check
    from server.settings import settings

    _head, commit_late = await _tombstone_held_back(ch, monkeypatch)
    (check,) = await _recorded_checks(ch)
    await set_commit._set_plain_insert(_in_flight_set(int(check["tombstone"]) - 1_000), ch)
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    assert await delete_version_check.run_pass() == 1
    await commit_late()
    got = await _read_back(ch)
    assert got is not None and got.value == "Thursdays"


@pytest.mark.asyncio
async def test_the_worker_leaves_a_check_to_its_delete_during_the_grace(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.workers import delete_version_check
    from server.settings import settings

    await _owed_after_a_failed_check(ch, monkeypatch)
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 3600)
    idle: list[bool] = []
    monkeypatch.setattr(delete_version_check.pass_context, "mark_idle", lambda: idle.append(True))
    assert await delete_version_check.run_pass() == 0
    assert idle == [True]  # nothing it may run: the supervisor sleeps the idle interval
    assert [c["done"] for c in await _recorded_checks(ch)] == [0]
    assert await _read_back(ch) is None


@pytest.mark.asyncio
async def test_a_retry_after_the_503_of_a_delete_that_stands_retires_its_version(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The common 503: the check's read failed and no write raced the delete. The retry
    # answers tombstoned at the delete's own tombstone, retires the removed version's
    # chunks (not a newer value's) and closes the check.
    from server.core.errors import BackendUnavailableError

    head, removed, newer = await _seed_long_value_with_chunks(ch)

    with monkeypatch.context() as m:
        m.setattr(hm, "_key_moved_on", _times_out)
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
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import BackendUnavailableError

    head = await _owed_after_a_failed_check(ch, monkeypatch)

    monkeypatch.setattr(hm, "_key_moved_on", _times_out)
    with pytest.raises(BackendUnavailableError):
        await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**_ID, memory_key=_KEY, version=head)
        )
    assert [c["done"] for c in await _recorded_checks(ch)] == [0]


@pytest.mark.asyncio
async def test_a_worker_pass_runs_at_most_its_batch(
    ch: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two owed checks of two keys, a batch of one: each pass runs one.
    from server.memory.workers import delete_version_check
    from server.settings import settings

    for key in ("first", "second"):
        await memory_repo.record_delete_check(
            {**_ID, "memory_key": key, "checked": 1, "tombstone": 2},
            done=False,
        )
    monkeypatch.setattr(settings, "delete_version_check_grace_seconds", 0)
    monkeypatch.setattr(settings, "delete_version_check_batch_size", 1)
    assert await delete_version_check.run_pass() == 1
    assert sorted(c["done"] for c in await _recorded_checks(ch)) == [0, 1]
    assert await delete_version_check.run_pass() == 1
    assert [c["done"] for c in await _recorded_checks(ch)] == [1, 1]
