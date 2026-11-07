"""Decision records on clickhouse-local: a delete or a tombstone by id retiring the record,
the outcome's writer read and conditional write, and whole-record writes."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import decisions as svc
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl, view
from tests.unit.decision_inputs import record_input

_KEY = svc.decision_key("deploy", "Roll back or hotfix the image?")
_RECORD_ID = "00000000-0000-0000-0000-0000000000d1"
# The seeded record's schema id; nothing looks a schema up by it (the record plane uses its name).
_SCHEMA_ID = "00000000-0000-0000-0000-0000000000a1"


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> KeptCH:
    if CLICKHOUSE is None:
        pytest.skip("clickhouse binary not installed")
    from server.core import ch as ch_mod
    from server.memory.store import key_write_lock

    ch = KeptCH(
        tmp_path,
        *(ddl(t) for t in ("memory", "memory_versions", "delete_version_checks")),
        view("memory_versions_mv"),
        *(ddl(t) for t in ("memory_records", "memory_record_versions")),
        view("memory_record_versions_mv"),
        *(ddl(t) for t in ("memory_record_fields", "memory_record_conflicts")),
        *(ddl(t) for t in ("memory_schemas", "memory_schema_fields")),
        ddl("operator_edges"),
        *(ddl(t) for t in ("blacklist", "blacklist_version")),
    )

    @asynccontextmanager
    async def _no_lease(*_key: str) -> AsyncIterator[None]:
        yield

    # memory.delete_version holds the key's write lock, a lease this database does not keep.
    monkeypatch.setattr(key_write_lock.key_write_locks, "hold", _no_lease)
    monkeypatch.setattr(ch_mod, "_ch", ch)
    return ch


def _memory_row(agent: str, memory_type: str, version: int) -> dict[str, Any]:
    from server.memory.store.rows import build_first_version_row

    return build_first_version_row(
        {
            "org": "o",
            "namespace": "n",
            "agent": agent,
            "user_id": "-",
            "session_id": "-",
            "memory_key": _KEY,
            "memory_type": memory_type,
            "value": json.dumps({"outcome": "rollback"}),
            "value_json": "{}",
            "content_sha256": "",
            "actor": f"process:{agent}",
            "source_class": "asserted",
            "confidence": 1.0,
            "version": version,
        },
        now=datetime.now(UTC).replace(tzinfo=None, microsecond=0),
    )


def _set_into(ch: KeptCH, monkeypatch: pytest.MonkeyPatch, before: Any = None) -> list[Any]:
    """memory.set reduced to its `if_match_sha256` precondition (the real one) and its
    insert: the row lands under the input's identity with its value, above any version
    written so far. `before`, if given, runs first on each call. Returns the inputs."""
    from server.memory.app.write import _set_check_if_match

    seen: list[Any] = []

    async def _set(inp: Any) -> Any:
        seen.append(inp)
        if before is not None:
            await before(len(seen))
        await _set_check_if_match(SimpleNamespace(input=inp, existing=None))
        row = _memory_row(inp.agent, inp.memory_type, time.time_ns())
        row.update(
            memory_key=inp.memory_key,
            user_id=inp.user_id,
            session_id=inp.session_id,
            value=json.dumps(inp.value),
            tags=list(inp.tags),
        )
        await ch.insert("loom.memory", [row])
        return SimpleNamespace(version=row["version"])

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set)
    return seen


# The record `_write_decision_rows` seeds under _KEY: the four required fields.
_SEEDED = {
    "category": "deploy",
    "scenario": "Roll back or hotfix the image?",
    "reasoning": "it takes two minutes",
    "outcome": "rollback",
}


async def _write_decision_rows(
    ch: KeptCH,
    *agents: str,
    memory_type: str = "decision",
    updated_at: datetime | None = None,
    run_id: str = "",
) -> int:
    """One active record under _KEY, written by the first agent (under `run_id`, if given),
    and one live memory row under _KEY per agent. Returns the rows' version."""
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    version = time.time_ns() - 10_000_000_000
    await ch.insert(
        "loom.memory_records",
        [
            {
                "record_id": _RECORD_ID,
                "org": "o",
                "namespace": "n",
                "schema_id": _SCHEMA_ID,
                "schema_name": "decision",
                "memory_key": _KEY,
                "identity_agent": agents[0],
                "record_json": json.dumps({**_SEEDED, "run_id": run_id} if run_id else _SEEDED),
                "status": "active",
                "version": version,
                "created_at": now,
                "updated_at": updated_at or now,
            }
        ],
    )
    for agent in agents:
        await ch.insert("loom.memory", [_memory_row(agent, memory_type, version)])
    return version


async def _delete(agent: str, *, version: int | None = None, user_id: str = "-") -> None:
    from server.memory.api.models_crud import MemoryDeleteInput, MemoryDeleteVersionInput
    from server.memory.app import handlers as hm

    key = {"org": "o", "namespace": "n", "agent": agent, "user_id": user_id, "memory_key": _KEY}
    if version is None:
        out: Any = await hm.handle_memory_delete(MemoryDeleteInput(**key))
    else:
        out = await hm.handle_memory_delete_version(
            MemoryDeleteVersionInput(**key, version=version)
        )
    assert out.tombstoned is True


async def _history() -> list[str]:
    from server.memory.api.models_records import MemoryRecordHistoryInput
    from server.memory.app.records import handle_memory_record_history

    out = await handle_memory_record_history(
        MemoryRecordHistoryInput(org="o", namespace="n", record_id=_RECORD_ID)
    )
    return [r.status for r in out.rows]


async def _search_hits_the_key(_inp: Any) -> Any:
    return SimpleNamespace(
        results=[SimpleNamespace(memory_key=_KEY, similarity=0.9)], embedding_model="m"
    )


async def _seed_decision() -> Any:
    from server.memory.api.models_decision import DecisionChainInput
    from server.memory.app import decision as h

    out = await h.handle_decision_chain(DecisionChainInput(org="o", namespace="n", memory_key=_KEY))
    return out.seed_decision


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["memory.delete", "memory.delete_version"])
async def test_deleting_a_decision_row_tombstones_its_record(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    import server.memory.app.search as msearch
    from server.core.errors import NotFoundError
    from server.memory.api.models_decision import DecisionOutcomeInput, DecisionPrecedentsInput
    from server.memory.app import decision as h

    # The search leg is stubbed to hit the key, so precedents answers from the record alone.
    monkeypatch.setattr(msearch, "handle_memory_search", _search_hits_the_key)
    precedents_in = DecisionPrecedentsInput(org="o", namespace="n", query="image rollback")

    version = await _write_decision_rows(local, "claude")
    assert (await _seed_decision()).outcome == "rollback"
    served = await h.handle_decision_precedents(precedents_in)
    assert [p.memory_key for p in served.precedents] == [_KEY]

    await _delete("claude", version=version if op == "memory.delete_version" else None)

    assert await _seed_decision() is None
    assert (await h.handle_decision_precedents(precedents_in)).precedents == []
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="x")
        )
    assert await _history() == ["tombstoned", "active"]

    # Deciding the scenario again records it afresh: nothing active was superseded.
    _set_into(local, monkeypatch)
    again = await h.handle_decision_record(
        record_input(
            category="deploy",
            scenario="Roll back or hotfix the image?",
            outcome="hotfix",
            caused_by=[],
            influenced_by=[],
        )
    )
    assert again.memory_key == _KEY and again.superseded_prior is False
    assert (await _seed_decision()).outcome == "hotfix"


@pytest.mark.asyncio
async def test_a_delete_by_another_identity_leaves_the_writers_record(
    local: KeptCH,
) -> None:
    # claude wrote the record; the maintainer's row under the key is not its writer's.
    await _write_decision_rows(local, "claude", "maintainer")
    await _delete("maintainer")
    assert await _seed_decision() is not None
    assert await _history() == ["active"]
    await _delete("claude")
    assert await _seed_decision() is None
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_deleting_a_row_of_another_type_leaves_the_record(local: KeptCH) -> None:
    await _write_decision_rows(local, "claude", memory_type="semantic")
    await _delete("claude")
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_a_failed_record_tombstone_leaves_the_delete_standing(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo

    async def _fails(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("timeout")

    monkeypatch.setattr(memory_repo, "tombstone_decision_records", _fails)
    await _write_decision_rows(local, "claude")
    await _delete("claude")
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    assert await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE) is None
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_a_tombstone_by_id_retires_the_record(local: KeptCH) -> None:
    # The by-id funnel blacklist.add's sweep, conflicts.resolve and dedup_resolve go through.
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo

    await _write_decision_rows(local, "claude")
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    row = await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE)
    await memory_repo.tombstone_cascade_by_ids(
        "o", [row.memory_id], namespace="n", when=datetime.now(UTC)
    )
    assert await _seed_decision() is None
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_a_tombstone_by_id_of_another_identitys_row_leaves_the_record(local: KeptCH) -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo

    await _write_decision_rows(local, "claude", "maintainer")
    identity = memory_repo.Identity(org="o", namespace="n", agent="maintainer")
    row = await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE)
    await memory_repo.tombstone_cascade_by_ids(
        "o", [row.memory_id], namespace="n", when=datetime.now(UTC)
    )
    assert await _seed_decision() is not None
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_a_memory_apply_delete_retires_the_record(local: KeptCH) -> None:
    from server.memory.api.models_apply import ApplyTarget, MemoryApplyInput
    from server.memory.app.apply_ops import handle_memory_apply

    await _write_decision_rows(local, "claude")
    await handle_memory_apply(
        MemoryApplyInput(
            org="o",
            namespace="n",
            agent="claude",
            target=ApplyTarget(keys=[_KEY]),
            action="delete",
            reason="r",
            dry_run=False,
        )
    )
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_a_move_without_a_redirect_retires_the_record(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_decision import DecisionChainInput
    from server.memory.api.models_move import MemoryMoveInput
    from server.memory.app import decision as h
    from server.memory.app.move import handle_memory_move

    await _write_decision_rows(local, "claude")
    _set_into(local, monkeypatch)
    await handle_memory_move(
        MemoryMoveInput(
            org="o",
            namespace="n",
            agent="claude",
            from_key=_KEY,
            to_key="decision:moved",
            keep_redirect=False,
        )
    )
    assert await _history() == ["tombstoned", "active"]
    # The new key has no record; recording the decision again lands on its derived key.
    chain = await h.handle_decision_chain(
        DecisionChainInput(org="o", namespace="n", memory_key="decision:moved")
    )
    assert chain.seed_decision is None
    again = await h.handle_decision_record(
        record_input(
            category="deploy",
            scenario="Roll back or hotfix the image?",
            caused_by=[],
            influenced_by=[],
        )
    )
    assert again.memory_key == _KEY


@pytest.mark.asyncio
async def test_a_decision_row_written_above_the_tombstone_keeps_the_record(
    local: KeptCH,
) -> None:
    # The same identity wrote the key again after the delete's tombstone, before the
    # delete reached its record: the key holds a live decision, so the record stays.
    from server.memory import store as memory_repo

    version = await _write_decision_rows(local, "claude")
    tombstone = _memory_row("claude", "decision", version + 1)
    tombstone["tombstoned_at"] = tombstone["updated_at"]
    await local.insert("loom.memory", [tombstone, _memory_row("claude", "decision", version + 2)])
    await memory_repo.tombstone_decision_records("o", "n", [_KEY], version=version + 1)
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_a_refused_memory_row_leaves_no_active_record(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The memory row is written first: when memory.set refuses the write, the
    # record write is never reached, so no record says what the memory plane refused.
    from fastapi import HTTPException

    from server.memory.app import decision as h

    # memory.set's refusal of a locked key, as it raises it, reaches the caller unchanged.
    locked = {"code": "LOCKED", "message": "memory.set: the key is locked"}

    async def _refused(_inp: Any) -> Any:
        raise HTTPException(status_code=409, detail=locked)

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _refused)
    with pytest.raises(HTTPException) as refused:
        await h.handle_decision_record(record_input(caused_by=[], influenced_by=[]))
    assert refused.value.detail == locked
    key = svc.decision_key("Deploy", "Roll back or hotfix the 26.5 image?")
    assert await h._decision_records(local, "o", "n", [key]) == {}
    assert await local.query_all("SELECT 1 FROM loom.memory_records") == []


@pytest.mark.asyncio
async def test_a_supersede_drawn_below_the_active_version_still_wins(local: KeptCH) -> None:
    # A slow clock on the superseding replica: its version is under the active row's.
    from server.memory import store as memory_repo

    version = await _write_decision_rows(local, "claude")
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await memory_repo.supersede_active_record(
        "o", "n", "decision", _KEY, close_at=now, version=version - 1_000, now=now
    )
    rows = await local.query_all(
        "SELECT status FROM loom.memory_records FINAL WHERE record_id = {rid:UUID}",
        {"rid": _RECORD_ID},
    )
    assert rows == [{"status": "superseded"}]


@pytest.mark.asyncio
async def test_the_writers_delete_retires_the_record_while_another_row_holds_the_key(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record is the maintainer's text; claude's row under the key was superseded in
    # the record plane. The maintainer's delete retires the record, and no outcome copies
    # it into claude's row.
    import server.memory.app.search as msearch
    from server.core.errors import NotFoundError
    from server.memory.api.models_decision import DecisionOutcomeInput, DecisionPrecedentsInput
    from server.memory.app import decision as h

    monkeypatch.setattr(msearch, "handle_memory_search", _search_hits_the_key)
    await _write_decision_rows(local, "maintainer", "claude")
    await _delete("maintainer")
    assert await _seed_decision() is None
    precedents = await h.handle_decision_precedents(
        DecisionPrecedentsInput(org="o", namespace="n", query="image rollback")
    )
    assert precedents.precedents == []
    seen = _set_into(local, monkeypatch)
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    assert seen == []
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_the_writers_identity_includes_its_user(local: KeptCH) -> None:
    # Same agent, another user: that row is not the writer's, so its delete keeps the record.
    version = await _write_decision_rows(local, "claude")
    other_user = _memory_row("claude", "decision", version)
    other_user["user_id"] = "u2"
    await local.insert("loom.memory", [other_user])
    await _delete("claude", user_id="u2")
    assert await _history() == ["active"]
    await _delete("claude")
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_the_tombstone_copies_the_active_record(local: KeptCH) -> None:
    version = await _write_decision_rows(local, "claude")
    await _delete("claude")
    active, tombstoned = await local.query_all(
        "SELECT * FROM loom.memory_record_versions WHERE record_id = {rid:UUID} ORDER BY version",
        {"rid": _RECORD_ID},
    )
    changed = {"status", "tombstoned_at", "valid_to", "version", "updated_at"}
    for column in active.keys() - changed:
        assert tombstoned[column] == active[column], column
    assert (active["status"], tombstoned["status"]) == ("active", "tombstoned")
    assert active["tombstoned_at"] is None and tombstoned["tombstoned_at"] is not None
    assert active["valid_to"] is None and tombstoned["valid_to"] is not None
    assert int(tombstoned["version"]) > int(active["version"]) == version


@pytest.mark.asyncio
async def test_a_tombstone_from_a_slow_clock_still_reads_newest(local: KeptCH) -> None:
    # The active version was written an hour ahead of the deleting replica's clock.
    ahead = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(hours=1)
    await _write_decision_rows(local, "claude", updated_at=ahead)
    await _delete("claude")
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_an_outcome_with_no_live_decision_row_is_not_found(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record is still active (its tombstone never ran) but no row holds the key.
    from server.core.errors import NotFoundError
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    version = await _write_decision_rows(local, "claude")
    tombstone = _memory_row("claude", "decision", version + 1)
    tombstone["tombstoned_at"] = tombstone["updated_at"]
    await local.insert("loom.memory", [tombstone])
    seen = _set_into(local, monkeypatch)
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="x")
        )
    assert seen == []
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_an_outcome_revises_the_record_writers_row_while_it_is_live(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # claude wrote the key after the maintainer, but the record is the maintainer's.
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    await _write_decision_rows(local, "maintainer")
    await local.insert("loom.memory", [_memory_row("claude", "decision", time.time_ns())])
    seen = _set_into(local, monkeypatch)
    await h.handle_decision_outcome(
        DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
    )
    assert [i.agent for i in seen] == ["maintainer"]
    # The revision is the key's active record, and the record it replaced is superseded.
    rows = await local.query_all(
        "SELECT record_json FROM loom.memory_records FINAL "
        "WHERE memory_key = {key:String} AND status = 'active'",
        {"key": _KEY},
    )
    assert [json.loads(r["record_json"])["realized_outcome"] for r in rows] == ["ok"]
    assert await _history() == ["superseded", "active"]


@pytest.mark.asyncio
async def test_a_delete_between_the_row_and_the_record_leaves_the_new_record_tombstoned(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # decision.record writes its row, the same identity deletes it before the record
    # lands, so that delete finds no record to retire; the write retires it itself.
    from server.memory.app import decision as h

    async def _set_then_delete(inp: Any) -> Any:
        await local.insert("loom.memory", [_memory_row(inp.agent, inp.memory_type, time.time_ns())])
        await _delete(inp.agent)
        return SimpleNamespace(version=0)

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set_then_delete)
    out = await h.handle_decision_record(
        record_input(
            category="deploy",
            scenario="Roll back or hotfix the image?",
            caused_by=[],
            influenced_by=[],
        )
    )
    assert await _seed_decision() is None
    rows = await local.query_all(
        "SELECT status FROM loom.memory_records FINAL WHERE record_id = {rid:UUID}",
        {"rid": out.record_id},
    )
    assert rows == [{"status": "tombstoned"}]


@pytest.mark.asyncio
async def test_history_hides_active_and_superseded_rows_with_tombstoned_at_set(
    local: KeptCH,
) -> None:
    # A mirror row with tombstoned_at set and another status stays out of history; the
    # decision delete's tombstoned version is served.
    await _write_decision_rows(local, "claude")
    await _delete("claude")
    (active,) = await local.query_all(
        "SELECT * FROM loom.memory_record_versions WHERE record_id = {rid:UUID} "
        "AND status = 'active'",
        {"rid": _RECORD_ID},
    )
    erased = []
    for status, bump in (("active", 10), ("superseded", 20)):
        row = {k: v for k, v in active.items() if v is not None}
        row.update(status=status, version=int(active["version"]) + bump)
        row["tombstoned_at"] = row["updated_at"]
        erased.append(row)
    await local.insert("loom.memory_record_versions", erased)
    assert await _history() == ["tombstoned", "active"]


@pytest.mark.asyncio
async def test_an_outcome_goes_on_the_writers_exact_identity(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A newer live row differs from the record's writer only by session: not the writer's.
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    version = await _write_decision_rows(local, "claude")
    other_session = _memory_row("claude", "decision", version + 1)
    other_session["session_id"] = "s2"
    await local.insert("loom.memory", [other_session])
    seen = _set_into(local, monkeypatch)
    await h.handle_decision_outcome(
        DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
    )
    assert [(i.agent, i.session_id) for i in seen] == [("claude", "-")]


@pytest.mark.asyncio
async def test_record_query_rows_carry_their_status(local: KeptCH) -> None:
    from server.memory.api.models_records import MemoryRecordQueryInput
    from server.memory.app.records import handle_memory_record_query

    await _write_decision_rows(local, "claude")
    out = await handle_memory_record_query(
        MemoryRecordQueryInput(org="o", namespace="n", schema_name="decision")
    )
    assert [(r.record_id, r.status) for r in out.rows] == [(_RECORD_ID, "active")]


@pytest.mark.asyncio
async def test_a_second_closing_write_keeps_the_record_tombstoned(local: KeptCH) -> None:
    from server.memory import store as memory_repo

    version = await _write_decision_rows(local, "claude")
    await _delete("claude")
    await memory_repo.tombstone_decision_records("o", "n", [_KEY], version=version + 1)
    rows = await local.query_all(
        "SELECT status FROM loom.memory_records FINAL WHERE record_id = {rid:UUID}",
        {"rid": _RECORD_ID},
    )
    assert rows == [{"status": "tombstoned"}]
    assert await _history() == ["tombstoned", "active"]  # the second call wrote nothing


@pytest.mark.asyncio
async def test_a_new_writers_decision_keeps_nothing_of_the_earlier_writers(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # claude records and learns the outcome; the maintainer then decides the same scenario
    # with no alternatives, confidence or outcome. None of claude's fields may live on in
    # the maintainer's record, where claude's delete or erasure would not reach them.
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    _set_into(local, monkeypatch)
    scenario = {"category": "deploy", "scenario": "Roll back or hotfix the image?"}
    await h.handle_decision_record(record_input(**scenario, caused_by=[], influenced_by=[]))
    await h.handle_decision_outcome(
        DecisionOutcomeInput(
            org="o", namespace="n", memory_key=_KEY, realized_outcome="took 90s", success=True
        )
    )
    restated = await h.handle_decision_record(
        record_input(
            **scenario,
            agent="maintainer",
            run_id="-",
            outcome="hotfix",
            alternatives=[],
            confidence=0.0,
            caused_by=[],
            influenced_by=[],
        )
    )
    rows = await local.query_all(
        "SELECT record_json FROM loom.memory_records FINAL "
        "WHERE memory_key = {key:String} AND status = 'active'",
        {"key": _KEY},
    )
    (record,) = [json.loads(r["record_json"]) for r in rows]
    assert record == {
        "category": "deploy",
        "scenario": "Roll back or hotfix the image?",
        "reasoning": "Rollback is 2 minutes; the fix is untested.",
        "outcome": "hotfix",
        "decision_maker": "maintainer",
        **dict.fromkeys(("alternatives", "confidence", "realized_outcome", "success", "run_id")),
    }
    states = await local.query_all(
        "SELECT field_name, value_state FROM loom.memory_record_fields FINAL "
        "WHERE record_id = {rid:UUID}",
        {"rid": restated.record_id},
    )
    by_field = {r["field_name"]: r["value_state"] for r in states}
    for field in ("realized_outcome", "success", "alternatives", "confidence", "run_id"):
        assert by_field[field] == "absent", field

    # The changed scalar is still recorded as a conflict between the two records.
    conflicts = await local.query_all(
        "SELECT prior_value, incoming_value FROM loom.memory_record_conflicts FINAL "
        "WHERE memory_key = {key:String} AND field_name = 'outcome'",
        {"key": _KEY},
    )
    assert conflicts == [{"prior_value": '"rollback"', "incoming_value": '"hotfix"'}]

    await _delete("claude")
    seed = await _seed_decision()
    assert seed is not None and seed.outcome == "hotfix"
    assert seed.realized_outcome == "" and seed.success is None and seed.alternatives == []


@pytest.mark.asyncio
async def test_a_delete_after_the_outcome_read_refuses_the_outcome(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The writer deletes its row after decision.outcome read it. The precondition finds no
    # row with the hash the outcome read, the second read finds no row at all, and the
    # outcome answers not-found instead of writing the deleted row and its record back.
    from server.core.errors import NotFoundError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await _write_decision_rows(local, "claude")

    async def _delete_first(call: int) -> None:
        if call == 1:
            await _delete("claude")

    seen = _set_into(local, monkeypatch, before=_delete_first)
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="x")
        )
    assert len(seen) == 1 and seen[0].if_match_sha256
    assert await _history() == ["tombstoned", "active"]
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    assert await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE) is None


@pytest.mark.asyncio
async def test_an_outcome_after_a_re_record_answers_a_conflict(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The writer records the decision again between the outcome's read and its write: the
    # outcome writes nothing over the re-record and asks to be called again.
    from server.core.errors import PreconditionFailedError
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    await _write_decision_rows(local, "claude")

    async def _re_record_first(call: int) -> None:
        if call == 1:
            await h.handle_decision_record(
                record_input(
                    category="deploy",
                    scenario="Roll back or hotfix the image?",
                    reasoning="the new image is already canaried",
                    caused_by=[],
                    influenced_by=[],
                )
            )

    seen = _set_into(local, monkeypatch, before=_re_record_first)
    with pytest.raises(PreconditionFailedError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    # Call 1 is the refused write, call 2 the re-record.
    assert len(seen) == 2
    seed = await _seed_decision()
    assert seed is not None and seed.realized_outcome == ""
    assert seed.reasoning == "the new image is already canaried"


@pytest.mark.asyncio
async def test_an_outcome_after_a_same_text_re_record_answers_a_conflict(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A re-record under another run writes the same statement, so the row's hash does not
    # move. Landing inside the outcome's row write, it is caught before the record: the
    # record stays the re-record's, and the row holds the outcome's statement until a retry.
    from server.core.errors import PreconditionFailedError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    scenario = {"category": "deploy", "scenario": "Roll back or hotfix the image?"}
    re_recorded = []

    async def _re_record_inside_the_outcome(_call: int) -> None:
        if "decision:outcome-known" in seen[-1].tags and not re_recorded:
            re_recorded.append(True)
            await h.handle_decision_record(
                record_input(**scenario, run_id="r2", caused_by=[], influenced_by=[])
            )

    seen = _set_into(local, monkeypatch, before=_re_record_inside_the_outcome)
    await h.handle_decision_record(record_input(**scenario, caused_by=[], influenced_by=[]))
    with pytest.raises(PreconditionFailedError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    seed = await _seed_decision()
    assert seed is not None and seed.run_id == "r2" and seed.realized_outcome == ""
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    row = await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE)
    assert row is not None and row.value.endswith("Realized: ok.")


@pytest.mark.asyncio
async def test_an_outcome_after_a_re_record_before_its_write_writes_nothing(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The re-record lands between the outcome's read and its row write: the outcome is
    # refused before it writes anything.
    from server.core.errors import PreconditionFailedError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    scenario = {"category": "deploy", "scenario": "Roll back or hotfix the image?"}
    _set_into(local, monkeypatch)
    await h.handle_decision_record(record_input(**scenario, caused_by=[], influenced_by=[]))
    checked = h._checked_statement

    async def _re_record_then_check(org: str, namespace: str, payload: Any, tags: Any) -> Any:
        if payload.get("realized_outcome"):
            await h.handle_decision_record(
                record_input(**scenario, run_id="r2", caused_by=[], influenced_by=[])
            )
        return await checked(org, namespace, payload, tags)

    monkeypatch.setattr(h, "_checked_statement", _re_record_then_check)
    with pytest.raises(PreconditionFailedError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    row = await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE)
    assert row is not None and "Realized" not in row.value
    assert (await _seed_decision()).run_id == "r2"


@pytest.mark.asyncio
async def test_a_delete_before_the_outcome_writes_answers_not_found(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The writer deletes its row after the outcome read it and before the outcome writes.
    from server.core.errors import NotFoundError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    _set_into(local, monkeypatch)
    await h.handle_decision_record(
        record_input(
            category="deploy",
            scenario="Roll back or hotfix the image?",
            caused_by=[],
            influenced_by=[],
        )
    )
    checked = h._checked_statement

    async def _delete_then_check(org: str, namespace: str, payload: Any, tags: Any) -> Any:
        if payload.get("realized_outcome"):
            await _delete("claude")
        return await checked(org, namespace, payload, tags)

    monkeypatch.setattr(h, "_checked_statement", _delete_then_check)
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    assert await _seed_decision() is None
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    assert await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE) is None


@pytest.mark.asyncio
async def test_a_delete_after_the_outcomes_row_write_answers_not_found(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The delete lands after the outcome's row write and before its record write: the
    # record check finds no live writer, so the outcome writes no record back.
    from server.core.errors import NotFoundError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h
    from server.memory.app import write

    _set_into(local, monkeypatch)
    write_row = write.handle_memory_set

    async def _write_then_delete(inp: Any) -> Any:
        out = await write_row(inp)
        if "decision:outcome-known" in inp.tags:
            await _delete("claude")
        return out

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _write_then_delete)
    await h.handle_decision_record(
        record_input(
            category="deploy",
            scenario="Roll back or hotfix the image?",
            caused_by=[],
            influenced_by=[],
        )
    )
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    assert await _seed_decision() is None
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    assert await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE) is None


@pytest.mark.asyncio
async def test_an_outcome_whose_row_changed_answers_a_conflict(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import PreconditionFailedError
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    version = await _write_decision_rows(local, "claude")

    async def _rewrite(_call: int) -> None:
        row = _memory_row("claude", "decision", version + 1)
        row["value"] = json.dumps("another outcome's statement")
        await local.insert("loom.memory", [row])

    seen = _set_into(local, monkeypatch, before=_rewrite)
    with pytest.raises(PreconditionFailedError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    assert len(seen) == 1
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_an_outcome_without_success_keeps_the_verdict(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    _set_into(local, monkeypatch)
    scenario = {"category": "deploy", "scenario": "Roll back or hotfix the image?"}
    await h.handle_decision_record(record_input(**scenario, caused_by=[], influenced_by=[]))
    for success in (True, None):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(
                org="o", namespace="n", memory_key=_KEY, realized_outcome="ok", success=success
            )
        )
    seed = await _seed_decision()
    assert seed is not None and seed.realized_outcome == "ok" and seed.success is True


@pytest.mark.asyncio
async def test_an_outcome_keeps_nothing_of_another_active_record(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record write's own read of the active record answers with another writer's
    # record (a lagging replica, or a concurrent decision.record). The revision is built
    # from the writer's record alone, so none of the other record's fields reach it.
    import server.memory.app.records as hrec
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    await _write_decision_rows(local, "claude")
    other = {"reasoning": "the other writer's reason", "alternatives": "wait", "run_id": "r9"}

    async def _other_record(*_a: Any, **_k: Any) -> Any:
        from server.memory.store import ActiveRecord

        return ActiveRecord("00000000-0000-0000-0000-0000000000e1", None, json.dumps(other))

    monkeypatch.setattr(hrec, "active_record", _other_record)
    _set_into(local, monkeypatch)
    await h.handle_decision_outcome(
        DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
    )
    rows = await local.query_all(
        "SELECT record_json FROM loom.memory_records FINAL "
        "WHERE memory_key = {key:String} AND status = 'active'",
        {"key": _KEY},
    )
    (record,) = [json.loads(r["record_json"]) for r in rows]
    absent = ("alternatives", "confidence", "decision_maker", "run_id", "success")
    assert record == {**_SEEDED, "realized_outcome": "ok", **dict.fromkeys(absent)}


@pytest.mark.asyncio
async def test_a_decision_record_write_keeps_nothing_of_the_prior(local: KeptCH) -> None:
    # memory.record.write on the decision schema states the record whole, as decision.record does.
    from server.memory.api.models_records import MemoryRecordWriteInput
    from server.memory.app import decision as h
    from server.memory.app.records import handle_memory_record_write

    await h.ensure_schema("o", "n")

    def _write(**fields: Any) -> MemoryRecordWriteInput:
        values = {"category": "deploy", "scenario": "Roll back?", "outcome": "rollback", **fields}
        return MemoryRecordWriteInput(
            org="o",
            namespace="n",
            schema_name="decision",
            memory_key=_KEY,
            agent="claude",
            fields=svc.record_fields(values),  # type: ignore[arg-type]
        )

    await handle_memory_record_write(_write(reasoning="fast"))
    out = await handle_memory_record_write(_write())
    assert out.superseded_record_id
    assert out.fold["filled_from_prior"] == []
    # Read back: the new record holds no `reasoning`, in its JSON or as a carried field row.
    (stored,) = await local.query_all(
        "SELECT record_json FROM loom.memory_records FINAL WHERE record_id = {rid:UUID}",
        {"rid": out.record_id},
    )
    assert json.loads(stored["record_json"]).get("reasoning") is None
    fields = await local.query_all(
        "SELECT field_name, value_state FROM loom.memory_record_fields FINAL "
        "WHERE record_id = {rid:UUID}",
        {"rid": out.record_id},
    )
    assert {r["field_name"]: r["value_state"] for r in fields}["reasoning"] == "absent"


@pytest.mark.asyncio
async def test_a_busy_key_lease_refuses_the_outcome_as_it_is(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # memory.set's lease refusal is not its hash mismatch: it reaches the caller unchanged.
    from server.core.errors import PreconditionFailedError
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    await _write_decision_rows(local, "claude")

    async def _lease_busy(_call: int) -> None:
        raise PreconditionFailedError("another conditional write to this key is still running")

    seen = _set_into(local, monkeypatch, before=_lease_busy)
    with pytest.raises(PreconditionFailedError, match="still running"):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    assert len(seen) == 1
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_a_delete_the_write_fence_catches_answers_not_found(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The delete lands after memory.set's hash check; its write fence refuses the insert.
    from server.core.errors import NotFoundError
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h
    from server.memory.app.write_fence import lift_above_deletes

    version = await _write_decision_rows(local, "claude")

    async def _fenced(inp: Any) -> Any:
        await _delete("claude")
        row = _memory_row(inp.agent, inp.memory_type, version + 1)
        await lift_above_deletes("o", "n", [row], [], conditional=True, ch=local)
        raise AssertionError("the fence let the write through")

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _fenced)
    with pytest.raises(NotFoundError):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    assert await _history() == ["tombstoned", "active"]


def _set_with_pin_guard(ch: KeptCH, monkeypatch: pytest.MonkeyPatch) -> None:
    """memory.set reduced to its real pin guard against the caller's live row, and its insert."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo
    from server.memory.app.write import _guard_existing_row

    async def _set(inp: Any) -> Any:
        identity = memory_repo.Identity(
            org=inp.org,
            namespace=inp.namespace,
            agent=inp.agent,
            user_id=inp.user_id,
            session_id=inp.session_id,
        )
        existing = await memory_repo.get_by_key(
            identity, inp.memory_key, strong=True, scope=WHOLE_NAMESPACE
        )
        _guard_existing_row(existing, inp, "")
        row = _memory_row(inp.agent, inp.memory_type, time.time_ns())
        row.update(pinned=inp.pinned, value=json.dumps(inp.value), memory_key=inp.memory_key)
        await ch.insert("loom.memory", [row])
        return SimpleNamespace(version=row["version"])

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _set)


async def _pin_claudes_row(ch: KeptCH, version: int) -> None:
    pinned_row = _memory_row("claude", "decision", version + 1)
    pinned_row["pinned"] = True
    await ch.insert("loom.memory", [pinned_row])


@pytest.mark.asyncio
async def test_an_outcome_on_a_pinned_decision_keeps_the_pin(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # memory.set's real pin guard refuses a changed value that does not pin; the outcome
    # pins, so it lands and the row stays pinned.
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    await _pin_claudes_row(local, await _write_decision_rows(local, "claude"))
    _set_with_pin_guard(local, monkeypatch)
    out = await h.handle_decision_outcome(
        DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
    )
    assert out.updated is True
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    row = await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE)
    assert row is not None and row.pinned is True
    seed = await _seed_decision()
    assert seed is not None and seed.realized_outcome == "ok"


@pytest.mark.asyncio
async def test_a_pinned_decision_refuses_its_writer_and_names_memory_unpin(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi import HTTPException

    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    await _pin_claudes_row(local, await _write_decision_rows(local, "claude"))
    _set_with_pin_guard(local, monkeypatch)
    again = record_input(
        category="deploy",
        scenario="Roll back or hotfix the image?",
        outcome="hotfix",
        caused_by=[],
        influenced_by=[],
    )
    with pytest.raises(HTTPException) as refused:
        await h.handle_decision_record(again)
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "PINNED_CONFLICT"
    assert "memory.unpin" in refused.value.detail["message"]
    assert "memory.update" not in refused.value.detail["message"]
    assert (await _seed_decision()).outcome == "rollback"
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_another_identitys_decision_supersedes_a_pinned_one(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pin guards its writer's row, not the record: another identity's decision on the
    # same scenario is the record's new writer, and the pinned row stays as it was.
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo
    from server.memory.app import decision as h

    await h.ensure_schema("o", "n")
    await _pin_claudes_row(local, await _write_decision_rows(local, "claude"))
    _set_with_pin_guard(local, monkeypatch)
    await h.handle_decision_record(
        record_input(
            agent="maintainer",
            category="deploy",
            scenario="Roll back or hotfix the image?",
            outcome="hotfix",
            caused_by=[],
            influenced_by=[],
        )
    )
    assert (await _seed_decision()).outcome == "hotfix"
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")
    row = await memory_repo.get_by_key(identity, _KEY, strong=True, scope=WHOLE_NAMESPACE)
    assert row is not None and row.pinned is True


@pytest.mark.asyncio
async def test_an_outcome_meeting_a_pin_after_its_read_asks_for_another_call(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The writer pins its row between the outcome's read and its write: memory.set's real
    # pin guard refuses the unpinned write, and the outcome answers as for a changed row.
    from server.core.errors import PreconditionFailedError
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory import store as memory_repo
    from server.memory.api.models_decision import DecisionOutcomeInput
    from server.memory.app import decision as h
    from server.memory.app.write import _guard_existing_row

    await h.ensure_schema("o", "n")
    version = await _write_decision_rows(local, "claude")
    identity = memory_repo.Identity(org="o", namespace="n", agent="claude")

    async def _pin_then_set(inp: Any) -> Any:
        await _pin_claudes_row(local, version)
        existing = await memory_repo.get_by_key(
            identity, inp.memory_key, strong=True, scope=WHOLE_NAMESPACE
        )
        _guard_existing_row(existing, inp, "")
        raise AssertionError("the pin guard let an unpinned write through")

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", _pin_then_set)
    with pytest.raises(PreconditionFailedError, match="call decision.outcome again"):
        await h.handle_decision_outcome(
            DecisionOutcomeInput(org="o", namespace="n", memory_key=_KEY, realized_outcome="ok")
        )
    assert await _history() == ["active"]


@pytest.mark.asyncio
async def test_one_tombstone_statement_retires_each_key_by_its_own_writer(local: KeptCH) -> None:
    # Two decision keys in one call: the key whose writer's row is gone is retired, the key
    # whose writer is still live keeps its record.
    from server.memory import store as memory_repo

    version = await _write_decision_rows(local, "claude")
    other_key = svc.decision_key("deploy", "Pause the canary?")
    record = (await local.query_all("SELECT * FROM loom.memory_records FINAL"))[0]
    await local.insert(
        "loom.memory_records",
        [
            {
                **{k: v for k, v in record.items() if v is not None},
                "record_id": "00000000-0000-0000-0000-0000000000d2",
                "memory_key": other_key,
            }
        ],
    )
    live_row = _memory_row("claude", "decision", version)
    live_row["memory_key"] = other_key
    tombstone = _memory_row("claude", "decision", version + 1)
    tombstone["tombstoned_at"] = tombstone["updated_at"]
    await local.insert("loom.memory", [live_row, tombstone])

    await memory_repo.tombstone_decision_records("o", "n", [_KEY, other_key], version=version + 1)
    rows = await local.query_all(
        "SELECT memory_key, status FROM loom.memory_records FINAL ORDER BY memory_key"
    )
    assert {r["memory_key"]: r["status"] for r in rows} == {
        _KEY: "tombstoned",
        other_key: "active",
    }


@pytest.mark.asyncio
async def test_the_record_read_takes_the_stage_from_its_writers_row(
    local: KeptCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import decision as h
    from server.memory.domain.decision_schema import PROPOSED_TAG

    _set_into(local, monkeypatch)
    plan = record_input(caused_by=[], influenced_by=[], stage="proposed")
    key = (await h.handle_decision_record(plan)).memory_key
    assert (await h._decision_records(local, "o", "n", [key]))[key]["stage"] == "proposed"
    # Recorded again as decided by the same writer: its newest row has no tag.
    await h.handle_decision_record(plan.model_copy(update={"stage": "decided"}))
    assert (await h._decision_records(local, "o", "n", [key]))[key]["stage"] == "decided"
    # Another identity's tagged row under the key says nothing about the record.
    other = _memory_row("other", "decision", time.time_ns())
    other.update(memory_key=key, tags=["decision", PROPOSED_TAG])
    await local.insert("loom.memory", [other])
    assert (await h._decision_records(local, "o", "n", [key]))[key]["stage"] == "decided"
