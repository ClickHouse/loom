"""The same-key judge against ClickHouse: the peer read, its limits and the winner
re-read run as SQL on real `loom.memory` rows; only the LLM is stubbed."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core.datetimes import naive_utc_now
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from tests.integration._resources import unique_org

NS = "same-key"
USER = "u1"
KEY = "user_ethnicity"


@pytest.fixture
async def ch() -> Any:
    from server.core.ch import get_client

    c = get_client()
    await c.connect()
    yield c
    await c.disconnect()


@pytest.fixture
def org() -> str:
    return unique_org("same-key")


def _row(org: str, session: str, value: str, valid_at: datetime, **extra: Any) -> dict[str, Any]:
    from server.memory import store as M

    return M.build_first_version_row(
        {
            "org": org,
            "namespace": NS,
            "agent": "bot",
            "user_id": USER,
            "session_id": session,
            "memory_key": KEY,
            "memory_type": "semantic",
            "value": json.dumps(value),
            "value_json": json.dumps({"v": value}),
            "content_sha256": hashlib.sha256(value.encode()).hexdigest(),
            "confidence": 1.0,
            "actor": "process:itest",
            "source_class": "extracted",
            "valid_at": valid_at,
            "version": M.next_version(),
            **extra,
        },
        now=datetime.now(UTC).replace(tzinfo=None),
    )


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The judge on under auto_reinforce, labelling every peer `label`; `during`
    runs inside the judge's call, while the LLM would be thinking."""
    from server.memory.app import conflicts
    from server.memory.app.conflict_judge import Judgement
    from server.settings import settings

    state: dict[str, Any] = {"label": "contradiction", "calls": [], "during": None}

    async def judge_peers(new_value: str, peers: list[Any]) -> Judgement:
        state["calls"].append([p.memory_id for p in peers])
        if state["during"] is not None:
            await state["during"]()
        labels = {p.memory_id: state["label"] for p in peers}
        return Judgement(verdict="conflict", ran=True, labels=labels, stated=labels)

    async def policy(org: str, namespace: str, ch: Any = None) -> str:
        return "auto_reinforce"

    monkeypatch.setattr(settings, "memory_conflict_judge", True)
    monkeypatch.setattr(conflicts, "judge_peers", judge_peers)
    monkeypatch.setattr(conflicts, "namespace_conflict_policy", policy)
    return state


async def _judge(ch: Any, org: str, new: dict[str, Any]) -> Any:
    from server.memory.app.key_supersession import judge_same_key_rows

    return await judge_same_key_rows(
        org=org,
        namespace=NS,
        memory_rows=[new],
        embedding_rows=[],
        ch=ch,
        now=datetime.now(UTC).replace(tzinfo=None),
    )


async def test_a_late_old_session_lands_superseded_behind_the_stored_row(ch, org, judge) -> None:
    from server.memory import store as M

    stored = _row(org, "s2", "User has mixed Irish and Italian ethnicity", datetime(2023, 5, 30))
    await M.insert_memory_versions([stored], ch=ch)
    late = _row(org, "s1", "User is Asian American", datetime(2023, 5, 23))
    out = await _judge(ch, org, late)
    assert out.new_rows == [(late["memory_id"], stored["memory_id"])]
    assert late["status"] == "superseded"


async def test_rows_that_are_not_current_or_not_the_writers_are_never_read(ch, org, judge) -> None:
    from server.memory import store as M

    past = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    await M.insert_memory_versions(
        [
            _row(org, "s2", "expired", datetime(2023, 5, 30), expires_at=past),
            _row(org, "s3", "closed", datetime(2023, 5, 30), valid_to=past),
            _row(org, "s4", "another user's", datetime(2023, 5, 30), user_id="u2"),
            _row(org, "s5", "pinned", datetime(2023, 5, 30), pinned=1),
        ],
        ch=ch,
    )
    out = await _judge(ch, org, _row(org, "s1", "new", datetime(2023, 5, 23)))
    assert judge["calls"] == [] and out.new_rows == [] and out.peers == []


async def test_a_winner_revised_while_the_judge_ran_keeps_the_new_row_live(ch, org, judge) -> None:
    from server.memory import store as M

    stored = _row(org, "s2", "User lives in London", datetime(2023, 5, 30))
    await M.insert_memory_versions([stored], ch=ch)

    async def revise() -> None:
        head = await M.get_by_id(stored["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE)
        await M.insert_memory_versions(
            [M.build_memory_version_row(head, version=M.next_version(), value='"unrelated"')],
            ch=ch,
        )

    judge["during"] = revise
    late = _row(org, "s1", "User lives in Paris", datetime(2023, 5, 23))
    out = await _judge(ch, org, late)
    assert judge["calls"] == [[stored["memory_id"]]]
    head = await M.get_by_id(stored["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE)
    assert head is not None and head.version > stored["version"], "the revision landed"
    assert out.new_rows == [] and late["status"] == "active"


async def test_a_stored_loser_retires_unless_it_is_locked(ch, org, judge) -> None:
    from server.memory import store as M
    from server.memory.app.key_supersession import retire_judged_peers

    old = _row(org, "s2", "User lives in Paris", datetime(2023, 5, 1))
    locked = _row(org, "s3", "User lives in Rome", datetime(2023, 5, 2), lock_mode="no_delete")
    await M.insert_memory_versions([old, locked], ch=ch)
    new = _row(org, "s1", "User lives in London", datetime(2023, 5, 30))
    out = await _judge(ch, org, new)
    assert {v["memory_id"] for v in out.peers} == {old["memory_id"], locked["memory_id"]}
    await M.insert_memory_versions([new], ch=ch)

    retired = await retire_judged_peers(org, out.peers, ch=ch)
    assert [v["memory_id"] for v in retired] == [old["memory_id"]]
    rows = await ch.query_all(
        "SELECT toString(memory_id) AS id, status, superseded_by FROM loom.memory FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        params={"org": org, "ns": NS},
    )
    status = {r["id"]: (r["status"], r["superseded_by"]) for r in rows}
    assert status[old["memory_id"]] == ("superseded", new["memory_id"])
    assert status[locked["memory_id"]] == ("active", "")


async def _retire(ch: Any, org: str, value: str, valid_at: datetime, session: str) -> str:
    """Write `value` from `session`, which the judge reads as the later statement;
    returns the new row's id, the memory the older rows now retire behind."""
    from server.memory import store as M
    from server.memory.app.key_supersession import retire_judged_peers

    new = _row(org, session, value, valid_at)
    out = await _judge(ch, org, new)
    await M.insert_memory_versions([new], ch=ch)
    await retire_judged_peers(org, out.peers, ch=ch)
    return str(new["memory_id"])


def _undo(org: str, superseded_by: str) -> Any:
    from server.memory.api.models_crud import MemoryRestoreInput

    return MemoryRestoreInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s2",
        memory_key=KEY,
        if_superseded_by=superseded_by,
    )


async def test_undo_restores_only_the_retirement_it_lists(ch, org, judge) -> None:
    # Irish retired behind Japanese, rewritten by its own session, then retired again
    # behind Korean: the first record no longer describes it.
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    irish = _row(org, "s2", "User is Irish", datetime(2023, 5, 1))
    await M.insert_memory_versions([irish], ch=ch)
    japanese = await _retire(ch, org, "User is Japanese", datetime(2023, 6, 1), "s3")
    head = await M.get_by_id(irish["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE)
    await M.insert_memory_versions(
        [
            M.build_memory_version_row(
                head,
                version=M.next_version(),
                value='"User is Brazilian"',
                valid_at=datetime(2023, 7, 1),
                status="active",
                superseded_by="",
            )
        ],
        ch=ch,
    )
    korean = await _retire(ch, org, "User is Korean", datetime(2023, 8, 1), "s4")

    assert (await handle_memory_restore(_undo(org, japanese))).restored is False
    assert (await handle_memory_restore(_undo(org, korean))).restored is True


# The revision lands just before the Undo takes the key's write lock, and the Undo
# refuses; or it starts during the Undo's read under the lock, and waits for the Undo.
@pytest.mark.parametrize("revised", ["before_the_lock", "under_the_lock"])
async def test_an_undo_never_overwrites_a_conditional_revision_made_while_it_ran(
    ch, org, judge, monkeypatch, revised
) -> None:
    import contextlib

    from server.memory import store as M
    from server.memory.api.models_crud import MemoryGetByIdInput, MemoryUpdateByIdInput
    from server.memory.app import restore
    from server.memory.app.handlers import handle_memory_get_by_id
    from server.memory.app.restore import handle_memory_restore
    from server.memory.app.update import handle_memory_update_by_id

    irish = _row(org, "s2", "User is Irish", datetime(2023, 5, 1))
    await M.insert_memory_versions([irish], ch=ch)
    # The hash a writer read while the row was live.
    read_live = await handle_memory_get_by_id(
        MemoryGetByIdInput(org=org, memory_id=str(irish["memory_id"]))
    )
    japanese = await _retire(ch, org, "User is Japanese", datetime(2023, 6, 1), "s3")
    revision = MemoryUpdateByIdInput(
        org=org,
        memory_id=str(irish["memory_id"]),
        value="User is Brazilian",
        if_match_sha256=read_live.content_sha256,
    )
    revising: list[asyncio.Task[Any]] = []
    locks, read = restore.key_write_lock.key_write_locks, M.get_by_key
    hold = locks.hold

    @contextlib.asynccontextmanager
    async def revise_then_hold(*args: Any) -> Any:
        # Only the Undo's call starts the revision; the revision's own goes straight on.
        if not revising:
            revising.append(asyncio.create_task(handle_memory_update_by_id(revision)))
            await asyncio.wait(revising, timeout=5)
        async with hold(*args):
            yield

    async def read_then_revise(*args: Any, **kwargs: Any) -> Any:
        row = await read(*args, **kwargs)
        if not revising:
            revising.append(asyncio.create_task(handle_memory_update_by_id(revision)))
            await asyncio.wait(revising, timeout=5)
        return row

    if revised == "before_the_lock":
        monkeypatch.setattr(locks, "hold", revise_then_hold)
    else:
        monkeypatch.setattr(M, "get_by_key", read_then_revise)
    undone = await handle_memory_restore(_undo(org, japanese))
    assert (await revising[0]).updated
    head = await M.get_by_id(
        str(irish["memory_id"]), org=org, ch=ch, strong=True, scope=WHOLE_NAMESPACE
    )
    assert "Brazilian" in str(head.value)
    if revised == "under_the_lock":
        assert undone.restored and undone.version < head.version
    else:  # the revision made the row live again: nothing to undo
        assert undone.restored is False
    assert not undone.restored or undone.version < head.version


async def test_undo_survives_a_re_emit_and_refuses_a_later_delete(ch, org, judge) -> None:
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    irish = _row(org, "s2", "User is Irish", datetime(2023, 5, 1))
    await M.insert_memory_versions([irish], ch=ch)
    winner = await _retire(ch, org, "User is Japanese", datetime(2023, 6, 1), "s3")
    # The entity tagger re-emits a retired row under a new version, retirement kept.
    head = await M.get_by_id(irish["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE)
    await M.insert_memory_versions(
        [M.build_memory_version_row(head, version=M.next_version(), entities=["irish"])], ch=ch
    )
    assert (await handle_memory_restore(_undo(org, winner))).restored is True

    # Retired again, then deleted by hand: the Undo leaves the delete alone.
    winner = await _retire(ch, org, "User is Korean", datetime(2023, 8, 1), "s4")
    head = await M.get_by_id(irish["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE)
    await M.insert_memory_versions(
        [
            M.build_memory_version_row(
                head, version=M.next_version(), tombstoned_at=datetime.now(UTC).replace(tzinfo=None)
            )
        ],
        ch=ch,
    )
    assert (await handle_memory_restore(_undo(org, winner))).restored is False


async def test_a_retirement_a_later_write_overtook_is_not_reported(ch, org, monkeypatch) -> None:
    # A revision lands after the retirement read the row, at a higher version: FINAL
    # keeps the revision, so the retirement is neither reported nor mirrored.
    from server.memory import store as M
    from server.memory.app.auto_resolution import RetirementTracker
    from server.memory.store import lifecycle

    row = _row(org, "s2", "User lives in Paris", datetime(2023, 5, 1))
    await M.insert_memory_versions([row], ch=ch)
    insert = lifecycle.insert_memory_versions

    async def revision_first(rows: list[dict[str, Any]], *, ch: Any) -> None:
        head = await M.get_by_id(row["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE)
        revision = M.build_memory_version_row(head, version=M.next_version(), lock_mode="no_delete")
        await insert([revision], ch=ch)
        await insert(rows, ch=ch)

    monkeypatch.setattr(lifecycle, "insert_memory_versions", revision_first)
    tracker = RetirementTracker()
    victims = [{**row, "superseded_by": "winner"}]
    n = await M.supersede_duplicates(
        org, victims, ch=ch, skip=tracker.skip, strong=True, on_written=tracker.written
    )
    assert n == 0 and tracker.retired(victims) == []
    head = await M.get_by_id(row["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE)
    assert head.status == "active" and head.lock_mode == "no_delete"


async def test_a_key_set_again_under_a_new_id_overtakes_the_retirement(
    ch, org, monkeypatch
) -> None:
    # Deleted and set again after the retirement's read: a new memory_id at the key.
    from server.memory import store as M
    from server.memory.app.auto_resolution import RetirementTracker
    from server.memory.store import lifecycle

    row = _row(org, "s2", "User lives in Paris", datetime(2023, 5, 1))
    await M.insert_memory_versions([row], ch=ch)
    insert = lifecycle.insert_memory_versions

    async def reset_first(rows: list[dict[str, Any]], *, ch: Any) -> None:
        await insert([_row(org, "s2", "User lives in Lisbon", datetime(2023, 6, 1))], ch=ch)
        await insert(rows, ch=ch)

    monkeypatch.setattr(lifecycle, "insert_memory_versions", reset_first)
    tracker = RetirementTracker()
    victims = [{**row, "superseded_by": "winner"}]
    n = await M.supersede_duplicates(
        org, victims, ch=ch, skip=tracker.skip, strong=True, on_written=tracker.written
    )
    assert n == 0 and tracker.retired(victims) == []


async def test_a_retirement_chain_is_read_to_its_end(ch, org) -> None:
    # a -> b -> c with b's link in capitals; d retired behind an erased row; e and f
    # behind ids ClickHouse can't parse as UUIDs, which end their chains.
    import uuid
    from types import SimpleNamespace

    from server.memory import store as M
    from server.memory.app.entity_projections import _chain_head, _rows_along_chains

    at = datetime(2026, 5, 1)
    c = _row(org, "s3", "Rome", at)
    c_id = str(c["memory_id"])
    b = _row(org, "s2", "London", at, status="superseded", superseded_by=c_id.upper())
    a = _row(org, "s1", "Paris", at, status="superseded", superseded_by=str(b["memory_id"]))
    erased = str(uuid.uuid4())
    d = _row(org, "s4", "Oslo", at, status="superseded", superseded_by=erased)
    e = _row(org, "s5", "Lima", at, status="superseded", superseded_by="junk")
    f = _row(org, "s6", "Kyiv", at, status="superseded", superseded_by="{" + erased + "}")
    await M.insert_memory_versions([a, b, c, d, e, f], ch=ch)

    a_id, d_id, e_id, f_id = (str(r["memory_id"]) for r in (a, d, e, f))
    scope = SimpleNamespace(org=org, namespace=NS)
    by_id = await _rows_along_chains(scope, [a_id, d_id, e_id, f_id], ch=ch)
    assert _chain_head(by_id[a_id], by_id)["id"] == c_id
    assert _chain_head(by_id[d_id], by_id) == {}
    assert [_chain_head(by_id[i], by_id)["id"] for i in (e_id, f_id)] == [e_id, f_id]


async def test_a_close_never_ends_a_triple_before_it_starts(ch, org) -> None:
    from server.memory.app.projections import close_triples, entity_triple_row

    def triple(obj: str, valid_from: datetime | None) -> dict[str, Any]:
        return entity_triple_row(
            org,
            NS,
            "user",
            "live_in",
            obj,
            provenance="extracted",
            confidence=1.0,
            adapter_name="itest",
            last_seen=datetime(2026, 10, 1),
            seen_at=datetime(2026, 10, 1),
            valid_from=valid_from,
        )

    # Each close carries its own date, so one landing on another row's would show.
    future, past, now = datetime(2027, 1, 1), datetime(2025, 1, 1), datetime(2026, 10, 5)
    rome_until = datetime(2026, 3, 1)
    rows = [
        triple("paris", future),
        triple("rome", past),
        triple("oslo", None),
        triple("lima", None),  # not closed
    ]
    await ch.insert("loom.entity_triples", rows, settings={"wait_for_async_insert": 1})
    closes = [
        ("user", "live_in", "paris", now, now),
        ("user", "live_in", "rome", rome_until, now),
        ("user", "live_in", "oslo", now, now),
    ]
    await close_triples(ch, org=org, namespace=NS, closes=closes)
    # The close is a mutation: wait for it to land.
    for _ in range(50):
        got = await ch.query_all(
            "SELECT object, valid_to FROM loom.entity_triples WHERE org = {org:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        valid_to = {r["object"]: r["valid_to"] for r in got}
        if all(valid_to.get(o) is not None for o in ("paris", "rome", "oslo")):
            break
        await asyncio.sleep(0.2)
    assert valid_to == {"paris": future, "rome": rome_until, "oslo": now, "lima": None}


async def test_ending_a_future_triple_again_keeps_its_close(ch, org) -> None:
    # A row dated ahead ends at its own start; a later end_triples_now finds nothing to
    # end and must leave invalid_at, the close's own mark, where the first one put it.
    from server.memory.app.projections import end_triples_now, entity_triple_row

    start = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=30)
    row = entity_triple_row(
        org,
        NS,
        "user",
        "live_in",
        "paris",
        provenance="extracted",
        confidence=1.0,
        adapter_name="itest",
        last_seen=start,
        seen_at=start,
        valid_from=start,
    )
    row["valid_to"] = start + timedelta(days=30)
    await ch.insert("loom.entity_triples", [row], settings={"wait_for_async_insert": 1})

    async def _end() -> dict[str, Any]:
        await end_triples_now(ch, org=org, namespace=NS, triples=[("user", "live_in", "paris")])
        await _mutations_done(ch, org)
        (got,) = await ch.query_all(
            "SELECT valid_to, invalid_at FROM loom.entity_triples WHERE org = {org:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        return got

    first = await _end()
    assert first["valid_to"] == start and first["invalid_at"] is not None
    await asyncio.sleep(1.1)  # a second later: a rewrite would move invalid_at
    assert await _end() == first


async def test_open_triples_are_looked_up_by_their_exact_key(ch, org) -> None:
    # Only the open copy counts: a closed one may be written again by a replay.
    from server.memory.app.projections import entity_triple_row, open_triples_among

    def triple(obj: str, **kw: Any) -> dict[str, Any]:
        return entity_triple_row(
            org,
            NS,
            "user",
            "live_in",
            obj,
            provenance="extracted",
            confidence=1.0,
            adapter_name="itest",
            last_seen=datetime(2026, 10, 1),
            seen_at=datetime(2026, 10, 1),
            **kw,
        )

    closed = datetime(2026, 10, 2)
    rows = [
        triple("berlin germany"),
        triple("berlin germany", valid_to=closed),
        triple("rome", valid_to=closed),
    ]
    await ch.insert("loom.entity_triples", rows, settings={"wait_for_async_insert": 1})
    asked = [
        ("user", "live_in", "berlin germany"),
        ("user", "live_in", "berlin"),
        ("user", "live_in", "rome"),
    ]
    found = await open_triples_among(ch, org=org, namespace=NS, triples=asked)
    assert found == {("user", "live_in", "berlin germany")}


async def _stated(
    ch: Any,
    org: str,
    session: str,
    value: str,
    triple: tuple | None = None,
    extracted: bool = True,
    also: tuple[tuple[str, str, str], ...] = (),
    **extra: Any,
) -> dict[str, Any]:
    """A row of `value` under `session`, its triple written by the projection."""
    from server.memory.app.projections import write_entity_triples

    triple = triple or ("user", "favourite_city", value.rsplit(" ", 1)[-1].lower())
    row, write, results = await _row_and_write(
        ch, org, session, value, [triple, *also] if extracted else [], **extra
    )
    await write_entity_triples(write, results, ch=ch, entity_map={})
    return row


async def _row_and_write(
    ch: Any,
    org: str,
    session: str,
    value: str,
    relations: list,
    valid_at: datetime | None = None,
    fact_valid_to: datetime | None = None,
    **extra: Any,
) -> tuple[dict[str, Any], Any, list[Any]]:
    """A stored row of `value` and the bulk write that states its `relations`, dated
    `valid_at` and ending at `fact_valid_to` when given."""
    from server.core.hashing import sha256_value
    from server.memory import store as M
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult

    row = _row(
        org, session, value, datetime(2026, 5, 1), content_sha256=sha256_value(value), **extra
    )
    await M.insert_memory_versions([row], ch=ch)
    key = row["memory_key"]
    write = MemoryBulkSetInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id=session,
        entries=[
            {
                "memory_key": key,
                "value": value,
                "relations": relations,
                "valid_at": valid_at,
                "valid_to": fact_valid_to,
            }
        ],
    )
    result = _BulkSetResult(memory_key=key, version=row["version"], memory_id=str(row["memory_id"]))
    return row, write, [result]


async def _follow_retirement(
    ch: Any, org: str, loser: dict[str, Any], winner: dict[str, Any], wait: bool = True
) -> None:
    from types import SimpleNamespace

    from server.memory import store as M
    from server.memory.app.entity_projections import follow_row_supersessions

    retired = {**loser, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(winner["memory_id"])
    await M.insert_memory_versions([retired], ch=ch)
    scope = SimpleNamespace(org=org, namespace=NS)
    await follow_row_supersessions(
        scope, [(str(loser["memory_id"]), retired["superseded_by"])], ch=ch
    )
    # Its closes land before the next write: an open loser would be a racing value.
    if wait:
        await _mutations_done(ch, org)


async def _mutations_done(ch: Any, org: str, kill_after: int | None = None) -> None:
    """Wait for the org's triple and edge closes to land: a check that nothing closed
    cannot poll. With `kill_after`, kill what is still running after that many polls
    instead of failing, so no mutation stays behind on the shared server."""
    for attempt in range(100):
        if attempt == kill_after:
            await ch.execute(
                "KILL MUTATION WHERE database = 'loom'"
                " AND table IN ('entity_triples', 'entity_edges')"
                f" AND position(command, '{org}') > 0"
            )
            return
        (row,) = await ch.query_all(
            "SELECT countIf(NOT is_done) AS running,"
            " maxIf(latest_fail_reason, NOT is_done) AS failed FROM system.mutations"
            " WHERE database = 'loom' AND table IN ('entity_triples', 'entity_edges')"
            " AND position(command, {org:String}) > 0",
            params={"org": org},
        )
        if row["failed"]:
            pytest.fail(f"a triple close failed: {row['failed']}")
        if not row["running"]:
            return
        await asyncio.sleep(0.1)
    pytest.fail("triple closes still running after ten seconds")


async def _open_cities(ch: Any, org: str, want: set[str]) -> set[str]:
    # A close is a mutation: wait for it to land, up to ten seconds.
    for _ in range(50):
        got = await ch.query_all(
            "SELECT object FROM loom.entity_triples WHERE org = {org:String}"
            " AND valid_to IS NULL SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        cities = {r["object"] for r in got}
        if cities == want:
            break
        await asyncio.sleep(0.2)
    return cities


async def _listed(org: str, relation: str, want: set[str]) -> set[str]:
    """The objects `facts.list` answers as current for `relation`, waiting up to two
    seconds for `want`: it counts a fact closed this very second as current."""
    from server.memory.api.models_facts import FactsListInput
    from server.memory.app.facts import handle_facts_list

    for _ in range(10):
        out = await handle_facts_list(FactsListInput(org=org, namespace=NS, relation=relation))
        listed = {r.object for r in out.rows}
        if listed == want:
            break
        await asyncio.sleep(0.2)
    return listed


async def test_a_first_person_correction_closes_the_triple_its_row_stated(ch, org) -> None:
    # "My" is stored as `user`, which the row's text never names.
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London")
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_triple_another_live_row_states_stays_open_until_it_is_revised(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    sibling = await _stated(ch, org, "s3", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London")
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london", "paris"}) == {"london", "paris"}
    # The sibling is revised in place: its id stays, its value no longer states Paris.
    await _stated(ch, org, "s3", "My favourite city is Rome", memory_id=sibling["memory_id"])
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london", "rome"}) == {"london", "rome"}


@pytest.mark.parametrize(
    ("revised", "want"),
    [
        # Its words are unchanged: the claim its extractor stated still holds.
        ("My favourite city is Paris.", {"london", "paris"}),
        # Words alone cannot tell added detail from a retraction.
        ("My favourite city is Paris, for now", {"london"}),
    ],
)
async def test_a_live_row_revised_without_its_relations_keeps_what_it_still_states(
    ch, org, revised, want
) -> None:
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    sibling = await _stated(ch, org, "s3", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London")
    # Revised in place, its extractor naming no relation this time.
    await _stated(ch, org, "s3", revised, extracted=False, memory_id=sibling["memory_id"])
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, want) == want


async def test_a_bulk_write_links_its_triple_to_the_row_it_stored(ch, org) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {
                    "memory_key": KEY,
                    "value": "My favourite city is Paris",
                    "relations": [("user", "favourite_city", "paris")],
                }
            ],
        )
    )
    stored = await ch.query_all(
        "SELECT toString(memory_id) AS id, toString(content_sha256) AS sha FROM loom.memory FINAL"
        " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    links = await ch.query_all(
        "SELECT toString(memory_id) AS id, toString(content_sha256) AS sha, object"
        " FROM loom.entity_triple_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r.memory_id for r in out.written] == [stored[0]["id"]]
    assert links == [{**stored[0], "object": "paris"}]


async def test_a_replayed_bulk_edge_projection_skips_a_value_the_same_bulk_replaced(
    ch, org, monkeypatch
) -> None:
    # One bulk revises a row twice and its edge insert fails: the replay asks by value,
    # so the row holding Rome does not bring back London's edge.
    from server.core import ch as ch_mod
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.projection_failures import retry_projection_failure
    from server.memory.app.write import handle_memory_set

    def _entry(city: str) -> dict[str, Any]:
        return {
            "memory_key": "alice",
            "value": f"Alice likes {city}",
            "entities": ["alice", city.lower()],
        }

    await handle_memory_set(
        MemorySetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", **_entry("Paris")
        )
    )
    await after_write.drain()
    client = ch_mod.get_client()
    insert = client.insert

    async def edges_fail(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_edges":
            raise ConnectionError("transient")
        return await insert(table, rows, **kw)

    monkeypatch.setattr(client, "insert", edges_fail)
    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[_entry("London"), _entry("Rome")],
        )
    )
    await after_write.drain()
    monkeypatch.setattr(client, "insert", insert)
    failure = await _recorded_reconcile(ch, org, "entity_edges")
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # past the second an edge ended in
    walked = await _walked(org, ["rome"], labeled=False, seed="alice", until=lambda w: "rome" in w)
    assert "rome" in walked and "london" not in walked


@pytest.mark.parametrize("verdict", ["conflict", "refines"])
async def test_a_verdict_on_a_value_the_same_bulk_replaced_settles_nothing(
    ch, org, monkeypatch, verdict
) -> None:
    # One bulk writes alice twice; only the first value is judged against Paris. The
    # row ends holding Rome, so neither it nor Paris may be flagged or retired.
    from types import SimpleNamespace

    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, bulk_set
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictPeer

    ids = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    paris = await handle_memory_set(
        MemorySetInput(**ids, memory_key="bob", value="Bob likes Paris")
    )
    await after_write.drain()
    peer = ConflictPeer(
        memory_id=paris.memory_id,
        memory_key="bob",
        similarity=0.9,
        pinned=False,
        canonical="bob likes paris",
        agent="bot",
        user_id=USER,
        session_id="s1",
        value="Bob likes Paris",
    )
    judged = SimpleNamespace(
        verdict=verdict, peers=[peer], refined_peers=[peer], refine_relation="refines"
    )

    async def first_entry_judged(*a: Any, **k: Any) -> dict[int, Any]:
        return {0: judged}

    monkeypatch.setattr(bulk_set, "_bulk_conflict_verdicts", first_entry_judged)
    out = await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(
            **ids,
            entries=[
                {"memory_key": "alice", "value": "Alice likes London"},
                {"memory_key": "alice", "value": "Alice likes Rome"},
            ],
        )
    )
    await after_write.drain()
    alice = {"memory_id": out.written[-1].memory_id}
    assert await _status(ch, org, alice) == "active"
    assert await _status(ch, org, {"memory_id": paris.memory_id}) == "active"


async def test_a_refine_of_a_row_a_stale_merge_rewrote_retires_nothing(
    ch, org, monkeypatch
) -> None:
    # Stored: A is Berlin, B is Jupiter. One bulk rewrites A, writes Berlin to B (a merge
    # into A, whose Berlin the first entry replaced, so it lands) and elaborates Jupiter
    # under C. C's refine was judged against B's old Jupiter: B now says Berlin, and
    # retiring it would lose the fact.
    from types import SimpleNamespace

    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, bulk_set
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictPeer, ConflictResult

    ids = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    a = await handle_memory_set(MemorySetInput(**ids, memory_key="a", value="Berlin"))
    b = await handle_memory_set(MemorySetInput(**ids, memory_key="b", value="Jupiter"))
    await after_write.drain()
    jupiter = ConflictPeer(
        memory_id=b.memory_id,
        memory_key="b",
        similarity=0.9,
        pinned=False,
        canonical="jupiter",
        agent="bot",
        user_id=USER,
        session_id="s1",
        value="Jupiter",
    )
    judged = {
        1: SimpleNamespace(verdict="merge", existing_memory_id=a.memory_id, peers=[]),
        2: ConflictResult(verdict="refines", peers=[jupiter], refined_peers=[jupiter]),
    }

    async def verdicts(*args: Any, **kw: Any) -> dict[int, Any]:
        return judged

    monkeypatch.setattr(bulk_set, "_bulk_conflict_verdicts", verdicts)
    await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(
            **ids,
            entries=[
                {"memory_key": "a", "value": "A red bicycle"},
                {"memory_key": "b", "value": "Berlin"},
                {"memory_key": "c", "value": "Jupiter, the largest planet"},
            ],
        )
    )
    await after_write.drain()
    assert await _status(ch, org, {"memory_id": b.memory_id}) == "active"
    assert (await _live_values(ch, org))[("s1", "b")] == ("Berlin", 0)


@pytest.mark.parametrize(
    "extra",
    [{}, {"valid_at": datetime(2019, 1, 1), "valid_to": datetime(2020, 1, 1)}],
    ids=["open", "closed-in-the-past"],
)
async def test_a_replayed_bulk_edge_projection_writes_what_the_bulk_write_would(
    ch, org, monkeypatch, extra
) -> None:
    # The edge insert fails once; the replay projects the entry as the write did,
    # a fact that ended in the past included.
    from server.core import ch as ch_mod
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.projection_failures import retry_projection_failure

    client = ch_mod.get_client()
    insert = client.insert

    async def edges_fail(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_edges":
            raise ConnectionError("transient")
        return await insert(table, rows, **kw)

    monkeypatch.setattr(client, "insert", edges_fail)
    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {
                    "memory_key": KEY,
                    "value": "Alice met Bob",
                    "entities": ["alice", "bob"],
                    **extra,
                }
            ],
        )
    )
    monkeypatch.setattr(client, "insert", insert)
    failure = await _recorded_reconcile(ch, org, "entity_edges")
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    (row,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'alice' AND entity_b = 'bob' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert row["n"] == 1


async def test_the_follows_own_successor_closes_when_its_row_retires(ch, org) -> None:
    # London's and Rome's extractors stated only a food: London's city is the follow's.
    pasta = ("user", "favourite_food", "pasta")
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London", triple=pasta)
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london", "pasta"}) == {"london", "pasta"}
    rome = await _stated(ch, org, "s3", "My favourite city is Rome", triple=pasta)
    await _follow_retirement(ch, org, london, rome)
    assert await _open_cities(ch, org, {"pasta", "rome"}) == {"pasta", "rome"}


async def test_repeated_first_person_corrections_keep_only_the_last(ch, org) -> None:
    # Only the first write's extractor named a relation; the follow writes the rest.
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    rome = await _stated(ch, org, "s3", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, london, rome)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}


async def test_a_successor_closes_after_its_row_is_revised_in_place(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # Revised in place: the id stays, the value (and so its hash) changes.
    acme = ("user", "works_at", "acme")
    london = await _stated(
        ch, org, "s2", "I live in London, at Acme", triple=acme, memory_id=london["memory_id"]
    )
    rome = await _stated(ch, org, "s3", "I live in Rome, at Acme", triple=acme)
    await _follow_retirement(ch, org, london, rome)
    assert await _open_cities(ch, org, {"acme", "rome"}) == {"acme", "rome"}


async def test_an_abandoned_repair_keeps_what_the_write_recorded(ch, org) -> None:
    from server.memory.app.projection_failures import (
        FOLLOW_ROWS,
        mark_projection_failure_abandoned,
        record_projection_failure,
    )

    await record_projection_failure(
        org=org, namespace=NS, projection=FOLLOW_ROWS, error="pairs left: 3", ch=ch
    )
    rows: list[dict[str, Any]] = []
    for _ in range(50):  # the failure row is an async insert
        rows = await ch.query_all(
            "SELECT toString(failure_id) AS id FROM loom.memory_projection_failures"
            " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        if rows:
            break
        await asyncio.sleep(0.2)
    (row,) = rows
    await mark_projection_failure_abandoned(
        org=org, failure_id=row["id"], reason="unfollowable_chain: 1 pair(s)", ch=ch
    )
    for _ in range(50):
        (got,) = await ch.query_all(
            "SELECT toString(status) AS status, error_message, resolved_at"
            " FROM loom.memory_projection_failures WHERE org = {org:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        if got["status"] == "abandoned":
            break
        await asyncio.sleep(0.2)
    assert got == {
        "status": "abandoned",
        "error_message": "pairs left: 3; abandoned: unfollowable_chain: 1 pair(s)",
        "resolved_at": None,
    }


async def test_a_deleted_row_is_found_by_id_only_when_asked(ch, org) -> None:
    from server.memory import store as M

    row = _row(org, "s1", "Paris", datetime(2026, 5, 1))
    deleted = {**row, "version": M.next_version(), "tombstoned_at": datetime(2026, 10, 1)}
    await M.insert_memory_versions([row, deleted], ch=ch)
    mid = str(row["memory_id"])
    seen = await M.get_by_id(
        mid, org=org, ch=ch, scope=WHOLE_NAMESPACE, strong=True, include_tombstoned=True
    )
    assert seen is not None and seen.tombstoned_at is not None
    assert await M.get_by_id(mid, org=org, ch=ch, scope=WHOLE_NAMESPACE, strong=True) is None


async def test_a_revised_row_still_holds_open_the_successor_written_for_it(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # Revised in place to the same words: London is still its own.
    revised = "I currently live in London."
    await _stated(ch, org, "s2", revised, extracted=False, memory_id=london["memory_id"])
    # Another session's London row retires behind Rome: the revised row keeps London.
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    want = {"london", "rome"}
    assert await _open_cities(ch, org, want) == want


async def test_a_revision_that_drops_the_successor_lets_it_close(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # Revised to Berlin: London is no longer the row's, so it is not carried.
    berlin = ("user", "favourite_city", "berlin")
    await _stated(
        ch, org, "s2", "I currently live in Berlin", triple=berlin, memory_id=london["memory_id"]
    )
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    want = {"berlin", "rome"}
    assert await _open_cities(ch, org, want) == want


async def _revise(
    path: str, org: str, row: dict[str, Any], value: str, relations: tuple = ()
) -> None:
    if path.startswith("memory.set"):
        from server.memory.api.models_crud import MemorySetInput
        from server.memory.app.write import handle_memory_set

        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id=row["session_id"],
                memory_key=KEY,
                value=value,
                relations=list(relations),
                # Not embedded: the plain insert path.
                sensitivity="high" if path == "memory.set high" else "",
            )
        )
    elif path == "memory.update":
        from server.memory.api.models_crud import MemoryUpdateInput
        from server.memory.app.update import handle_memory_update

        await handle_memory_update(
            MemoryUpdateInput(
                org=org,
                namespace=NS,
                memory_key=KEY,
                session_id=row["session_id"],
                value=value,
                relations=list(relations),
            )
        )
    else:
        from server.memory.api.models_crud import MemoryUpdateByIdInput
        from server.memory.app.update import handle_memory_update_by_id

        await handle_memory_update_by_id(
            MemoryUpdateByIdInput(
                org=org, memory_id=str(row["memory_id"]), value=value, relations=list(relations)
            )
        )


_POINT_PATHS = ["memory.set", "memory.set high", "memory.update_by_id"]


@pytest.mark.parametrize("path", _POINT_PATHS)
@pytest.mark.parametrize(
    ("revised", "want"),
    [
        ("I currently live in London.", {"london", "rome"}),
        ("I currently live in London, near the river", {"rome"}),
        ("I currently live in Berlin", {"rome"}),
        # Still names London, not as where the user lives.
        ("I currently live in Rome and visited London", {"rome"}),
    ],
)
async def test_a_point_revision_carries_the_successor_it_still_states(
    ch, org, path, revised, want
) -> None:
    if path == "memory.set high":
        want = {"rome"}  # a 'high' row holds no graph facts, whatever it says
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    await _revise(path, org, london, revised)
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, want) == want
    assert await _listed(org, "favourite_city", want) == want


# A 'high' row's named facts are not graphed: its words alone decide.
@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("memory.set", {"london", "rome"}),
        ("memory.set high", {"rome"}),
        ("memory.update_by_id", {"london", "rome"}),
        ("memory.update", {"london", "rome"}),
    ],
)
async def test_a_point_revision_that_states_its_fact_keeps_it_through_added_detail(
    ch, org, path, want
) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    lives = ("user", "favourite_city", "london")
    await _revise(path, org, london, "I currently live in London, near the river", (lives,))
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, want) == want
    assert await _listed(org, "favourite_city", want) == want


async def test_a_point_write_naming_a_single_valued_fact_closes_the_older_one(ch, org) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    await _stated(ch, org, "s1", "I currently live in London", ("user", "live_in", "london"))
    await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s2",
            memory_key="user_home",
            value="I moved to Rome",
            relations=[("user", "live_in", "rome")],
        )
    )
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "rome")}) == {("live_in", "rome")}


async def test_a_proposal_naming_a_single_valued_fact_changes_no_established_one(
    ch, org, newest_wins
) -> None:
    # A fact on trial supersedes nothing: the facts it names stay out of the graph.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    london = await _stated(ch, org, "s1", "I live in London", ("user", "live_in", "london"))
    await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s2",
            memory_key="user_home",
            value="I moved to Rome",
            relations=[("user", "live_in", "rome")],
            proposed=True,
        )
    )
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "london")}) == {("live_in", "london")}
    assert await _status(ch, org, london) == "active"


async def _legacy_stated(
    ch: Any, org: str, value: str, triple: tuple, key: str, written: datetime | None = None
) -> dict[str, Any]:
    """A row written before rows were linked to triples, or (`written` now) by the
    previous build during a rollout: its triple is linked to its key only, as that
    build's projection links it just after the row. Returns the row."""
    from server.memory import store as M
    from server.memory.app.projections import entity_triple_row

    now = naive_utc_now()
    written = written or datetime(2020, 1, 1)  # before row links
    row = _row(org, "s0", value, now, memory_key=key)
    row["created_at"] = row["updated_at"] = written
    await M.insert_memory_versions([row], ch=ch)
    s, r, o = triple
    await ch.insert(
        "loom.entity_triples",
        [
            entity_triple_row(
                org,
                NS,
                s,
                r,
                o,
                provenance="",
                confidence=1.0,
                adapter_name="legacy",
                last_seen=now,
                seen_at=now,
            )
        ],
    )
    link = {"org": org, "namespace": NS, "subject": s, "relation": r, "object": o}
    await ch.insert(
        "loom.entity_triple_memories", [{**link, "memory_key": key, "seen_at": written}]
    )
    return row


@pytest.mark.parametrize(
    ("legacy", "want"),
    [
        ("I like jazz", {("like", "jazz"), ("like", "rock")}),
        # Its key is linked to jazz, but its value no longer names it.
        ("I like folk", {("like", "rock")}),
    ],
)
async def test_a_fact_a_row_predating_row_links_states_survives_a_revision(
    ch, org, legacy, want
) -> None:
    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    await _legacy_stated(ch, org, legacy, jazz, "legacy")
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, want) == want


async def test_a_fact_a_previous_build_row_states_survives_a_revision(ch, org) -> None:
    # Mid-rollout the previous build writes "I like jazz" with a key link and no row
    # link. Another row stating jazz is revised to rock: the previous build's row still
    # holds jazz.
    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    await _legacy_stated(ch, org, "I like jazz", jazz, "older", written=naive_utc_now())
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    want = {("like", "jazz"), ("like", "rock")}
    assert await _open_facts(ch, org, want) == want


@pytest.mark.parametrize("changed", ["pinned", "revised"])
async def test_a_row_predating_row_links_holds_by_when_its_value_was_first_written(
    ch, org, changed
) -> None:
    # A pin, a lock, a tag or an Undo writes a new version of the same value: the row
    # still holds what its value stated before rows were linked.
    from server.memory import store as M

    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    want = {("like", "jazz"), ("like", "rock")}
    await _legacy_stated(ch, org, "I like jazz", jazz, "legacy")
    (old,) = await ch.query_all(
        "SELECT memory_id FROM loom.memory FINAL WHERE org = {org:String}"
        " AND memory_key = 'legacy'",
        params={"org": org},
    )
    head = await M.get_by_id(str(old["memory_id"]), org=org, ch=ch, scope=WHOLE_NAMESPACE)
    if changed == "pinned":
        again = {"pinned": True}
    else:  # a value written since links, which states no fact, holds none
        retraction = "I no longer like jazz"
        again = {
            "value": json.dumps(retraction),
            "content_sha256": hashlib.sha256(retraction.encode()).hexdigest(),
        }
        want = {("like", "rock")}
    rewritten = M.build_memory_version_row(
        head, version=M.next_version(), updated_at=naive_utc_now(), **again
    )
    await M.insert_memory_versions([rewritten], ch=ch)
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, want) == want


async def _link_inferred(ch: Any, org: str, row: dict[str, Any], triple: tuple) -> None:
    """Link `row` to `triple` as a follow links a successor to its winner."""
    from server.memory.app.projections import triple_row_link

    link = triple_row_link(
        org, NS, str(row["memory_id"]), row["content_sha256"], triple, user_id=USER, inferred=True
    )
    await ch.insert("loom.entity_triple_rows", [link])


async def test_an_inferred_link_leaves_a_row_predating_row_links_its_old_facts(ch, org) -> None:
    # A successor written for it is not what its value stated before rows were linked.
    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    legacy = await _legacy_stated(ch, org, "I like jazz", jazz, "legacy")
    await _link_inferred(ch, org, legacy, ("user", "like", "folk"))
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    want = {("like", "jazz"), ("like", "rock")}
    assert await _open_facts(ch, org, want) == want


async def test_a_row_written_since_row_links_that_stated_nothing_keeps_nothing_open(
    ch, org
) -> None:
    # "I no longer like jazz" produced no triples, so it has no row link; it is not a
    # row from before links, and must not keep jazz open once the jazz row is revised.
    from server.memory import store as M

    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    await M.insert_memory_versions(
        [_row(org, "s9", "I no longer like jazz", naive_utc_now(), memory_key="r1")], ch=ch
    )
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("like", "rock")}) == {("like", "rock")}


async def test_a_fact_a_row_predating_row_links_states_survives_a_follow(ch, org) -> None:
    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    await _legacy_stated(ch, org, "I like jazz", jazz, "legacy")
    loser = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    winner = await _stated(ch, org, "s2", "I like rock", rock, memory_key="r1")
    await _follow_retirement(ch, org, loser, winner)
    want = {("like", "jazz"), ("like", "rock")}
    assert await _open_facts(ch, org, want) == want


@pytest.mark.parametrize("inferred", [False, True])
async def test_undo_brings_back_the_facts_a_row_predating_row_links_holds(
    ch, org, inferred
) -> None:
    # The follow closes a legacy row's facts by what its value names; the Undo has no
    # row link to find them by, and reopens them by the same rule. A successor linked
    # to the row (`inferred`) is no link to them.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore

    jazz, rock = ("alice", "like", "jazz"), ("alice", "like", "rock")
    legacy = await _legacy_stated(ch, org, "Alice likes jazz", jazz, KEY)
    if inferred:
        await _link_inferred(ch, org, legacy, ("alice", "like", "folk"))
    winner = await _stated(ch, org, "s2", "Alice likes rock", rock)
    await _follow_retirement(ch, org, legacy, winner)
    assert await _open_facts(ch, org, {("like", "rock")}) == {("like", "rock")}
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s0",
            memory_key=KEY,
            if_superseded_by=str(winner["memory_id"]),
        )
    )
    assert out.restored
    await _mutations_done(ch, org)
    want = {("like", "jazz"), ("like", "rock")}
    assert await _open_facts(ch, org, want) == want


async def test_a_merged_write_links_the_facts_it_names_to_the_row_it_merged_into(ch, org) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    def write(key: str, relations: list, user: str = USER, **extra: Any) -> MemorySetInput:
        return MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=user,
            session_id="s1",
            memory_key=key,
            value="I currently live in London",
            relations=relations,
            **extra,
        )

    first = await handle_memory_set(write("home", []))
    merged = await handle_memory_set(write("city", [("user", "live_in", "london")]))
    assert merged.conflict_verdict == "merge"
    links = await ch.query_all(
        "SELECT toString(memory_id) AS id, toString(content_sha256) AS sha"
        " FROM loom.entity_triple_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert links == [{"id": first.memory_id, "sha": first.content_sha256}]
    # Merged into another user's row, the facts link to nothing.
    theirs = await handle_memory_set(write("place", [("user", "live_in", "rome")], user="u2"))
    assert theirs.conflict_verdict == "merge" and "relations not linked" in theirs.next
    dry = await handle_memory_set(write("place", [("user", "live_in", "rome")], "u2", dry_run=True))
    assert dry.next == theirs.next
    links = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triple_rows WHERE org = {org:String}"
        " AND object = 'rome' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert links == [{"n": 0}]


async def test_a_row_restating_a_triple_while_the_follow_closes_it_keeps_it(
    ch, org, monkeypatch
) -> None:
    from server.memory.app import entity_projections

    london = await _stated(ch, org, "s1", "My favourite city is London")
    berlin = await _stated(ch, org, "s2", "My favourite city is Berlin")
    support = await _stated(ch, org, "s3", "My favourite city is Rome", pinned=1)
    close = entity_projections.close_triples

    async def _revised_back_first(*args: Any, **kw: Any) -> Any:
        # After the follow read its plan, before its close: the row says London again.
        await _stated(ch, org, "s3", "My favourite city is London", memory_id=support["memory_id"])
        return await close(*args, **kw)

    monkeypatch.setattr(entity_projections, "close_triples", _revised_back_first)
    await _follow_retirement(ch, org, london, berlin)
    await _mutations_done(ch, org)
    want = {"london", "berlin", "rome"}
    assert await _open_cities(ch, org, want) == want
    assert await _listed(org, "favourite_city", want) == want
    # Undone in place: London's two observations, both open, and no copy of either.
    rows = await ch.query_all(
        "SELECT valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'london' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r["valid_to"] for r in rows] == [None, None]


def _today() -> datetime:
    # The day a session dates its facts by.
    return datetime.now(UTC).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)


@pytest.mark.parametrize("dated", [False, True])
async def test_a_value_stated_again_while_a_repair_closes_it_stays_current(
    ch, org, monkeypatch, dated
) -> None:
    # Back to London lands while Rome's close of London runs, and closes Rome. Dated
    # by its session day, before Rome's write, it is still the newer of the two.
    from server.memory.app import projections

    lives = ("user", "live_in", "london")
    await _stated(ch, org, "s1", "I currently live in London", lives)
    close, raced = projections.close_triples, []

    async def _back_to_london_first(*args: Any, **kw: Any) -> Any:
        # After the Rome repair read London open, before its close: London again.
        if not raced:
            raced.append(True)
            back_from = _today() if dated else None
            await _stated(ch, org, "s3", "I am back in London", lives, valid_at=back_from)
        return await close(*args, **kw)

    monkeypatch.setattr(projections, "close_triples", _back_to_london_first)
    await _stated(ch, org, "s2", "I currently live in Rome", ("user", "live_in", "rome"))
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "london")}) == {("live_in", "london")}


async def test_another_relation_to_the_same_object_does_not_gainsay_a_successor(ch, org) -> None:
    was = "I currently live in London and work in London"
    paris = await _stated(
        ch, org, "s1", "I currently live in Paris and work in London", ("user", "live_in", "paris")
    )
    london = await _stated(ch, org, "s2", was, extracted=False)
    await _follow_retirement(ch, org, paris, london)
    works = ("user", "work_in", "london")
    await _stated(ch, org, "s2", was + ".", works, memory_id=london["memory_id"])
    other = await _stated(
        ch, org, "s3", "I currently live in London", ("user", "live_in", "london")
    )
    berlin = await _stated(ch, org, "s4", "I currently live in Berlin", extracted=False)
    await _follow_retirement(ch, org, other, berlin)
    await _mutations_done(ch, org)
    # Berlin starts May 1, before the undated London seen today: the repair closes it.
    want = {("live_in", "london"), ("work_in", "london")}
    assert await _open_facts(ch, org, want) == want


async def test_a_name_in_another_script_is_carried_through_a_revision(ch, org) -> None:
    acme = await _stated(ch, org, "s1", "Bob is CEO of Acme", ("bob", "ceo_of", "acme"))
    bob = await _stated(ch, org, "s2", "Bob is CEO of Компания", extracted=False, pinned=1)
    await _follow_retirement(ch, org, acme, bob)
    await _stated(
        ch, org, "s2", "Bob is CEO of Компания!", extracted=False, memory_id=bob["memory_id"]
    )
    other = await _stated(ch, org, "s3", "Bob is CEO of Компания", ("bob", "ceo_of", "компания"))
    initech = await _stated(ch, org, "s4", "Bob is CEO of Initech", extracted=False)
    await _follow_retirement(ch, org, other, initech)
    await _mutations_done(ch, org)
    # Initech starts May 1, before the undated Компания seen today: the repair closes it.
    want = {"компания"}
    assert await _open_cities(ch, org, want) == want


async def _open_facts(ch: Any, org: str, want: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """The open (relation, object) pairs, waiting up to ten seconds for `want`."""
    for _ in range(50):
        got = await ch.query_all(
            "SELECT relation, object FROM loom.entity_triples WHERE org = {org:String}"
            " AND valid_to IS NULL SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        facts = {(r["relation"], r["object"]) for r in got}
        if facts == want:
            break
        await asyncio.sleep(0.2)
    return facts


@pytest.mark.parametrize(
    ("revised", "stated", "want"),
    [
        # The same words, and no relation saying otherwise: the successor holds, and
        # closes Berlin, which starts May 1, before the undated London seen today.
        ("I currently live in London.", (), {("live_in", "london")}),
        # More detail, which words alone cannot tell from a retraction: it closes.
        (
            "I currently live in London and work at Acme",
            (("user", "work_at", "acme"),),
            {("work_at", "acme"), ("live_in", "berlin")},
        ),
        # Names London, but its relations say Rome and a visit: the successor goes.
        (
            "I currently live in Rome and visited London",
            (("user", "live_in", "rome"), ("user", "visit", "london")),
            # Rome is closed by the later London row: `live_in` holds one value.
            {("visit", "london"), ("live_in", "berlin")},
        ),
    ],
)
async def test_a_revision_keeps_a_successor_only_while_its_relations_agree(
    ch, org, revised, stated, want
) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris", ("user", "live_in", "paris"))
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # Revised in place: London's residence is a successor the follow wrote for it.
    mid = london["memory_id"]
    if stated:
        await _stated(ch, org, "s2", revised, stated[0], also=stated[1:], memory_id=mid)
    else:
        await _stated(ch, org, "s2", revised, extracted=False, memory_id=mid)
    other = await _stated(
        ch, org, "s3", "I currently live in London", ("user", "live_in", "london")
    )
    # London's close of Rome lands first: Berlin's repair reads stored rows, and an
    # undated Rome still open would be newer than Berlin.
    await _mutations_done(ch, org)
    berlin = await _stated(ch, org, "s4", "I currently live in Berlin", extracted=False)
    await _follow_retirement(ch, org, other, berlin)
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, want) == want
    # What a reader is told: London is still where the user lives, or no longer. The
    # wait above for `valid_to IS NULL` comes first: facts.list alone still lists a
    # fact closed this very second.
    lives = {o for r, o in want if r == "live_in"}
    assert ("london" in await _listed(org, "live_in", lives)) == ("london" in lives)


async def test_a_successors_repair_reads_the_close_its_follow_queued(ch, org) -> None:
    # The follow closes London, then repairs Berlin's interval. With that close still a
    # pending mutation (as under load), London read as open is newer than Berlin,
    # dated May 1: Berlin must not close behind it.
    london = await _stated(
        ch, org, "s1", "I currently live in London", ("user", "live_in", "london")
    )
    berlin = await _stated(ch, org, "s2", "I currently live in Berlin", extracted=False)
    await ch.execute("SYSTEM STOP MERGES loom.entity_triples")  # no mutation runs
    try:
        await _follow_retirement(ch, org, london, berlin, wait=False)
    finally:
        await ch.execute("SYSTEM START MERGES loom.entity_triples")
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "berlin")}) == {("live_in", "berlin")}
    # The follow's own Berlin, current since May 1: not closed and stated again later.
    rows = await ch.query_all(
        "SELECT valid_from FROM loom.entity_triples WHERE org = {org:String}"
        " AND valid_to IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert rows == [{"valid_from": datetime(2026, 5, 1)}]


async def test_a_live_row_holds_the_successor_written_for_its_value(ch, org) -> None:
    # Its extractor names the residence another way: the follow's successor is still its.
    paris = await _stated(ch, org, "s1", "I currently live in Paris", ("user", "live_in", "paris"))
    london = await _stated(
        ch, org, "s2", "I currently live in London", ("user", "reside_in", "london")
    )
    await _follow_retirement(ch, org, paris, london)
    other = await _stated(
        ch, org, "s3", "I currently live in London", ("user", "live_in", "london")
    )
    berlin = await _stated(ch, org, "s4", "I currently live in Berlin", extracted=False)
    await _follow_retirement(ch, org, other, berlin)
    await _mutations_done(ch, org)
    # Berlin starts May 1, before the undated London seen today: the repair closes it.
    want = {("resid_in", "london"), ("live_in", "london")}
    assert await _open_facts(ch, org, want) == want


async def test_a_revision_whose_links_fail_to_write_still_holds_its_successor(
    ch, org, monkeypatch
) -> None:
    from server.core import ch as ch_mod
    from server.memory.app.projection_errors import TriplesInsertFailed

    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False, pinned=1)
    await _follow_retirement(ch, org, paris, london)

    client = ch_mod.get_client()
    insert, attempted = client.insert, []

    async def _flaky(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_triple_rows":
            attempted.append(rows)
            raise ConnectionError("transient")
        return await insert(table, rows, **kw)

    monkeypatch.setattr(client, "insert", _flaky)
    lives = ("user", "favourite_city", "london")
    # Raised with the write's arrival, for its replay to date the triples by.
    with pytest.raises(TriplesInsertFailed, match="ConnectionError"):
        await _stated(
            ch, org, "s2", "I currently live in London.", lives, memory_id=london["memory_id"]
        )
    monkeypatch.setattr(client, "insert", insert)
    assert attempted
    # Its successor was written for the value it was revised from: the same words.
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london", "rome"}) == {"london", "rome"}
    assert await _listed(org, "favourite_city", {"london", "rome"}) == {"london", "rome"}


async def test_a_replay_inserting_the_same_graph_rows_again_lands_them_once(ch, org) -> None:
    from server.memory.app.projections import entity_triple_row, graph_insert_settings

    now = naive_utc_now()
    row = entity_triple_row(
        org,
        NS,
        "user",
        "lives_in",
        "paris",
        provenance="extracted",
        confidence=1.0,
        adapter_name="t",
        last_seen=now.replace(microsecond=0),
        seen_at=now,
    )
    settings = graph_insert_settings("entity_triples", wait=True, dedup_token=f"replay:{org}")
    for _ in range(2):
        await ch.insert("loom.entity_triples", [row], settings=settings)
    (got,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert got["n"] == 1


async def test_a_replayed_insert_names_its_triples_as_the_write_resolved_them(
    ch, org, monkeypatch
) -> None:
    # London's triple lands, its key links fail; London then resolves to another name.
    # The replay must repair the London that landed, so Paris closes.
    from server.core import ch as ch_mod
    from server.memory.app import projections
    from server.memory.app.projection_errors import (
        TriplesInsertFailed,
        bulk_projection_payload,
        record_input_projection_failure,
    )
    from server.memory.app.projection_failures import retry_projection_failure

    live = ("user", "live_in")
    await _stated(ch, org, "s1", "I live in Paris", (*live, "paris"))
    _row, write, results = await _row_and_write(
        ch, org, "s2", "I live in London", [(*live, "london")], memory_key="k2"
    )
    names: dict[str, str] = {}

    async def resolve(input: Any, entity_texts: list[str], *, ch: Any) -> dict[str, str]:
        return names

    client = ch_mod.get_client()
    insert, landed = client.insert, asyncio.Event()

    async def links_fail(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_triple_memories":
            await landed.wait()
            raise ConnectionError("transient")
        out = await insert(table, rows, **kw)
        if table == "loom.entity_triples":
            landed.set()
        return out

    monkeypatch.setattr(projections, "resolve_projection_entity_map", resolve)
    monkeypatch.setattr(client, "insert", links_fail)
    with pytest.raises(TriplesInsertFailed) as raised:
        await projections.write_entity_triples(write, results, ch=ch)
    monkeypatch.setattr(client, "insert", insert)
    await record_input_projection_failure(
        write,
        projection="entity_triples",
        error=raised.value,
        payload=bulk_projection_payload(write, results, error=raised.value),
        source_operation="memory.bulk_set",
    )
    failure = await _recorded_reconcile(ch, org, "entity_triples")
    names["london"] = "london city"
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


@pytest.mark.parametrize(("copies", "want"), [(1, {"rome"}), (2, {"london", "rome"})])
async def test_a_successor_is_judged_by_an_unredacted_copy_of_its_value(
    ch, org, copies, want
) -> None:
    from server.memory import store as M
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.store.redaction import redact_row

    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # A pin or a touch writes another version with the same value and hash.
    versions = [london] + [{**london, "version": M.next_version()} for _ in range(copies - 1)]
    await M.insert_memory_versions(versions[1:], ch=ch)
    value = "I currently live in London."
    await handle_memory_update_by_id(
        MemoryUpdateByIdInput(org=org, memory_id=str(london["memory_id"]), value=value)
    )
    # The latest copy of the value the successor was written for is scrubbed; an
    # earlier copy, if any, still says it.
    when = datetime.now(UTC).replace(tzinfo=None)
    mid = str(london["memory_id"])
    await redact_row(org, mid, versions[-1]["version"], when=when, reason_blob="t", ch=ch)
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, want) == want


# --- Which rows may hold graph facts: one rule (live, not deleted, not expired, validity
# not ended, not 'high') on every path that asks it.


def _past() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)


async def test_a_winner_revised_to_high_projects_nothing_when_the_follow_replays(ch, org) -> None:
    from types import SimpleNamespace

    from server.memory import store as M
    from server.memory.app.entity_projections import follow_row_supersessions

    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    retired = {**paris, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(london["memory_id"])
    await M.insert_memory_versions([retired], ch=ch)
    # Before the follow replays, the winner is revised to a protected value.
    secret = {**london, "version": M.next_version(), "sensitivity": "high"}
    secret["value"] = '"I currently live in Secretville"'
    await M.insert_memory_versions([secret], ch=ch)
    scope = SimpleNamespace(org=org, namespace=NS)
    await follow_row_supersessions(
        scope, [(str(paris["memory_id"]), str(london["memory_id"]))], ch=ch
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


@pytest.mark.parametrize("ended", ["expires_at", "valid_to"])
async def test_an_ended_sibling_does_not_keep_a_fact_open(ch, org, ended) -> None:
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    await _stated(ch, org, "s3", "My favourite city is Paris", **{ended: _past()})
    london = await _stated(ch, org, "s2", "My favourite city is London")
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_successor_closes_the_older_value_a_sibling_still_states(ch, org) -> None:
    # Another live row still says Paris, so the follow keeps it; London is the newer
    # value of a single-valued relation and closes it, as a write of London would.
    lives, april = ("user", "lives_in", "paris"), datetime(2026, 4, 1)
    paris = await _stated(ch, org, "s1", "I currently live in Paris", lives, valid_at=april)
    await _stated(ch, org, "s3", "I currently live in Paris", lives, valid_at=april)
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_clause_the_winner_keeps_word_for_word_keeps_its_end(ch, org) -> None:
    # The winner has no link to London's fact, but still says it in the same words.
    december = naive_utc_now().replace(microsecond=0) + timedelta(days=60)
    lives = ("user", "live_in", "london")
    acme = await _stated(
        ch,
        org,
        "s1",
        "I live in London until December. I work at Acme",
        lives,
        fact_valid_to=december,
    )
    was = "I live in London until December. I work at Initech"
    initech = await _stated(ch, org, "s2", was, extracted=False)
    await _follow_retirement(ch, org, acme, initech)
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'london' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r["valid_to"] for r in rows] == [december]


async def test_an_ended_live_end_of_the_chain_holds_nothing(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(
        ch, org, "s2", "I currently live in London", extracted=False, expires_at=_past()
    )
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


async def test_a_revision_closes_the_inferred_fact_it_no_longer_states(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    await _revise("memory.update_by_id", org, london, "I currently live in Berlin")
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


async def test_a_bulk_revision_closes_the_inferred_fact_it_no_longer_states(ch, org) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s2",
            entries=[{"memory_key": KEY, "value": "I currently live in Berlin"}],
        )
    )
    assert [r.memory_id for r in out.written] == [str(london["memory_id"])]
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


async def test_undo_brings_back_the_facts_the_restored_row_holds(ch, org) -> None:
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore

    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London")
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(london["memory_id"]),
        )
    )
    assert out.restored
    assert await _open_cities(ch, org, {"london", "paris"}) == {"london", "paris"}


async def test_an_undo_leaves_the_graph_to_a_revision_made_before_its_graph_step(
    ch, org, monkeypatch
) -> None:
    # The Undo restores London and releases the key's lock; before its graph step runs,
    # the row is revised to Rome and Berlin is written. The Undo must not assert Rome.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import entity_projections
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    reconcile, revised = entity_projections.reconcile_row_facts, []

    async def revised_first(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("op") != "memory.restore" or revised:
            return await reconcile(*args, **kwargs)
        revised.append(True)
        await _revise("memory.update_by_id", org, london, "I live in Rome", ((*live, "rome"),))
        await _stated(ch, org, "s3", "I moved to Berlin", (*live, "berlin"), memory_key="k3")
        await _mutations_done(ch, org)
        await reconcile(*args, **kwargs)

    monkeypatch.setattr(entity_projections, "reconcile_row_facts", revised_first)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_an_undo_leaves_the_graph_to_a_revision_made_while_its_graph_step_reads(
    ch, org, monkeypatch
) -> None:
    # As above, but the revision lands while the graph step reads what the row holds.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    linked, revised = graph_undo.linked_triples, []

    async def revised_first(*args: Any, **kwargs: Any) -> Any:
        if not revised:
            revised.append(True)
            await _revise("memory.update_by_id", org, london, "I live in Rome", ((*live, "rome"),))
            await _stated(ch, org, "s3", "I moved to Berlin", (*live, "berlin"), memory_key="k3")
            await _mutations_done(ch, org)
        return await linked(*args, **kwargs)

    monkeypatch.setattr(graph_undo, "linked_triples", revised_first)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored and revised
    await _mutations_done(ch, org)
    # Nor does it assert the revised value at its own time: Rome was not stated then.
    undone = await ch.query_all(
        "SELECT object FROM loom.entity_triples WHERE org = {org:String} AND restore_id != ''",
        params={"org": org},
    )
    assert undone == []
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_an_undo_closes_its_markers_for_a_revision_made_after_its_hash_check(
    ch, org, monkeypatch
) -> None:
    # The revision lands after the Undo checked the row's hash, before its markers:
    # the revision's own close ran too early to see them, so the Undo closes them.
    from server.identity.app import namespace_ontology
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(ch, org, "s1", "I like London", ("user", "like", "london"))
    paris = await _stated(ch, org, "s2", "I like Paris", ("user", "like", "paris"))
    await _follow_retirement(ch, org, london, paris)
    checked, revised = graph_undo.rows_still_holding, []
    cardinality = namespace_ontology.load_relation_cardinality

    async def check(*args: Any, **kwargs: Any) -> Any:
        revised.append(False)
        return await checked(*args, **kwargs)

    async def revised_after_the_check(*args: Any, **kwargs: Any) -> Any:
        if revised == [False]:
            revised.append(True)
            await _revise(
                "memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),)
            )
            await _mutations_done(ch, org)
        return await cardinality(*args, **kwargs)

    monkeypatch.setattr(graph_undo, "rows_still_holding", check)
    monkeypatch.setattr(namespace_ontology, "load_relation_cardinality", revised_after_the_check)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored and True in revised
    await _mutations_done(ch, org)
    # The open facts are what the live rows hold: Rome and Paris, not London.
    want = {("like", "rome"), ("like", "paris")}
    assert await _open_facts(ch, org, want) == want


async def test_an_undo_closes_its_markers_for_a_retirement_made_after_its_hash_check(
    ch, org, monkeypatch
) -> None:
    # As above, but the restored London row is retired behind a Rome row: it keeps its
    # value, so only its status says it no longer holds London.
    from server.identity.app import namespace_ontology
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(ch, org, "s1", "I like London", ("user", "like", "london"))
    paris = await _stated(ch, org, "s2", "I like Paris", ("user", "like", "paris"))
    await _follow_retirement(ch, org, london, paris)
    checked, retired = graph_undo.rows_still_holding, []
    cardinality = namespace_ontology.load_relation_cardinality

    async def check(*args: Any, **kwargs: Any) -> Any:
        retired.append(False)
        return await checked(*args, **kwargs)

    async def retired_after_the_check(*args: Any, **kwargs: Any) -> Any:
        if retired == [False]:
            retired.append(True)
            rome = await _stated(
                ch, org, "s3", "I like Rome", ("user", "like", "rome"), memory_key="k3"
            )
            await _follow_retirement(ch, org, london, rome)
        return await cardinality(*args, **kwargs)

    monkeypatch.setattr(graph_undo, "rows_still_holding", check)
    monkeypatch.setattr(namespace_ontology, "load_relation_cardinality", retired_after_the_check)
    assert (await handle_memory_restore(_undo_of(paris))).restored and True in retired
    await _mutations_done(ch, org)
    want = {("like", "rome"), ("like", "paris")}
    assert await _open_facts(ch, org, want) == want


async def test_a_replayed_undo_closes_its_markers_for_a_revision_made_during_it(
    ch, org, monkeypatch
) -> None:
    # As above, but the Undo's own close fails: its replay finds the revised row no
    # longer holding the restored value, and must still close London's marker.
    from server.identity.app import namespace_ontology
    from server.memory.app import graph_undo
    from server.memory.app.projection_failures import retry_projection_failure
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(ch, org, "s1", "I like London", ("user", "like", "london"))
    paris = await _stated(ch, org, "s2", "I like Paris", ("user", "like", "paris"))
    await _follow_retirement(ch, org, london, paris)
    checked, closed, revised = graph_undo.rows_still_holding, graph_undo.close_dropped_facts, []
    cardinality = namespace_ontology.load_relation_cardinality

    async def check(*args: Any, **kwargs: Any) -> Any:
        revised.append(False)
        return await checked(*args, **kwargs)

    async def revised_after_the_check(*args: Any, **kwargs: Any) -> Any:
        if revised == [False]:
            revised.append(True)
            await _revise(
                "memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),)
            )
            await _mutations_done(ch, org)
        return await cardinality(*args, **kwargs)

    async def close_fails(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("close down")

    monkeypatch.setattr(graph_undo, "rows_still_holding", check)
    monkeypatch.setattr(graph_undo, "close_dropped_facts", close_fails)
    monkeypatch.setattr(namespace_ontology, "load_relation_cardinality", revised_after_the_check)
    assert (await handle_memory_restore(_undo_of(paris))).restored and True in revised
    monkeypatch.setattr(graph_undo, "close_dropped_facts", closed)
    assert (await retry_projection_failure(await _recorded_reconcile(ch, org), ch=ch)).resolved
    await _mutations_done(ch, org)
    want = {("like", "rome"), ("like", "paris")}
    assert await _open_facts(ch, org, want) == want


async def test_undo_makes_the_restored_value_current_and_retires_nothing(
    ch, org, monkeypatch
) -> None:
    # An Undo is the newest assertion: the restored London is the current home, and
    # Paris's fact closes. The Paris row stays live: an Undo never retires a row.
    from types import SimpleNamespace

    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(
        ch, org, "s1", "I currently live in London", ("user", "live_in", "london")
    )
    paris = await _stated(ch, org, "s2", "I currently live in Paris", ("user", "live_in", "paris"))
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    # Undone in the second Paris was written, after it: the Undo is the newer.
    restored_at = await _undo_in_the_second_of(ch, org, "paris", monkeypatch)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    # The edge plane agrees: London's edge is the one current edge of the user.
    assert await _open_edges(ch, org, {"london"}) == ["london"]
    # The retirement closed London's own rows, so the Undo's open rows carry its
    # weight: London is current for the readers on either plane, Paris is not.
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    assert await _open_weights(ch, org, "paris") == {"triples": 0, "edges": 0}
    assert await _walked(org, ["london"], labeled=True) == ["london"]
    assert await _walked(org, ["london"], labeled=False) == ["london"]
    # A replay of the Undo finds its observations and weighs London no more.
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restored_at=restored_at,
        restore_id=await _restore_id(ch, org),
    )
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    (row,) = await ch.query_all(
        "SELECT status FROM loom.memory FINAL WHERE org = {org:String} AND memory_id = {id:UUID}",
        params={"org": org, "id": str(paris["memory_id"])},
    )
    assert row["status"] == "active"


async def _open_weights(ch: Any, org: str, city: str) -> dict[str, int]:
    """The summed weight of `city`'s rows the graph's readers count as current from the
    next second on (they count a row closed this second too), on the triple and the edge
    plane."""
    weights = await ch.query_all(
        "SELECT 'triples' AS plane, sum(co_occur) AS n FROM loom.entity_triples"
        " WHERE org = {org:String} AND object = {city:String}"
        " AND (valid_to IS NULL OR valid_to > now())"
        " UNION ALL SELECT 'edges', sum(co_occur) FROM loom.entity_edges"
        " WHERE org = {org:String} AND entity_a = {city:String}"
        " AND (valid_to IS NULL OR valid_to > now()) AND invalid_at IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "city": city},
    )
    return {str(w["plane"]): int(w["n"]) for w in weights}


async def _walked(
    org: str,
    want: list[str],
    *,
    labeled: bool,
    as_of: datetime | None = None,
    seed: str = "user",
    until: Callable[[list[str]], bool] | None = None,
) -> list[str]:
    """The entities one hop of `facts.traverse` from `seed` reaches (at `as_of`, when
    given), waiting up to two seconds for `want` (or for a walk `until` accepts): it
    counts a fact closed this very second as current."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app.facts import handle_facts_traverse

    walk = FactsTraverseInput(
        org=org, namespace=NS, seed_entity=seed, max_hops=1, labeled=labeled, as_of=as_of
    )
    for _ in range(10):
        out = await handle_facts_traverse(walk)
        walked = sorted(n.entity for n in out.nodes)
        if until(walked) if until else walked == want:
            break
        await asyncio.sleep(0.2)
    return walked


@pytest.mark.parametrize("late", [True, False])
async def test_a_projection_landing_after_a_revision_leaves_no_edge(
    ch, org, monkeypatch, late
) -> None:
    # The first write's edges are written beside its response; held back past the
    # revision (`late`), they must not walk once they land.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, entity_projections
    from server.memory.app.write import handle_memory_set

    write_edges, release = entity_projections.write_entity_edges, asyncio.Event()
    calls: list[str] = []

    async def _first_held(*args: Any, **kw: Any) -> Any:
        calls.append("edges")
        if late and len(calls) == 1:
            await release.wait()
        return await write_edges(*args, **kw)

    monkeypatch.setattr(entity_projections, "write_entity_edges", _first_held)

    def _set(value: str, relations: list[Any]) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                value=value,
                relations=relations,
            )
        )

    await _set("Alice lives in London", [("alice", "live_in", "london")])
    if not late:
        await after_write.drain()
    await _set("Alice likes jazz", [])
    release.set()
    await after_write.drain()
    await _mutations_done(ch, org)
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "london" not in w
    )
    assert "jazz" in walked and "london" not in walked


# Episodic and profile rows are not embedded: their write takes the plain insert path.
@pytest.mark.parametrize("memory_type", ["semantic", "episodic", "profile"])
async def test_a_first_write_paused_past_a_conditional_revision_leaves_no_fact(
    ch, org, monkeypatch, memory_type
) -> None:
    # London's first write stops before its edges; a conditional revision to jazz
    # completes; then London resumes. Its edges and its stated relation must not walk.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, entity_projections
    from server.memory.app.write import handle_memory_set

    write_edges, release, held = entity_projections.write_entity_edges, asyncio.Event(), []

    async def _london_held(input: Any, *args: Any, **kw: Any) -> Any:
        if "London" in str(input.entries[0].value):
            held.append(True)
            await release.wait()
        return await write_edges(input, *args, **kw)

    monkeypatch.setattr(entity_projections, "write_entity_edges", _london_held)

    def _set(value: str, relations: list[Any], **extra: Any) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                memory_type=memory_type,
                value=value,
                relations=relations,
                **extra,
            )
        )

    london = asyncio.create_task(
        _set(
            "Alice lives in London", [("alice", "live_in", "london")], entities=["alice", "london"]
        )
    )
    for _ in range(100):
        if held:
            break
        await asyncio.sleep(0.05)
    assert held
    (row,) = await ch.query_all(
        "SELECT toString(content_sha256) AS sha FROM loom.memory FINAL"
        " WHERE org = {org:String} AND memory_key = 'alice'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    jazz = await _set(
        "Alice likes jazz", [], entities=["alice", "jazz"], if_match_sha256=row["sha"]
    )
    release.set()
    await london
    await after_write.drain()
    await _mutations_done(ch, org)
    (stored,) = await ch.query_all(
        "SELECT toString(content_sha256) AS sha FROM loom.memory FINAL"
        " WHERE org = {org:String} AND memory_key = 'alice'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert stored["sha"] == jazz.content_sha256
    assert await _walked(org, [], labeled=True, seed="alice") == []
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "london" not in w
    )
    assert "jazz" in walked and "london" not in walked


async def _london_ends_with_the_next_revision(
    ch: Any,
    org: str,
    monkeypatch: pytest.MonkeyPatch,
    owner: Any,
    to_london: Callable[[], Any],
    to_jazz: Callable[[], Any],
) -> None:
    """`to_london()` revises the row to London with its edges held back (`owner`'s
    `write_entity_edges`) while `to_jazz()` revises it again, whose close runs before
    London's edges exist. Once they land they end when jazz ended London, not when
    the London write's own cleanup ran."""
    from server.memory.app import after_write

    write_edges, release, held = owner.write_entity_edges, asyncio.Event(), []

    async def _london_held(input: Any, *args: Any, **kw: Any) -> Any:
        if "London" in str(input.entries[0].value):
            held.append(True)
            await release.wait()
        return await write_edges(input, *args, **kw)

    monkeypatch.setattr(owner, "write_entity_edges", _london_held)
    writer_a = asyncio.create_task(to_london())
    for _ in range(100):
        if held:
            break
        await asyncio.sleep(0.05)
    assert held
    await to_jazz()
    revised = naive_utc_now()
    await asyncio.sleep(2.5)
    release.set()
    await writer_a
    await after_write.drain()
    await _mutations_done(ch, org)
    walked = await _walked(
        org,
        [],
        labeled=False,
        as_of=revised + timedelta(seconds=1.2),
        seed="alice",
        until=lambda w: "jazz" in w and "london" not in w,
    )
    assert "jazz" in walked and "london" not in walked


# A profile row is not embedded: its write takes the plain insert path.
@pytest.mark.parametrize("memory_type", ["semantic", "profile"])
async def test_a_revisions_edges_landing_after_the_next_revision_end_with_it(
    ch, org, monkeypatch, memory_type
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, entity_projections
    from server.memory.app.write import handle_memory_set

    def _set(value: str, relations: list[Any]) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                memory_type=memory_type,
                value=value,
                relations=relations,
            )
        )

    await _set("Alice lives in Paris", [("alice", "live_in", "paris")])
    await after_write.drain()
    await _london_ends_with_the_next_revision(
        ch,
        org,
        monkeypatch,
        entity_projections,
        lambda: _set("Alice lives in London", [("alice", "live_in", "london")]),
        lambda: _set("Alice likes jazz", []),
    )


async def test_an_update_by_ids_edges_landing_after_the_next_revision_end_with_it(
    ch, org, monkeypatch
) -> None:
    # As above, through memory.update_by_id.
    from server.memory.api.models_crud import MemorySetInput, MemoryUpdateByIdInput
    from server.memory.app import entity_projections
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.app.write import handle_memory_set

    first = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            memory_key="alice",
            value="Alice lives in Paris",
            relations=[("alice", "live_in", "paris")],
        )
    )

    def _update(value: str, relations: list[Any]) -> Any:
        return handle_memory_update_by_id(
            MemoryUpdateByIdInput(
                org=org, memory_id=str(first.memory_id), value=value, relations=relations
            )
        )

    await _london_ends_with_the_next_revision(
        ch,
        org,
        monkeypatch,
        entity_projections,
        lambda: _update("Alice lives in London", [("alice", "live_in", "london")]),
        lambda: _update("Alice likes jazz", []),
    )


async def test_a_new_key_written_twice_in_a_bulk_keeps_one_row_and_one_edge(ch, org) -> None:
    # A key with no row yet, written twice in one bulk: one row, revised in the batch,
    # and the first value's edge ends with it.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, bulk_set

    def _entry(city: str) -> dict[str, Any]:
        return {
            "memory_key": "alice",
            "value": f"Alice likes {city}",
            "entities": ["alice", city.lower()],
        }

    out = await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            entries=[_entry("London"), _entry("Rome")],
        )
    )
    await after_write.drain()
    assert len({r.memory_id for r in out.written if r.version}) == 1
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # past the second the edge ended in
    walked = await _walked(org, ["rome"], labeled=False, seed="alice", until=lambda w: "rome" in w)
    assert "rome" in walked and "london" not in walked


async def test_an_overtaken_version_ends_its_edge_though_its_row_holds_another(ch, org) -> None:
    # A bulk can revise one row twice: asked about both versions while the row holds
    # the second, the check must still end the first's edge (it asks by value, not by row).
    from server.memory import store as M
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.entity_projections import end_overtaken_edges
    from server.memory.app.write import handle_memory_set

    out = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            memory_key="alice",
            value="Alice likes London",
            entities=["alice", "london"],
        )
    )
    await after_write.drain()
    london = await M.get_by_id(str(out.memory_id), org=org, ch=ch, scope=WHOLE_NAMESPACE)
    rome = M.build_memory_version_row(
        london,
        version=M.next_version(),
        value=json.dumps("Alice likes Rome"),
        content_sha256=hashlib.sha256(json.dumps("Alice likes Rome").encode()).hexdigest(),
        updated_at=naive_utc_now(),
    )
    await M.insert_memory_versions([rome], ch=ch)
    assert "london" in await _walked(org, ["london"], labeled=False, seed="alice")
    scope = london.model_copy()
    versions = [
        (str(london.memory_id), "alice", london.content_sha256),
        (str(london.memory_id), "alice", rome["content_sha256"]),
    ]
    await end_overtaken_edges(scope, versions, op="memory.bulk_set", ch=ch)
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # past the second the edge ended in
    assert "london" not in await _walked(org, [], labeled=False, seed="alice")


async def test_a_bulk_revisions_edges_landing_after_the_next_revision_end_with_it(
    ch, org, monkeypatch
) -> None:
    # As above, through memory.bulk_set.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import bulk_set

    def _set(value: str, city: str) -> Any:
        return bulk_set.handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                entries=[
                    {
                        "memory_key": "alice",
                        "value": value,
                        "entities": ["alice", city],
                        "relations": [("alice", "likes", city)],
                    }
                ],
            )
        )

    await _set("Alice likes Paris", "paris")
    await _london_ends_with_the_next_revision(
        ch,
        org,
        monkeypatch,
        bulk_set,
        lambda: _set("Alice likes London", "london"),
        lambda: _set("Alice likes jazz", "jazz"),
    )


async def test_a_late_projection_whose_cleanup_failed_is_closed_by_the_repair(
    ch, org, monkeypatch
) -> None:
    # As above with the edges landing late, but the cleanup that ends them fails
    # once: it is recorded, and the projection repair ends them.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, entity_projections
    from server.memory.app.projection_failures import (
        list_projection_failures,
        retry_projection_failures,
    )
    from server.memory.app.write import handle_memory_set

    write_edges, release = entity_projections.write_entity_edges, asyncio.Event()
    end_edges_of, failures = entity_projections._end_edges_of, ["once"]
    calls: list[str] = []

    async def _held(*args: Any, **kw: Any) -> Any:
        calls.append("edges")
        if len(calls) == 1:
            await release.wait()
        return await write_edges(*args, **kw)

    async def _fails_once(*args: Any, **kw: Any) -> Any:
        if failures:
            failures.pop()
            raise RuntimeError("transient")
        return await end_edges_of(*args, **kw)

    monkeypatch.setattr(entity_projections, "write_entity_edges", _held)
    monkeypatch.setattr(entity_projections, "_end_edges_of", _fails_once)

    def _set(value: str, relations: list[Any]) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                value=value,
                relations=relations,
            )
        )

    await _set("Alice lives in London", [("alice", "live_in", "london")])
    await _set("Alice likes jazz", [])
    release.set()
    await after_write.drain()
    (failure,) = await list_projection_failures(
        org=org, projection="entity_edges_close", status="active", ch=ch
    )
    await _mutations_done(ch, org)
    assert "london" in await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "london" in w
    )
    await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    await _mutations_done(ch, org)
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "london" not in w
    )
    assert "jazz" in walked and "london" not in walked
    assert not await list_projection_failures(
        org=org, projection="entity_edges_close", status="active", ch=ch
    )


@pytest.mark.parametrize("cleanup", ["beside the write", "by the repair"])
async def test_a_late_projections_edge_ends_when_the_revision_ended_its_value(
    ch, org, monkeypatch, cleanup
) -> None:
    # London's edges land two seconds after the revision to jazz. Their cleanup, or
    # its replay, must end them at the revision, not when it runs: a walk as of a
    # moment in between must not reach London.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, entity_projections
    from server.memory.app.projection_failures import retry_projection_failures
    from server.memory.app.write import handle_memory_set

    write_edges, release = entity_projections.write_entity_edges, asyncio.Event()
    end_edges_of = entity_projections._end_edges_of
    failures = ["once"] if cleanup == "by the repair" else []
    calls: list[str] = []

    async def _held(*args: Any, **kw: Any) -> Any:
        calls.append("edges")
        if len(calls) == 1:
            await release.wait()
        return await write_edges(*args, **kw)

    async def _fails_once(*args: Any, **kw: Any) -> Any:
        if failures:
            failures.pop()
            raise RuntimeError("transient")
        return await end_edges_of(*args, **kw)

    monkeypatch.setattr(entity_projections, "write_entity_edges", _held)
    monkeypatch.setattr(entity_projections, "_end_edges_of", _fails_once)

    def _set(value: str, relations: list[Any]) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                value=value,
                relations=relations,
            )
        )

    await _set("Alice lives in London", [("alice", "live_in", "london")])
    await _set("Alice likes jazz", [])
    revised = naive_utc_now()
    await asyncio.sleep(2.5)
    release.set()
    await after_write.drain()
    if cleanup == "by the repair":
        await asyncio.sleep(1)
        await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    await _mutations_done(ch, org)
    between = revised + timedelta(seconds=1.2)
    walked = await _walked(
        org,
        [],
        labeled=False,
        as_of=between,
        seed="alice",
        until=lambda w: "jazz" in w and "london" not in w,
    )
    assert "jazz" in walked and "london" not in walked
    # Jazz states no relation: the labeled walk reaches nothing then.
    assert await _walked(org, [], labeled=True, as_of=between, seed="alice") == []
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "london" not in w
    )
    assert "jazz" in walked and "london" not in walked


@pytest.mark.parametrize("cleanup", ["beside the write", "by the repair"])
async def test_a_late_projections_edge_ends_when_a_bulk_retired_its_row(
    ch, org, monkeypatch, cleanup
) -> None:
    # The retirement-history case of the test above: London's edges land after a
    # later bulk write under another session retired its row. A retirement keeps the
    # row's updated_at, so the edges end at the retirement, not at London's write: a
    # walk between the two still reaches London, a walk after it does not.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, bulk_set, conflicts, entity_projections
    from server.memory.app.projection_failures import (
        list_projection_failures,
        retry_projection_failures,
    )
    from server.memory.app.write import handle_memory_set

    write_edges, release, held = entity_projections.write_entity_edges, asyncio.Event(), []
    end_edges_of = entity_projections._end_edges_of
    failures = ["once"] if cleanup == "by the repair" else []

    async def _london_held(input: Any, *args: Any, **kw: Any) -> Any:
        if "London" in str(input.entries[0].value):
            held.append(True)
            await release.wait()
        return await write_edges(input, *args, **kw)

    async def _fails_once(*args: Any, **kw: Any) -> Any:
        if failures:
            failures.pop()
            raise RuntimeError("transient")
        return await end_edges_of(*args, **kw)

    async def _newest_wins(org: str, namespace: str, ch: Any = None) -> str:
        return "newest_wins"  # a later write to the key retires the older rows

    monkeypatch.setattr(entity_projections, "write_entity_edges", _london_held)
    monkeypatch.setattr(entity_projections, "_end_edges_of", _fails_once)
    monkeypatch.setattr(conflicts, "namespace_conflict_policy", _newest_wins)
    written = naive_utc_now()
    london = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="alice",
            value="Alice lives in London",
            entities=["alice", "london"],
        )
    )
    await asyncio.sleep(2.5)  # the retirement well after London's write
    await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s2",
            entries=[
                {"memory_key": "alice", "value": "Alice likes jazz", "entities": ["alice", "jazz"]}
            ],
        )
    )
    (retired,) = await ch.query_all(
        "SELECT invalid_at FROM loom.memory FINAL WHERE org = {org:String}"
        " AND memory_id = {id:UUID} AND status = 'superseded'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": str(london.memory_id)},
    )
    await asyncio.sleep(2.5)  # London's edges land after the retirement
    assert held
    release.set()
    await after_write.drain()
    if cleanup == "by the repair":
        (failure,) = await list_projection_failures(
            org=org, projection="entity_edges_close", status="active", ch=ch
        )
        assert failure.payload["ended_at"] == retired["invalid_at"].isoformat()
        await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    await _mutations_done(ch, org)
    before = written + timedelta(seconds=1.2)
    assert "london" in await _walked(
        org, [], labeled=False, as_of=before, seed="alice", until=lambda w: "london" in w
    )
    after = retired["invalid_at"] + timedelta(seconds=1.2)
    walked = await _walked(
        org, [], labeled=False, as_of=after, seed="alice", until=lambda w: "london" not in w
    )
    assert "london" not in walked


async def test_a_late_cleanup_keeps_the_end_another_row_gave_the_pair(ch, org) -> None:
    # Alice and London have two edge rows: one another row closed ten minutes after
    # the revision, and the late projection's own open one. The late cleanup ends
    # the open one at the revision and leaves the closed one's end alone.
    from server.memory.app.entity_projections import close_late_edges
    from server.memory.app.projections import entity_edge_row

    revised = naive_utc_now().replace(microsecond=0) - timedelta(hours=1)
    started = revised - timedelta(hours=1)
    other_end = revised + timedelta(minutes=10)
    closed = entity_edge_row(
        org, NS, "alice", "london", last_seen=started, valid_from=started, valid_to=other_end
    )
    late = entity_edge_row(org, NS, "alice", "london", last_seen=started, valid_from=started)
    await ch.insert("loom.entity_edges", [closed, late], settings={"wait_for_async_insert": 1})
    memory_id, sha = str(uuid.uuid4()), "a" * 64
    link = {
        "org": org,
        "namespace": NS,
        "memory_id": memory_id,
        "user_id": USER,
        "content_sha256": sha,
        "entity_a": "alice",
        "entity_b": "london",
    }
    await ch.insert("loom.entity_edge_rows", [link], settings={"wait_for_async_insert": 1})
    await close_late_edges(org, NS, memory_id, sha, ended_at=revised, ch=ch)
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " ORDER BY valid_to SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r["valid_to"] for r in rows] == [revised, other_end]


@pytest.mark.parametrize(
    "values",
    [
        ["Alice lives in London", "Alice likes jazz"],
        ["Alice lives in London", "Alice likes jazz", "Alice lives in London", "Alice likes blues"],
    ],
    ids=["once", "held-again"],
)
async def test_a_value_ends_when_the_row_last_stopped_holding_it(ch, org, values) -> None:
    # A row held London, then other values, maybe London again in between: its
    # London ended with the last revision away from it, not the first.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.entity_projections import value_ended_at
    from server.memory.app.write import handle_memory_set

    for i, value in enumerate(values):
        if i:
            await asyncio.sleep(1.1)  # one version per second of updated_at
        out = await handle_memory_set(
            MemorySetInput(
                org=org, namespace=NS, agent="bot", user_id=USER, memory_key="alice", value=value
            )
        )
    await after_write.drain()
    memory_id = str(out.memory_id)
    versions = await ch.query_all(
        "SELECT content_sha256, updated_at FROM loom.memory_versions"
        " WHERE org = {org:String} AND memory_id = {id:UUID} ORDER BY version"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": memory_id},
    )
    assert len(versions) == len(values)
    london = versions[0]["content_sha256"].decode()
    ended = await value_ended_at(org, NS, memory_id, london, ch=ch)
    assert ended == versions[-1]["updated_at"]


async def test_a_value_a_retirement_ended_ends_at_the_retirement(ch, org) -> None:
    # A retirement keeps the row's updated_at and stamps invalid_at: the value ended
    # when it was retired, not when it was written.
    from server.memory import store as M
    from server.memory.app.entity_projections import value_ended_at

    row = _row(org, "s1", "User lives in London", None)
    await M.insert_memory_versions([row], ch=ch)
    await asyncio.sleep(1.1)  # the retirement a second after the write
    assert await M.supersede_duplicates(
        org, [{**row, "superseded_by": "winner"}], ch=ch, strong=True
    )
    versions = await ch.query_all(
        "SELECT updated_at, invalid_at FROM loom.memory_versions"
        " WHERE org = {org:String} AND memory_id = {id:UUID} ORDER BY version"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": str(row["memory_id"])},
    )
    written, retired = versions[0]["updated_at"], versions[-1]["invalid_at"]
    assert retired is not None and retired > written
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended == retired


async def test_a_value_an_undated_value_update_ended_ends_at_the_retirement(ch, org) -> None:
    # A value update whose newer row has no valid_at retires the older row without an
    # invalid_at, keeping its updated_at: the value ended when it was retired.
    from server.memory import store as M
    from server.memory.app.entity_projections import value_ended_at
    from server.memory.app.value_updates import _supersession_update

    row = _row(org, "s1", "price is $10", None)
    await M.insert_memory_versions([row], ch=ch)
    await asyncio.sleep(1.1)  # the retirement a second after the write
    retiring = naive_utc_now().replace(microsecond=0)
    update = _supersession_update("newer", None)
    assert await M.write_versions_by_ids(org, [str(row["memory_id"])], update, ch=ch, strong=True)
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended >= retiring > row["updated_at"]


async def test_a_value_a_back_dated_value_update_ended_ends_at_its_date(ch, org) -> None:
    # A value update retires the older row as of the newer row's valid_at, which is
    # usually before the retirement was written: that date is the value's end.
    from server.memory import store as M
    from server.memory.app.entity_projections import value_ended_at
    from server.memory.app.value_updates import _supersession_update

    row = _row(org, "s1", "price is $10", datetime(2026, 1, 1))
    await M.insert_memory_versions([row], ch=ch)
    newer_valid_at = datetime(2026, 3, 1)
    update = _supersession_update("newer", newer_valid_at)
    assert await M.write_versions_by_ids(org, [str(row["memory_id"])], update, ch=ch, strong=True)
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended == newer_valid_at


async def test_a_value_a_by_id_tombstone_ended_ends_at_the_tombstone(ch, org) -> None:
    # A by-id tombstone (a conflict's losing side, a blacklist sweep) keeps the row's
    # updated_at and stamps only tombstoned_at.
    from server.memory import store as M
    from server.memory.app.entity_projections import value_ended_at

    row = _row(org, "s1", "User lives in London", None)
    await M.insert_memory_versions([row], ch=ch)
    await asyncio.sleep(1.1)  # the tombstone a second after the write
    when = naive_utc_now().replace(microsecond=0)
    assert await M.tombstone_cascade_by_ids(org, [str(row["memory_id"])], when=when, ch=ch)
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended == when


async def _restored_dependent(ch: Any, org: str) -> dict[str, Any]:
    """A summary tombstoned with its last source (tombstoned_at and invalid_at both
    stamped), then restored."""
    from server.memory import store as M
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore

    row = _row(org, "s1", "User lives in London", None, tags=["consolidated", "src:gone"])
    await M.insert_memory_versions([row], ch=ch)
    assert await M.downgrade_consolidated_dependents(org, NS, "gone", ch=ch) == (1, 0)
    restore = MemoryRestoreInput(
        org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", memory_key=KEY
    )
    assert (await handle_memory_restore(restore)).restored
    return row


async def test_a_restored_tombstone_is_not_left_retired(ch, org) -> None:
    from server.memory import store as M

    row = await _restored_dependent(ch, org)
    head = await M.get_by_id(
        str(row["memory_id"]), org=org, ch=ch, strong=True, scope=WHOLE_NAMESPACE
    )
    assert head.tombstoned_at is None and head.invalid_at is None


async def test_a_value_revised_after_a_restore_ends_at_the_revision(ch, org) -> None:
    # The tombstone's invalid_at must not date the end of a value the row held again.
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.entity_projections import value_ended_at
    from server.memory.app.update import handle_memory_update_by_id

    row = await _restored_dependent(ch, org)
    await asyncio.sleep(1.1)  # the revision a second after the tombstone
    revised = await handle_memory_update_by_id(
        MemoryUpdateByIdInput(org=org, memory_id=str(row["memory_id"]), value="User lives in Rome")
    )
    assert revised.updated
    (version,) = await ch.query_all(
        "SELECT updated_at FROM loom.memory_versions"
        " WHERE org = {org:String} AND version = {v:UInt64}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "v": revised.new_version},
    )
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended == version["updated_at"]


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"proposed": True},
        {"valid_at": datetime(2019, 1, 1), "valid_to": datetime(2020, 1, 1)},
    ],
    ids=["plain", "proposed", "closed-in-the-past"],
)
async def test_a_first_write_still_holding_its_value_projects_its_edge(ch, org, extra) -> None:
    # The fence asks only whether the row still holds this value: a proposal
    # or a fact dated in the past gets its edge, as an active row does.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.write import handle_memory_set

    out = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            memory_key="alice",
            value="Alice met Bob in Paris",
            **extra,
        )
    )
    await after_write.drain()
    rows = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_edge_rows "
        "WHERE org = {org:String} AND memory_id = {id:UUID} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": str(out.memory_id)},
    )
    assert int(rows[0]["n"]) > 0


async def test_a_plain_first_write_projects_its_edge_fenced_on_its_value(
    ch, org, monkeypatch
) -> None:
    # A profile row takes the plain insert path. Its first write's edges land beside
    # the response, flushed, and fenced as a semantic row's are: written only once the
    # row is asked whether it still holds the value.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, entity_projections
    from server.memory.app.write import handle_memory_set

    rows_holding, asked = entity_projections.rows_holding, []

    async def _counted(*a: Any, **k: Any) -> Any:
        asked.append(True)
        return await rows_holding(*a, **k)

    monkeypatch.setattr(entity_projections, "rows_holding", _counted)
    out = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            memory_key="alice",
            memory_type="profile",
            value="Alice met Bob in Paris",
        )
    )
    await after_write.drain()
    rows = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_edge_rows "
        "WHERE org = {org:String} AND memory_id = {id:UUID} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": str(out.memory_id)},
    )
    assert int(rows[0]["n"]) > 0
    assert asked


async def test_an_undo_keeps_its_edge_open_past_another_relations_end(ch, org) -> None:
    # The pair's latest edge is a bounded work fact; the restored London is open-ended,
    # and so is its edge: past the work fact's end both walks still reach London.
    from server.memory.app.restore import handle_memory_restore

    until = naive_utc_now() + timedelta(days=17)
    london = await _stated(ch, org, "s1", "I live in London", ("user", "live_in", "london"))
    paris = await _stated(ch, org, "s2", "I live in Paris", ("user", "live_in", "paris"))
    work = ("user", "work_in", "london")
    await _stated(ch, org, "s3", "I work in London", work, memory_key="job", fact_valid_to=until)
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    later = until + timedelta(days=30)
    assert await _walked(org, ["london"], labeled=True, as_of=later) == ["london"]
    assert await _walked(org, ["london"], labeled=False, as_of=later) == ["london"]


def _date_undos_at(monkeypatch: Any, clock: Any) -> None:
    """Date the next Undo by `clock`: the restore reads its own clock, and a direct
    reconcile reads the projections'."""
    from server.memory.app import entity_projections, restore

    monkeypatch.setattr(restore, "naive_utc_now", clock)
    monkeypatch.setattr(entity_projections, "naive_utc_now", clock)


async def _undo_in_the_second_of(ch: Any, org: str, city: str, monkeypatch: Any) -> datetime:
    """Date the next Undo a microsecond after `city`'s fact was last seen (in its second,
    but for the last microsecond of one), and return it."""

    (row,) = await ch.query_all(
        "SELECT max(seen_at) AS seen FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = {city:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org, "city": city},
    )
    at = row["seen"] + timedelta(microseconds=1)
    _date_undos_at(monkeypatch, lambda: at)
    return at


async def test_an_undo_weighs_its_fact_after_the_retirements_closes_land(ch, org) -> None:
    # The retirement's closes are mutations. Held back (merges stopped), London's old
    # row is still stored open when the Undo runs; the Undo reads with the pending
    # closes applied, so its own row carries London's weight once they land.
    from types import SimpleNamespace

    from server.memory import store as M
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.entity_projections import follow_row_supersessions
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(ch, org, "s1", "My favourite city is London")
    paris = await _stated(ch, org, "s2", "My favourite city is Paris")
    await _edges_like_triples(ch, org)
    retired = {**london, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(paris["memory_id"])
    await M.insert_memory_versions([retired], ch=ch)
    # Merges stop on shared tables: only from the retirement's closes to the Undo.
    tables = ("loom.entity_triples", "loom.entity_edges")
    for table in tables:
        await ch.execute(f"SYSTEM STOP MERGES {table}")
    try:
        await follow_row_supersessions(
            SimpleNamespace(org=org, namespace=NS),
            [(str(london["memory_id"]), retired["superseded_by"])],
            ch=ch,
        )
        (pending,) = await ch.query_all(
            "SELECT count() AS n FROM system.mutations WHERE database = 'loom'"
            " AND table = 'entity_triples' AND NOT is_done"
            " AND position(command, {org:String}) > 0",
            params={"org": org},
        )
        assert pending["n"] > 0, "the close landed before the Undo: nothing is tested"
        out = await handle_memory_restore(
            MemoryRestoreInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id="s1",
                memory_key=KEY,
                if_superseded_by=str(paris["memory_id"]),
            )
        )
    finally:
        for table in tables:
            await ch.execute(f"SYSTEM START MERGES {table}")
    assert out.restored
    await _mutations_done(ch, org)
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}


@contextlib.asynccontextmanager
async def _another_orgs_erasure_pending(ch: Any) -> Any:
    """A user erasure in another org deletes with a subquery; while it is pending,
    ClickHouse refuses every on-the-fly read of the table. Merges are held so it stays
    pending, and on the way out they restart and the mutation is killed."""
    other = unique_org("same-key-erasure")
    tables = ("loom.entity_triples", "loom.entity_edges")
    for table in tables:
        await ch.execute(f"SYSTEM STOP MERGES {table}")
    try:
        for table in tables:
            # Matches nothing: only its subquery matters.
            await ch.execute(
                f"ALTER TABLE {table} DELETE WHERE org = '{other}'"
                f" AND namespace IN (SELECT namespace FROM loom.entity_triple_memories"
                f" WHERE org = '{other}')"
            )
        yield
    finally:
        for table in tables:
            await ch.execute(f"SYSTEM START MERGES {table}")
        await _mutations_done(ch, other, kill_after=0)


async def _restored_markers(ch: Any, org: str) -> int:
    (row,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return int(row["n"])


async def test_an_undo_waits_for_another_orgs_subquery_mutation_to_reach_the_graph(ch, org) -> None:
    # While the erasure is pending the Undo restores the row and writes nothing to
    # the graph: the failure is recorded, and its replay makes London current, weighed
    # once, on both planes.
    from server.memory.app.projection_failures import retry_projection_failure
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    async with _another_orgs_erasure_pending(ch):
        out = await handle_memory_restore(_undo_of(paris))
        failure = await _recorded_reconcile(ch, org)
        assert await _restored_markers(ch, org) == 0
    assert out.restored
    assert failure.payload["memory_ids"] == [str(london["memory_id"])]
    assert failure.payload["restored_at"] and failure.payload["content_sha256"]
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    assert await _open_edges(ch, org, {"london"}) == ["london"]
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    assert await _walked(org, ["london"], labeled=True) == ["london"]
    assert await _walked(org, ["london"], labeled=False) == ["london"]


async def test_a_deferred_undo_adds_no_weight_to_a_fact_another_live_row_holds(ch, org) -> None:
    # Two live rows say London (weight 2). An Undo of one, deferred by another org's
    # erasure and replayed after it, leaves London's weight at 2.
    from types import SimpleNamespace

    from server.memory.app.entity_projections import reconcile_row_facts
    from server.memory.app.projection_failures import retry_projection_failure

    london = await _stated(ch, org, "s1", "My favourite city is London")
    await _stated(ch, org, "s3", "My favourite city is London", memory_key="k3")
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    assert await _open_weights(ch, org, "london") == {"triples": 2, "edges": 2}
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    ids = [str(london["memory_id"])]
    async with _another_orgs_erasure_pending(ch):
        await reconcile_row_facts(
            scope,
            ids,
            reopen=True,
            op="memory.restore",
            ch=ch,
            hashes={ids[0]: london["content_sha256"]},
        )
        failure = await _recorded_reconcile(ch, org)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_weights(ch, org, "london") == {"triples": 2, "edges": 2}


async def test_an_undo_starts_a_fact_dated_ahead_on_its_date(ch, org) -> None:
    # "I live in Rome. Moving to Berlin on Dec 1": the row retires behind Paris and is
    # Undone. Berlin starts on Dec 1 again, and Rome is current until then.
    from server.memory.app.projections import write_entity_triples
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    december = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=47)
    row, write, results = await _row_and_write(
        ch, org, "s1", "I live in Rome. Moving to Berlin on Dec 1.", [(*live, "rome")]
    )
    await write_entity_triples(write, results, ch=ch, entity_map={})
    # The same row states Berlin, dated Dec 1.
    entry = write.entries[0].model_copy(
        update={"relations": [(*live, "berlin")], "valid_at": december}
    )
    dated = write.model_copy(update={"entries": [entry]})
    await write_entity_triples(dated, results, ch=ch, entity_map={})
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, row, paris)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    markers = await ch.query_all(
        "SELECT object, valid_from, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        # Paris, which Berlin's start ended, is stated again by the retirement too.
        " AND adapter_name = 'row-restore' AND object != 'paris' ORDER BY object"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    berlin, rome = markers
    assert (berlin["object"], berlin["valid_from"], berlin["valid_to"]) == (
        "berlin",
        december,
        None,
    )
    assert (rome["object"], rome["valid_to"]) == ("rome", december)
    assert rome["valid_from"] < december
    edges = await ch.query_all(
        "SELECT entity_a, valid_from, valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a IN ('berlin', 'rome') SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    edges = {(e["entity_a"], e["valid_from"], e["valid_to"]) for e in edges}
    assert {("berlin", december, None), ("rome", rome["valid_from"], december)} <= edges
    # No Berlin edge is current before Dec 1.
    assert not [e for e in edges if e[0] == "berlin" and e[2] is None and e[1] < december]


async def test_a_replayed_undo_restates_a_fact_dated_ahead_as_stored(ch, org, monkeypatch) -> None:
    # As above, but the repair fails after the markers land. The replay reads them back
    # as stored, Berlin from Dec 1 to the second, so Rome still closes on Dec 1.
    from server.memory.app import graph_undo
    from server.memory.app.projection_failures import retry_projection_failure
    from server.memory.app.projections import write_entity_triples
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    december = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=47)
    row, write, results = await _row_and_write(
        ch, org, "s1", "I live in Rome. Moving to Berlin on Dec 1.", [(*live, "rome")]
    )
    await write_entity_triples(write, results, ch=ch, entity_map={})
    entry = write.entries[0].model_copy(
        update={"relations": [(*live, "berlin")], "valid_at": december}
    )
    dated = write.model_copy(update={"entries": [entry]})
    await write_entity_triples(dated, results, ch=ch, entity_map={})
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, row, paris)
    repair = graph_undo.repair_intervals

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("repair down")

    monkeypatch.setattr(graph_undo, "repair_intervals", _fails)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    monkeypatch.setattr(graph_undo, "repair_intervals", repair)
    failure = await _recorded_reconcile(ch, org)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    markers = await ch.query_all(
        "SELECT object, valid_from, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        # Paris, which Berlin's start ended, is stated again by the retirement too.
        " AND adapter_name = 'row-restore' AND object != 'paris' ORDER BY object"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    berlin, rome = markers  # one each: the replay wrote none
    assert (berlin["object"], berlin["valid_from"], berlin["valid_to"]) == (
        "berlin",
        december,
        None,
    )
    assert (rome["object"], rome["valid_to"]) == ("rome", december)


async def test_a_bulk_write_keeps_the_value_with_the_newest_observation(ch, org) -> None:
    # One write: London from Oct 15, Paris from Oct 14, London from Oct 13. London has
    # the newest observation and stays current; Paris closes when it starts.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    live = ("user", "live_in")
    stated = [
        ("k1", "I moved to London this week", "london", datetime(2026, 10, 15)),
        ("k2", "Paris was my home base for a while", "paris", datetime(2026, 10, 14)),
        ("k3", "Back then I had a flat near the Thames", "london", datetime(2026, 10, 13)),
    ]
    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {"memory_key": k, "value": v, "relations": [(*live, city)], "valid_at": at}
                for k, v, city, at in stated
            ],
        )
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    (paris,) = await ch.query_all(
        "SELECT valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'paris' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert paris["valid_to"] == datetime(2026, 10, 15)


async def test_an_undo_of_a_fact_open_until_later_adds_no_weight(ch, org) -> None:
    # "I live in London until next year": the readers count that row as current, so
    # the Undo's own row of it weighs nothing.
    from types import SimpleNamespace

    from server.memory.app import graph_undo

    until = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=60)
    london = await _stated(
        ch, org, "s1", "I live in London", ("user", "live_in", "london"), fact_valid_to=until
    )
    await _edges_like_triples(ch, org)
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restore_id=uuid.uuid4().hex,
        restored_at=naive_utc_now(),
    )
    await _mutations_done(ch, org)
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    # The Undo's own rows end where London does, so London is not current past then.
    ends = await ch.query_all(
        "SELECT 'triples' AS plane, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore'"
        " UNION ALL SELECT 'edges', valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'london' AND co_occur = 0"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert sorted((str(e["plane"]), e["valid_to"]) for e in ends) == [
        ("edges", until),
        ("triples", until),
    ]


async def test_an_undo_is_weighed_against_what_is_valid_at_the_restore(ch, org) -> None:
    # London retires behind Paris; another row says London from next week. That row is
    # not valid at the Undo, so the Undo's own London rows carry the weight then.
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app.facts import handle_facts_traverse
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(ch, org, "s1", "My favourite city is London")
    paris = await _stated(ch, org, "s2", "My favourite city is Paris")
    await _follow_retirement(ch, org, london, paris)
    later = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=5)
    await _stated(ch, org, "s3", "My favourite city is London", memory_key="k3", valid_at=later)
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    as_of = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(seconds=1)
    for labeled in (True, False):
        walk = FactsTraverseInput(
            org=org, namespace=NS, seed_entity="user", max_hops=1, labeled=labeled, as_of=as_of
        )
        nodes = {n.entity: n.via_co_occur for n in (await handle_facts_traverse(walk)).nodes}
        assert nodes.get("london", 0) >= 1, (labeled, nodes)


async def test_an_undo_keeps_no_end_a_supersession_set(ch, org) -> None:
    # London undated; Berlin from next month closes London when Berlin begins; Berlin's
    # fact then closes. That end was Berlin's, not London's: an Undo of London leaves
    # London open.
    from types import SimpleNamespace

    from server.memory.app import graph_undo
    from server.memory.app.projections import close_triples

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"))
    starts = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=45)
    await _stated(
        ch, org, "s2", "I move to Berlin", (*live, "berlin"), memory_key="k2", valid_at=starts
    )
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await close_triples(ch, org=org, namespace=NS, closes=[(*live, "berlin", now, now)])
    await _mutations_done(ch, org)
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restore_id=uuid.uuid4().hex,
        restored_at=naive_utc_now(),
    )
    await _mutations_done(ch, org)
    ends = await ch.query_all(
        "SELECT 'triples' AS plane, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore'"
        " UNION ALL SELECT 'edges', valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'london' AND valid_from >= {now:DateTime}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "now": now},
    )
    assert sorted((str(e["plane"]), e["valid_to"]) for e in ends) == [
        ("edges", None),
        ("triples", None),
    ]
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_an_undo_of_a_row_holding_two_values_keeps_the_later_one_current(ch, org) -> None:
    # "I lived in Rome. I now live in Berlin.": Berlin, stated after Rome, is current.
    # The row retires behind Paris and is Undone: Berlin is current again, not Rome.
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    row = await _stated(
        ch,
        org,
        "s1",
        "I lived in Rome. I now live in Berlin.",
        (*live, "rome"),
        also=((*live, "berlin"),),
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, row, paris)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_an_undo_weighs_a_fact_whose_row_a_close_ends_ahead(ch, org) -> None:
    # A close dated December ends London's row ahead and stamps it now: that row is
    # not current for the readers, so the Undo's own London row carries the weight.
    from types import SimpleNamespace

    from server.memory.app import graph_undo
    from server.memory.app.projections import close_triples

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"))
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    december = now + timedelta(days=60)
    await close_triples(ch, org=org, namespace=NS, closes=[(*live, "london", december, now)])
    await _mutations_done(ch, org)
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restore_id=uuid.uuid4().hex,
        restored_at=naive_utc_now(),
    )
    await _mutations_done(ch, org)
    (marker,) = await ch.query_all(
        "SELECT co_occur FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert marker["co_occur"] == 1


async def _support(ch: Any, org: str, city: str, ends: datetime) -> None:
    """An older weighted row of `city` that ends at `ends`, not closed by any repair."""
    from server.memory.app.projections import entity_triple_row

    hour_ago = datetime.now(UTC).replace(tzinfo=None, microsecond=0) - timedelta(hours=1)
    row = entity_triple_row(
        org,
        NS,
        "user",
        "live_in",
        city,
        provenance="t",
        confidence=1.0,
        adapter_name="t",
        last_seen=hour_ago,
        seen_at=hour_ago,
        valid_to=ends,
    )
    await ch.insert("loom.entity_triples", [row], settings={"wait_for_async_insert": 1})


async def _london_edges(ch: Any, org: str) -> list[tuple]:
    got = await ch.query_all(
        "SELECT co_occur, valid_from, valid_to, last_seen FROM loom.entity_edges"
        " WHERE org = {org:String} AND entity_a = 'london'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return sorted((tuple(g.values()) for g in got), key=str)


async def _marker_weights(ch: Any, org: str, edges_before: list[tuple]) -> dict[str, list[int]]:
    """The weights of the Undo's own London rows: its `row-restore` triples, and the
    London edges not there before it."""
    triples = await ch.query_all(
        "SELECT co_occur FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'london' AND adapter_name = 'row-restore'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    edges = await _london_edges(ch, org)
    for e in edges_before:
        edges.remove(e)
    return {"triples": [int(t["co_occur"]) for t in triples], "edges": [e[0] for e in edges]}


async def test_an_undo_keeps_its_weight_past_the_end_of_a_row_weighed_now(ch, org) -> None:
    # An older London row carries weight until next month; the Undo's London has no
    # end. Past that month London is still current, so the Undo's rows weigh 1.
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app.facts import handle_facts_traverse
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    next_month = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=17)
    await _support(ch, org, "london", next_month)
    await _edges_like_triples(ch, org)
    before = await _london_edges(ch, org)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    assert await _marker_weights(ch, org, before) == {"triples": [1], "edges": [1]}
    for labeled in (True, False):
        walk = FactsTraverseInput(
            org=org,
            namespace=NS,
            seed_entity="user",
            max_hops=1,
            labeled=labeled,
            as_of=next_month + timedelta(days=13),
        )
        nodes = {n.entity: n.via_co_occur for n in (await handle_facts_traverse(walk)).nodes}
        assert nodes.get("london", 0) >= 1, (labeled, nodes)


async def test_an_undo_dated_ahead_keeps_its_weight_past_a_row_that_ends_first(ch, org) -> None:
    # London from Dec 1 retires behind Paris and is Undone: its rows start on Dec 1.
    # An older London row weighs until next month only, so it never covers them.
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    december = now + timedelta(days=47)
    london = await _stated(
        ch, org, "s1", "I move to London on Dec 1", (*live, "london"), valid_at=december
    )
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    await _support(ch, org, "london", now + timedelta(days=17))
    await _edges_like_triples(ch, org)
    before = await _london_edges(ch, org)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    (start,) = await ch.query_all(
        "SELECT valid_from FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore' AND object = 'london'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert start["valid_from"] == december
    assert await _marker_weights(ch, org, before) == {"triples": [1], "edges": [1]}


async def test_two_undos_a_microsecond_apart_each_keep_their_own_markers(
    ch, org, monkeypatch
) -> None:
    # Two rows say "I lived in Rome. I now live in Berlin." and retire behind Paris.
    # Undone a microsecond apart, the second must not take the first's Berlin marker
    # for its own: Berlin, stated after Rome, stays current.
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    value = "I lived in Rome. I now live in Berlin."
    first = await _stated(ch, org, "s1", value, (*live, "rome"), also=((*live, "berlin"),))
    second = await _stated(
        ch, org, "s3", value, (*live, "rome"), also=((*live, "berlin"),), memory_key="k3"
    )
    await _mutations_done(ch, org)
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, first, paris)
    await _follow_retirement(ch, org, second, paris)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    at = datetime.now(UTC).replace(tzinfo=None)
    undo_second = _undo_of(paris).model_copy(update={"session_id": "s3", "memory_key": "k3"})
    for undo, instant in ((_undo_of(paris), at), (undo_second, at + timedelta(microseconds=1))):
        _date_undos_at(monkeypatch, lambda t=instant: t)
        assert (await handle_memory_restore(undo)).restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_an_undo_takes_no_end_from_an_older_row_of_its_fact(ch, org) -> None:
    # An older row said London until December; a later row said London with no end,
    # and a supersession closed it. The latest row's end was the close's: the Undo
    # keeps London open, not until December.
    from types import SimpleNamespace

    from server.memory.app import graph_undo
    from server.memory.app.projections import close_triples, entity_triple_row

    live = ("user", "live_in")
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    december = now + timedelta(days=60)
    hour_ago = now - timedelta(hours=1)
    older = entity_triple_row(
        org,
        NS,
        *live,
        "london",
        provenance="t",
        confidence=1.0,
        adapter_name="t",
        last_seen=hour_ago,
        seen_at=hour_ago,
        valid_to=december,
    )
    await ch.insert("loom.entity_triples", [older], settings={"wait_for_async_insert": 1})
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"))
    await close_triples(ch, org=org, namespace=NS, closes=[(*live, "london", now, now)])
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restore_id=uuid.uuid4().hex,
        restored_at=naive_utc_now(),
    )
    await _mutations_done(ch, org)
    ends = await ch.query_all(
        "SELECT 'triples' AS plane, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore'"
        " UNION ALL SELECT 'edges', valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'london' AND last_seen >= {now:DateTime} AND invalid_at IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "now": now},
    )
    assert sorted((str(e["plane"]), e["valid_to"]) for e in ends) == [
        ("edges", None),
        ("triples", None),
    ]


async def _restore_id(ch: Any, org: str) -> str:
    """The id of the org's one Undo, from the markers it wrote."""
    (row,) = await ch.query_all(
        "SELECT DISTINCT restore_id FROM loom.entity_triples WHERE org = {org:String}"
        " AND restore_id != '' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return str(row["restore_id"])


async def test_undo_of_an_open_fact_keeps_its_date_for_later_writes(ch, org, monkeypatch) -> None:
    # London from May retires behind Paris from June; another row says London from
    # July. Undoing the May row is the newest assertion, so Rome from August arrives
    # older than it and closes, however often the Undo is replayed.
    from types import SimpleNamespace

    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(
        ch, org, "s1", "I live in London", (*live, "london"), valid_at=datetime(2026, 5, 1)
    )
    paris = await _stated(
        ch, org, "s2", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1)
    )
    await _follow_retirement(ch, org, london, paris)
    await _stated(
        ch, org, "s3", "I live in London", (*live, "london"), valid_at=datetime(2026, 7, 1)
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    await _edges_like_triples(ch, org)
    restored_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    _date_undos_at(monkeypatch, lambda: restored_at)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored
    # A replay, as the repair runs it: the Undo's instant and id.
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restored_at=restored_at,
        restore_id=await _restore_id(ch, org),
    )
    (counted,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'london' AND valid_from = {at:DateTime}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "at": restored_at},
    )
    assert counted["n"] == 1
    await _mutations_done(ch, org)
    # London's edges are the current ones, one of them from the Undo, however often
    # it is replayed.
    assert set(await _open_edges(ch, org, {"london"})) == {"london"}
    (counted,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'london' AND valid_from = {at:DateTime} AND valid_to IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "at": restored_at},
    )
    assert counted["n"] == 1
    # London from July was open with its weight: the Undo adds none.
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    await _stated(ch, org, "s4", "I live in Rome", (*live, "rome"), valid_at=datetime(2026, 8, 1))
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_failed_interval_repair_is_recorded_for_replay(ch, org, monkeypatch) -> None:
    from server.memory.app import projections
    from server.memory.app.projection_failures import (
        list_projection_failures,
        retry_projection_failure,
    )

    await _stated(ch, org, "s1", "I currently live in London", ("user", "live_in", "london"))
    repair = projections._repair_contradictory_triples

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("repair down")

    monkeypatch.setattr(projections, "_repair_contradictory_triples", _fails)
    await _revise(
        "memory.set", org, {"session_id": "s2"}, "I moved to Rome", (("user", "live_in", "rome"),)
    )
    # The failure log is a fire-and-forget async insert: wait for its flush.
    for _ in range(100):
        failures = await list_projection_failures(org=org, projection="entity_triples", ch=ch)
        if failures:
            break
        await asyncio.sleep(0.1)
    (failure,) = failures
    monkeypatch.setattr(projections, "_repair_contradictory_triples", repair)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    # The triples landed before the repair failed: the replay repaired, not inserted.
    (counted,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'rome' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert counted["n"] == 1


async def _failed_repair(
    ch: Any,
    org: str,
    monkeypatch: Any,
    step: str,
    value: str,
    triple: tuple,
    key: str,
    fails: Any = None,
) -> tuple[dict[str, Any], Any]:
    """A row of `value` whose interval repair fails at `step` (or runs `fails` in its
    place), and the failure its write records for the projection repair."""
    import uuid

    from server.memory.app import projections
    from server.memory.app.projection_errors import TriplesRepairFailed, bulk_projection_payload
    from server.memory.app.projection_failures import ProjectionFailureRow

    row, write, results = await _row_and_write(ch, org, key, value, [triple], memory_key=key)

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError(f"{step} down")

    with monkeypatch.context() as patched:
        patched.setattr(projections, step, fails or _fails)
        with pytest.raises(TriplesRepairFailed) as raised:
            await projections.write_entity_triples(write, results, ch=ch, entity_map={})
    payload = bulk_projection_payload(write, results, error=raised.value)
    failure = ProjectionFailureRow(
        failure_id=str(uuid.uuid4()),
        org=org,
        namespace=NS,
        agent="bot",
        projection="entity_triples",
        source_operation="memory.bulk_set",
        source_resource_id=f"memory.bulk_set:{key}",
        memory_keys=[key],
        error_type="RuntimeError",
        error_message=str(raised.value),
        # As the failure log stores it.
        payload=json.loads(json.dumps(payload)),
    )
    return row, failure


async def _status(ch: Any, org: str, row: dict[str, Any]) -> str:
    (got,) = await ch.query_all(
        "SELECT status FROM loom.memory FINAL WHERE org = {org:String} AND memory_id = {id:UUID}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": str(row["memory_id"])},
    )
    return str(got["status"])


@pytest.fixture
def newest_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import projections

    async def _policy(*a: Any, **k: Any) -> str:
        return "newest_wins"

    monkeypatch.setattr(projections._conflicts, "namespace_conflict_policy", _policy)


async def test_a_key_revised_inside_one_bulk_keeps_its_live_value_searchable(
    ch, org, newest_wins
) -> None:
    # Moscow then Russellville under one key: the row and its vector both hold the
    # later value live, and no version names its own row as its successor.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set

    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {"memory_key": "cap", "value": "The capital of Soviet Union is Moscow."},
                {"memory_key": "cap", "value": "The capital of Soviet Union is Russellville."},
            ],
        )
    )
    await after_write.drain()
    assert await _status(ch, org, {"memory_id": out.written[-1].memory_id}) == "active"
    (emb,) = await ch.query_all(
        "SELECT status FROM loom.memory_embeddings FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert emb["status"] == "active"
    selfs = await ch.query_all(
        "SELECT count() AS n FROM loom.memory_versions WHERE org = {org:String}"
        " AND superseded_by = toString(memory_id) SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert selfs[0]["n"] == 0


async def test_a_bulk_that_returns_a_key_to_its_stored_value_ends_on_it(ch, org) -> None:
    # Berlin is stored; one bulk writes Paris then Berlin. The last word is Berlin, as
    # three memory.set calls in that order would leave it, not Paris.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set

    def _bulk(*values: str) -> Any:
        return handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id="s1",
                entries=[{"memory_key": "k", "value": v} for v in values],
            )
        )

    await _bulk("User lives in Berlin.")
    await _bulk("User lives in Paris.", "User lives in Berlin.")
    await after_write.drain()
    rows = await ch.query_all(
        "SELECT value FROM loom.memory FINAL WHERE org = {org:String} AND memory_key = 'k'"
        " AND tombstoned_at IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [json.loads(r["value"]) for r in rows] == ["User lives in Berlin."]


async def _bulk_in(org: str, session: str, *entries: dict[str, Any]) -> Any:
    """One memory.bulk_set of `entries` in `session`, its projections drained."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set

    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id=session,
            entries=list(entries),
        )
    )
    await after_write.drain()
    return out


async def _live_values(ch: Any, org: str) -> dict[tuple[str, str], tuple[str, int]]:
    """(session, key) -> (value, pinned) of every live row of the org."""
    rows = await ch.query_all(
        "SELECT session_id, memory_key, value, pinned FROM loom.memory FINAL"
        " WHERE org = {org:String} AND tombstoned_at IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return {
        (r["session_id"], r["memory_key"]): (json.loads(r["value"]), int(r["pinned"])) for r in rows
    }


async def test_a_bulk_keeps_a_value_it_moves_off_a_key_it_overwrites(ch, org) -> None:
    # k2 holds Berlin; one bulk moves k2 to the bicycle and states Berlin under k.
    # k2's Berlin is gone by the time k is written, so k must not dedup or merge into it.
    await _bulk_in(org, "s1", {"memory_key": "k2", "value": "User lives in Berlin."})
    out = await _bulk_in(
        org,
        "s1",
        {"memory_key": "k2", "value": "User owns a red bicycle."},
        {"memory_key": "k", "value": "User lives in Berlin."},
    )
    assert out.identity_duplicates == {} and out.merged == []
    live = await _live_values(ch, org)
    assert live[("s1", "k2")][0] == "User owns a red bicycle."
    assert live[("s1", "k")][0] == "User lives in Berlin."


@pytest.mark.parametrize("one_bulk", [True, False])
async def test_a_revision_of_a_live_key_is_not_eaten_by_another_sessions_copy(
    ch, org, one_bulk: bool
) -> None:
    # s0 says Berlin; s1 says Paris, then Berlin again. s1's last word is Berlin, as
    # memory.set in that order would leave it; s0's copy must not swallow the revision.
    await _bulk_in(org, "s0", {"memory_key": "k", "value": "User lives in Berlin."})
    paris = {"memory_key": "k", "value": "User lives in Paris."}
    berlin = {"memory_key": "k", "value": "User lives in Berlin."}
    if one_bulk:
        out = await _bulk_in(org, "s1", paris, berlin)
    else:
        await _bulk_in(org, "s1", paris)
        out = await _bulk_in(org, "s1", berlin)
    assert out.identity_duplicates == {}
    assert (await _live_values(ch, org))[("s1", "k")][0] == "User lives in Berlin."


async def test_a_new_key_still_dedups_against_another_sessions_copy(ch, org) -> None:
    await _bulk_in(org, "s0", {"memory_key": "k", "value": "User lives in Berlin."})
    out = await _bulk_in(org, "s1", {"memory_key": "k", "value": "User lives in Berlin."})
    assert out.identity_duplicates == {"k": "k"}
    assert ("s1", "k") not in await _live_values(ch, org)


async def test_a_key_said_twice_in_its_first_bulk_reinforces_the_row(
    ch, org, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    async def _record(**kw: Any) -> None:
        events.append(str(kw["memory_id"]))

    monkeypatch.setattr("server.memory.store.usage.record_usage_event", _record)
    out = await _bulk_in(
        org,
        "s1",
        {"memory_key": "k", "value": "User lives in Berlin."},
        {"memory_key": "k", "value": "User lives in Berlin."},
    )
    # The second says it again: it writes nothing and reinforces the row the first wrote.
    first, second = out.written
    assert first.memory_id and second.version == 0
    assert events == [first.memory_id]


async def test_a_key_pinned_earlier_in_the_bulk_refuses_a_later_value(ch, org) -> None:
    out = await _bulk_in(
        org,
        "s1",
        {"memory_key": "k", "value": "User lives in Berlin.", "pinned": True},
        {"memory_key": "k", "value": "User owns a red bicycle."},
    )
    assert out.skipped_pinned == ["k"]
    assert (await _live_values(ch, org))[("s1", "k")] == ("User lives in Berlin.", 1)


async def test_a_value_its_own_bulk_replaced_retires_no_other_row(ch, org, newest_wins) -> None:
    # k2's Russellville would retire k1's Moscow, but the same bulk then writes k2 again:
    # k2 no longer says anything about the capital, so Moscow stays live and searchable.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set

    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {"memory_key": "k1", "value": "The capital of Soviet Union is Moscow."},
                {"memory_key": "k2", "value": "The capital of Soviet Union is Russellville."},
                {"memory_key": "k2", "value": "Alice likes green tea."},
            ],
        )
    )
    await after_write.drain()
    assert await _status(ch, org, {"memory_id": out.written[0].memory_id}) == "active"
    rows = await ch.query_all(
        "SELECT memory_key, status FROM loom.memory_embeddings FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert {r["memory_key"]: r["status"] for r in rows} == {"k1": "active", "k2": "active"}


async def test_a_replayed_repair_never_closes_a_newer_value(
    ch, org, monkeypatch, newest_wins
) -> None:
    # Rome's repair fails; Paris then closes London and Rome. The replay of Rome's
    # repair is dated at Rome's write: it closes nothing Paris stated, nor retires it.
    from server.memory.app.projection_failures import retry_projection_failure

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1")
    _, failure = await _failed_repair(
        ch,
        org,
        monkeypatch,
        "_repair_contradictory_triples",
        "I moved to Rome",
        (*live, "rome"),
        "k2",
    )
    # Paris arrives after Rome's write, in its second or a later one.
    paris = await _stated(ch, org, "s3", "I moved to Paris", (*live, "paris"), memory_key="k3")
    await _mutations_done(ch, org)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert await _status(ch, org, paris) == "active"
    assert await _status(ch, org, london) == "superseded"


@pytest.mark.parametrize(
    "step",
    [
        "close_triples",
        "_undo_raced_closes",
        "_repair_contradictory_edges",
        "_retire_memories_behind_repairs",
    ],
)
async def test_a_replay_finishes_a_repair_that_failed_after_its_close(
    ch, org, monkeypatch, newest_wins, step
) -> None:
    # Rome's close of London was issued, a later step did not run: the replay closes
    # London if that close never landed, and retires the row behind it.
    from server.memory.app.projection_failures import retry_projection_failure

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1")
    _, failure = await _failed_repair(
        ch, org, monkeypatch, step, "I moved to Rome", (*live, "rome"), "k2"
    )
    await _mutations_done(ch, org)
    assert await _status(ch, org, london) == "active"
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    assert await _status(ch, org, london) == "superseded"


async def test_a_replay_that_fails_after_its_close_is_finished_by_the_next(
    ch, org, monkeypatch, newest_wins
) -> None:
    # Rome's repair fails before it closes anything. Its replay closes London and then
    # fails to retire: the next replay, finding London closed, still retires its row.
    from server.memory.app import projections
    from server.memory.app.projection_failures import (
        list_projection_failures,
        record_projection_failure,
        retry_projection_failure,
    )

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1")
    _, failure = await _failed_repair(
        ch,
        org,
        monkeypatch,
        "_repair_contradictory_triples",
        "I moved to Rome",
        (*live, "rome"),
        "k2",
    )
    await record_projection_failure(
        org=org,
        namespace=NS,
        agent="bot",
        projection="entity_triples",
        source_operation="memory.bulk_set",
        memory_keys=["k2"],
        error=failure.error_message,
        payload=failure.payload,
        ch=ch,
    )

    async def _stored() -> Any:
        for _ in range(100):
            stored = await list_projection_failures(org=org, projection="entity_triples", ch=ch)
            if stored:
                return stored[0]
            await asyncio.sleep(0.1)
        pytest.fail("the failure log never flushed")

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("retire down")

    with monkeypatch.context() as patched:
        patched.setattr(projections, "_retire_memories_behind_repairs", _fails)
        assert not (await retry_projection_failure(await _stored(), ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    assert await _status(ch, org, london) == "active"
    assert (await retry_projection_failure(await _stored(), ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    assert await _status(ch, org, london) == "superseded"


async def test_a_replay_leaves_a_value_stated_again_since(
    ch, org, monkeypatch, newest_wins
) -> None:
    # Back to London after Rome's retirement failed: London is current again, and
    # finishing Rome's repair would retire the row that just stated it.
    from server.memory.app.projection_failures import retry_projection_failure

    live = ("user", "live_in")
    await _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1")
    _, failure = await _failed_repair(
        ch,
        org,
        monkeypatch,
        "_retire_memories_behind_repairs",
        "I moved to Rome",
        (*live, "rome"),
        "k2",
    )
    await _mutations_done(ch, org)
    # Back to London arrives after Rome, in its second or a later one.
    back = await _stated(
        ch, org, "s3", "I moved back to London", (*live, "london"), memory_key="k3"
    )
    await _mutations_done(ch, org)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    assert await _status(ch, org, back) == "active"


async def _rome_raced_by_back_to_london(
    ch: Any, org: str, monkeypatch: Any, back_from: datetime | None = None
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    """London (k1); Rome (k2), whose close of London lands after "I moved back to
    London" (k3, dated `back_from`) and takes its row too; then Rome's re-check fails.
    Returns the k1 and k3 rows and Rome's recorded failure."""
    from server.memory.app import projections

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1")
    close, undo = projections.close_triples, projections._undo_raced_closes
    back: dict[str, Any] = {}

    async def _restated_first(*a: Any, **k: Any) -> Any:
        if not back:
            back["started"] = True  # its own repair's close runs as usual
            back["row"] = await _stated(
                ch,
                org,
                "s3",
                "I moved back to London",
                (*live, "london"),
                memory_key="k3",
                valid_at=back_from,
            )
        return await close(*a, **k)

    async def _rome_fails(input: Any, *a: Any, **k: Any) -> Any:
        if input.entries[0].memory_key == "k2":
            raise RuntimeError("re-check down")
        return await undo(input, *a, **k)

    with monkeypatch.context() as patched:
        patched.setattr(projections, "close_triples", _restated_first)
        _, failure = await _failed_repair(
            ch,
            org,
            monkeypatch,
            "_undo_raced_closes",
            "I moved to Rome",
            (*live, "rome"),
            "k2",
            fails=_rome_fails,
        )
    await _mutations_done(ch, org)
    return london, back["row"], failure


@pytest.mark.parametrize("dated", [False, True])
async def test_a_replay_reopens_a_value_stated_again_while_its_close_landed(
    ch, org, monkeypatch, newest_wins, dated
) -> None:
    # The replay opens London again instead of finishing Rome's close, also when London
    # is dated by its session day, before Rome's write.
    from server.memory.app.projection_failures import retry_projection_failure

    back_from = _today() if dated else None
    _, back, failure = await _rome_raced_by_back_to_london(ch, org, monkeypatch, back_from)
    assert await _open_cities(ch, org, set()) == set()
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    assert await _status(ch, org, back) == "active"


async def test_a_reopened_value_meets_the_value_stated_after_it(
    ch, org, monkeypatch, newest_wins
) -> None:
    # As above, and Paris is stated before the replay: it found no open London to
    # close. Opened again, London meets Paris as written in turn: Paris stays current
    # and the London rows retire behind it, as if Paris's write had closed London.
    from server.memory.app.projection_failures import retry_projection_failure

    london, back, failure = await _rome_raced_by_back_to_london(ch, org, monkeypatch)
    live = ("user", "live_in")
    paris = await _stated(ch, org, "s4", "I moved to Paris", (*live, "paris"), memory_key="k4")
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert await _status(ch, org, paris) == "active"
    assert await _status(ch, org, back) == "superseded"
    assert await _status(ch, org, london) == "superseded"


async def test_a_replayed_undo_is_dated_at_the_undo(ch, org, monkeypatch) -> None:
    # The Undo's reconcile fails; Rome is stated after it. Replayed, the Undo brings
    # London back as of the Undo, so Rome, the newer value, stays current.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.projection_failures import (
        TRIPLES_RECONCILE,
        list_projection_failures,
        retry_projection_failure,
    )
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    reopen = graph_undo.reopen_held_facts

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("reconcile down")

    monkeypatch.setattr(graph_undo, "reopen_held_facts", _fails)
    undo = MemoryRestoreInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        memory_key=KEY,
        if_superseded_by=str(paris["memory_id"]),
    )
    assert (await handle_memory_restore(undo)).restored
    monkeypatch.setattr(graph_undo, "reopen_held_facts", reopen)
    # Rome arrives after the Undo, in its second or a later one.
    await _stated(ch, org, "s3", "I moved to Rome", (*live, "rome"), memory_key="k3")
    await _mutations_done(ch, org)
    for _ in range(100):
        failures = await list_projection_failures(org=org, projection=TRIPLES_RECONCILE, ch=ch)
        if failures:
            break
        await asyncio.sleep(0.1)
    (failure,) = failures
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}


async def test_a_users_row_links_are_erased_after_their_memory_row_is_gone(ch, org) -> None:
    # A deleted row dropped by its TTL leaves no `loom.memory` row to find its links by.
    import uuid

    from server.erasure.store.census import residue_census
    from server.erasure.store.steps_graph import delete_graph_links_by_user

    link = {
        "org": org,
        "namespace": NS,
        "memory_id": str(uuid.uuid4()),
        "user_id": USER,
        "content_sha256": "0" * 64,
        "subject": "user",
        "relation": "live_in",
        "object": "paris",
        "inferred": False,
    }
    theirs = {**link, "memory_id": str(uuid.uuid4()), "user_id": "u2"}
    await ch.insert("loom.entity_triple_rows", [link, theirs])
    # The census counts by the same predicate the step deletes by.
    assert (await residue_census(org, "user_id", USER, ch=ch))["loom.entity_triple_rows"] == 1
    await delete_graph_links_by_user(org, USER, ch=ch)
    assert (await residue_census(org, "user_id", USER, ch=ch))["loom.entity_triple_rows"] == 0
    rows = await ch.query_all(
        "SELECT user_id FROM loom.entity_triple_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert rows == [{"user_id": "u2"}]


async def test_a_users_edge_row_links_are_erased(ch, org) -> None:
    # As the triple links: found by the writer they carry, not by their row.
    import uuid

    from server.erasure.store.census import residue_census
    from server.erasure.store.steps_graph import delete_graph_links_by_user

    link = {
        "org": org,
        "namespace": NS,
        "memory_id": str(uuid.uuid4()),
        "user_id": USER,
        "content_sha256": "0" * 64,
        "entity_a": "alice",
        "entity_b": "paris",
    }
    theirs = {**link, "memory_id": str(uuid.uuid4()), "user_id": "u2"}
    await ch.insert("loom.entity_edge_rows", [link, theirs])
    assert (await residue_census(org, "user_id", USER, ch=ch))["loom.entity_edge_rows"] == 1
    await delete_graph_links_by_user(org, USER, ch=ch)
    assert (await residue_census(org, "user_id", USER, ch=ch))["loom.entity_edge_rows"] == 0
    rows = await ch.query_all(
        "SELECT user_id FROM loom.entity_edge_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert rows == [{"user_id": "u2"}]


async def test_a_replay_projects_nothing_for_a_value_revised_since(ch, org) -> None:
    # London's projection failed; the row was then revised to Berlin. The replay
    # finds the row no longer holds London and leaves Berlin current.
    import uuid

    from server.memory.app.projection_errors import bulk_projection_payload
    from server.memory.app.projection_failures import ProjectionFailureRow, retry_projection_failure

    live = ("user", "live_in")
    london, write, results = await _row_and_write(
        ch, org, "s1", "I live in London", [(*live, "london")]
    )
    payload = bulk_projection_payload(write, results, error=RuntimeError("triples down"))
    await _revise("memory.update_by_id", org, london, "I live in Berlin", ((*live, "berlin"),))
    failure = ProjectionFailureRow(
        failure_id=str(uuid.uuid4()),
        org=org,
        namespace=NS,
        agent="bot",
        projection="entity_triples",
        source_operation="memory.bulk_set",
        source_resource_id="memory.bulk_set:s1:-",
        memory_keys=[KEY],
        error_type="RuntimeError",
        error_message="triples down",
        payload=json.loads(json.dumps(payload)),
    )
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_undo_reopens_only_what_the_restored_value_holds(ch, org, monkeypatch) -> None:
    # The row said Paris, was revised to London, then retired behind Rome. Another
    # row still says Paris, but the restored row no longer does: Undo reopens London.
    from server.core.hashing import sha256_value
    from server.memory import store as M
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.projections import write_entity_triples
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    paris = await _stated(ch, org, "s1", "I currently live in Paris", (*live, "paris"))
    await _stated(ch, org, "s3", "I currently live in Paris", (*live, "paris"))
    text = "I currently live in London"
    london = {
        **paris,
        "version": M.next_version(),
        "value": json.dumps(text),
        "value_json": json.dumps({"v": text}),
        "content_sha256": sha256_value(text),
    }
    await M.insert_memory_versions([london], ch=ch)
    write = MemoryBulkSetInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        entries=[{"memory_key": KEY, "value": text, "relations": [(*live, "london")]}],
    )
    result = _BulkSetResult(
        memory_key=KEY, version=london["version"], memory_id=str(london["memory_id"])
    )
    await write_entity_triples(write, [result], ch=ch, entity_map={})
    rome = await _stated(ch, org, "s2", "I currently live in Rome", (*live, "rome"))
    await _follow_retirement(ch, org, london, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    await _undo_in_the_second_of(ch, org, "rome", monkeypatch)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(rome["memory_id"]),
        )
    )
    assert out.restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_high_bulk_entry_puts_nothing_in_the_shared_graph(ch, org) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {
                    "memory_key": "k1",
                    "value": "My vault code is 4417",
                    "sensitivity": "high",
                    "entities": ["vault", "4417"],
                    "relations": [("user", "vault_code", "4417")],
                },
                {
                    "memory_key": "k2",
                    "value": "I like Oslo and Bergen",
                    "entities": ["oslo", "bergen"],
                    "relations": [("user", "likes", "oslo")],
                },
            ],
        )
    )

    async def _graphed() -> set[tuple[str, str]]:
        got = await ch.query_all(
            "SELECT 'triples' AS t, object AS text FROM loom.entity_triples WHERE org = {org:String}"
            " UNION ALL SELECT 'rows', object FROM loom.entity_triple_rows WHERE org = {org:String}"
            " UNION ALL SELECT 'edges', concat(entity_a, ' ', entity_b) FROM loom.entity_edges"
            " WHERE org = {org:String}"
            " UNION ALL SELECT 'vectors', entity_text FROM loom.entity_vectors"
            " WHERE org = {org:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        return {(str(r["t"]), str(r["text"])) for r in got}

    # The batch's other entry is the sentinel: once its rows are in, all of them are.
    want = {("triples", "oslo"), ("rows", "oslo"), ("edges", "bergen oslo"), ("vectors", "oslo")}
    for _ in range(100):
        graphed = await _graphed()
        if want <= graphed:
            break
        await asyncio.sleep(0.1)
    assert want <= graphed
    assert not [g for g in graphed if "4417" in g[1] or "vault" in g[1]]


async def test_a_high_point_write_puts_no_entity_edge_in_the_shared_graph(ch, org) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    for key, value, sensitivity in (
        ("k1", "Alice Smith met Bob Jones in Secretville", "high"),
        ("k2", "Carol White met Dave Brown in Oslo", ""),
    ):
        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id="s1",
                memory_key=key,
                value=value,
                sensitivity=sensitivity,
            )
        )

    async def _edges() -> list[str]:
        got = await ch.query_all(
            "SELECT concat(entity_a, ' ', entity_b) AS pair FROM loom.entity_edges"
            " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        return [str(r["pair"]) for r in got]

    # The ordinary write is the sentinel: once its edges are in, the high one's would be.
    for _ in range(100):
        edges = await _edges()
        if any("oslo" in e for e in edges):
            break
        await asyncio.sleep(0.1)
    assert any("oslo" in e for e in edges)
    assert not [e for e in edges if "secretville" in e or "alice" in e or "bob" in e]


async def test_a_bulk_revision_to_high_closes_what_its_row_held(ch, org) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    london = await _stated(ch, org, "s1", "I currently live in London")
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    entry = {
        "memory_key": KEY,
        "value": "I currently live in London, flat 4B",
        "sensitivity": "high",
    }
    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=[entry]
        )
    )
    assert [r.memory_id for r in out.written] == [str(london["memory_id"])]
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


async def test_a_stored_row_revised_into_a_superseded_value_closes_what_it_held(
    ch, org, judge
) -> None:
    # s1's row is revised in place by a late bulk and lands superseded behind s2's
    # later row: the facts it held close, as they would for any revision.
    from server.memory import store as M
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    jazz = await _stated(ch, org, "s1", "I like jazz", triple=("user", "likes", "jazz"))
    rock = _row(org, "s2", "I like rock", datetime(2026, 6, 1))
    await M.insert_memory_versions([rock], ch=ch)
    assert await _open_cities(ch, org, {"jazz"}) == {"jazz"}
    entry = {"memory_key": KEY, "value": "I like blues", "valid_at": datetime(2026, 5, 1)}
    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=[entry]
        )
    )
    (written,) = out.written
    assert written.memory_id == str(jazz["memory_id"])
    assert written.superseded_by == str(rock["memory_id"])
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


async def test_an_undo_stays_newer_than_a_value_dated_before_it(ch, org) -> None:
    # London (May) retires behind Paris (June); an Undo brings London back now. Rome,
    # dated July and imported after the Undo, is older than the Undo's assertion.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(
        ch, org, "s1", "I live in London", (*live, "london"), valid_at=datetime(2026, 5, 1)
    )
    paris = await _stated(
        ch, org, "s2", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1)
    )
    await _follow_retirement(ch, org, london, paris)
    undo = MemoryRestoreInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        memory_key=KEY,
        if_superseded_by=str(paris["memory_id"]),
    )
    assert (await handle_memory_restore(undo)).restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    await _stated(
        ch,
        org,
        "s3",
        "I live in Rome",
        (*live, "rome"),
        memory_key="k3",
        valid_at=datetime(2026, 7, 1),
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def _london_undone_over_paris(ch: Any, org: str, relation: str) -> dict[str, Any]:
    """London (May) retired behind Paris (June), then undone: London is current."""
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(
        ch,
        org,
        "s1",
        "I live in London",
        ("user", relation, "london"),
        valid_at=datetime(2026, 5, 1),
    )
    paris = await _stated(
        ch, org, "s2", "I live in Paris", ("user", relation, "paris"), valid_at=datetime(2026, 6, 1)
    )
    await _follow_retirement(ch, org, london, paris)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    return london


async def test_a_restored_row_edited_to_an_older_value_leaves_the_newer_one_current(
    ch, org
) -> None:
    # The edit to Rome keeps the row's May date. Rome closes behind the Undo's London
    # marker, then London closes: Paris (June) is current, as without the Undo.
    from server.memory.api.models_crud import MemoryGetByIdInput, MemoryUpdateByIdInput
    from server.memory.app.handlers import handle_memory_get_by_id
    from server.memory.app.update import handle_memory_update_by_id

    london = await _london_undone_over_paris(ch, org, "live_in")
    got = await handle_memory_get_by_id(
        MemoryGetByIdInput(org=org, memory_id=str(london["memory_id"]))
    )
    out = await handle_memory_update_by_id(
        MemoryUpdateByIdInput(
            org=org,
            memory_id=str(london["memory_id"]),
            value="I live in Rome",
            relations=[("user", "live_in", "rome")],
            if_match_sha256=got.content_sha256,
        )
    )
    assert out.updated
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}


async def test_a_revision_dropping_the_undone_value_makes_the_value_it_beat_current(
    ch, org
) -> None:
    # The restored London row no longer says where the user lives: the Paris row
    # still does, so Paris is current again rather than no value at all.
    london = await _london_undone_over_paris(ch, org, "live_in")
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    want = {("live_in", "paris"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want
    # London's marker is the Undo's; Paris's, stated again for want of a value, is not.
    markers = await ch.query_all(
        "SELECT object, adapter_name FROM loom.entity_triples WHERE org = {org:String}"
        " AND restore_id != '' ORDER BY object SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [(m["object"], m["adapter_name"]) for m in markers] == [
        ("london", "row-restore"),
        ("paris", "row-fallback"),
    ]


async def test_a_retirement_of_the_undone_value_makes_the_value_it_beat_current(ch, org) -> None:
    # As above, but London's row is retired behind a row that only says Rome.
    london = await _london_undone_over_paris(ch, org, "live_in")
    rome = await _stated(ch, org, "s3", "I like Rome", ("user", "like", "rome"))
    await _follow_retirement(ch, org, london, rome)
    want = {("live_in", "paris"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want


async def test_a_value_closed_with_a_future_end_is_not_current(ch, org) -> None:
    # London (2027) closes Paris (June) at 2027-01-01. London's row then drops it:
    # Paris is current again, not closed at a day no value follows.
    live = ("user", "live_in")
    await _stated(ch, org, "s1", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1))
    london = await _stated(
        ch, org, "s2", "I live in London", (*live, "london"), valid_at=datetime(2027, 1, 1)
    )
    await _mutations_done(ch, org)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    want = {("live_in", "paris"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want


async def test_unrelated_revisions_add_no_weight_to_a_value_valid_until_a_dated_move(
    ch, org
) -> None:
    # Paris holds until the move to Berlin next month starts. Each revision of an
    # unrelated Oslo row checks the relation, finds Paris valid now and states nothing.
    live = ("user", "live_in")
    ahead = naive_utc_now() + timedelta(days=45)
    await _stated(ch, org, "s1", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1))
    await _stated(
        ch, org, "s2", "I am moving to Berlin", (*live, "berlin"), memory_key="move", valid_at=ahead
    )
    oslo = await _stated(
        ch,
        org,
        "s3",
        "I lived in Oslo",
        (*live, "oslo"),
        memory_key="trip",
        valid_at=datetime(2026, 1, 1),
    )
    await _mutations_done(ch, org)
    assert (await _open_weights(ch, org, "paris"))["triples"] == 1
    for value in ("I like Rome", "I like Rome a lot", "I like Rome most"):
        await _revise("memory.update_by_id", org, oslo, value, (("user", "like", "rome"),))
        await _mutations_done(ch, org)
    assert (await _open_weights(ch, org, "paris"))["triples"] == 1


async def test_a_recovered_value_starts_at_the_revision_and_walks_on_both_planes(ch, org) -> None:
    # London undone over Paris at T1, then revised away at T2: Paris is stated again
    # from T2, its edge too, and a walk between T1 and T2 still reaches only London.
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(
        ch, org, "s1", "I live in London", (*live, "london"), valid_at=datetime(2026, 5, 1)
    )
    paris = await _stated(
        ch, org, "s2", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1)
    )
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the DateTime columns store whole seconds
    between = naive_utc_now()
    await asyncio.sleep(1.1)
    await _revise("memory.update_by_id", org, london, "I like Rome")
    await _mutations_done(ch, org)
    assert await _walked(org, ["paris"], labeled=True) == ["paris"]
    # London's edge ends with its triple: no live row states it.
    assert await _walked(org, ["paris"], labeled=False) == ["paris"]
    assert await _walked(org, ["london"], labeled=True, as_of=between) == ["london"]
    assert await _walked(org, ["london"], labeled=False, as_of=between) == ["london"]


async def _row_saying(ch: Any, org: str, value: str) -> dict[str, Any]:
    (row,) = await ch.query_all(
        "SELECT toString(memory_id) AS memory_id FROM loom.memory FINAL"
        " WHERE org = {org:String} AND value = {value:String}",
        params={"org": org, "value": json.dumps(value)},
    )
    return row


async def test_a_fallback_asks_only_the_rows_holding_the_value(ch, org, monkeypatch) -> None:
    # A jazz row was linked to Paris for a value it no longer has. London's row drops
    # London: Paris is stated again through the row that says it, not the jazz row.
    from server.memory import store as M
    from server.memory.app import graph_undo
    from server.memory.app.projections import triple_row_link

    london = await _london_undone_over_paris(ch, org, "live_in")
    paris = await _row_saying(ch, org, "I live in Paris")
    jazz = _row(org, "s3", "I like jazz", datetime(2026, 5, 1), memory_key="k3")
    await M.insert_memory_versions([jazz], ch=ch)
    old = triple_row_link(
        org, NS, str(jazz["memory_id"]), "0" * 64, ("user", "live_in", "paris"), user_id=USER
    )
    await ch.insert("loom.entity_triple_rows", [old])
    reopen, asked = graph_undo.reopen_held_facts, []

    async def _asked(input: Any, ids: list[str], **kw: Any) -> None:
        if kw.get("adapter_name") == "row-fallback":
            asked.append(ids)
        await reopen(input, ids, **kw)

    monkeypatch.setattr(graph_undo, "reopen_held_facts", _asked)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    assert asked == [[paris["memory_id"]]]
    assert await _open_cities(ch, org, {"paris", "rome"}) == {"paris", "rome"}


async def test_a_fallback_leaves_out_a_row_revised_before_its_markers_land(
    ch, org, monkeypatch
) -> None:
    # London's row is revised away and Paris is picked to be current again; before its
    # markers land, the Paris row is revised to jazz. No live row says Paris.
    from server.memory.app import graph_undo

    london = await _london_undone_over_paris(ch, org, "live_in")
    paris = await _row_saying(ch, org, "I live in Paris")
    restate = graph_undo.restate_facts

    async def _paris_revised_first(*a: Any, **k: Any) -> Any:
        monkeypatch.setattr(graph_undo, "restate_facts", restate)
        await _revise("memory.update_by_id", org, paris, "I like jazz", (("user", "like", "jazz"),))
        return await restate(*a, **k)

    monkeypatch.setattr(graph_undo, "restate_facts", _paris_revised_first)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    want = {("like", "rome"), ("like", "jazz")}
    assert await _open_facts(ch, org, want) == want


async def test_a_fallback_leaves_out_a_row_retired_before_its_markers_land(
    ch, org, judge, monkeypatch
) -> None:
    # As above, but a bulk write of Berlin retires the Paris row (and the Rome one)
    # before the markers land. The row keeps its value, so only its status says it no
    # longer holds Paris: Berlin is current, not Paris.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import graph_undo
    from server.memory.app.bulk_set import handle_memory_bulk_set

    london = await _london_undone_over_paris(ch, org, "live_in")
    restate, retired = graph_undo.restate_facts, []

    async def _paris_retired_first(*a: Any, **k: Any) -> Any:
        monkeypatch.setattr(graph_undo, "restate_facts", restate)
        entry = {
            "memory_key": KEY,
            "value": "I live in Berlin",
            "relations": [("user", "live_in", "berlin")],
            "valid_at": datetime(2026, 7, 1),
        }
        out = await handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org, namespace=NS, agent="bot", user_id=USER, session_id="s3", entries=[entry]
            )
        )
        retired.extend(out.written)
        await _mutations_done(ch, org)
        return await restate(*a, **k)

    monkeypatch.setattr(graph_undo, "restate_facts", _paris_retired_first)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    assert retired
    assert await _open_facts(ch, org, {("live_in", "berlin")}) == {("live_in", "berlin")}


async def test_a_replayed_fallback_dates_its_triple_as_its_edge(ch, org, monkeypatch) -> None:
    # The fallback's edge marker lands and its triple marker fails. Replayed later, the
    # triple starts when the edge does, so both walks agree in between.
    from server.core import ch as ch_mod
    from server.memory.app import entity_projections
    from server.memory.app.projection_failures import retry_projection_failure

    london = await _london_undone_over_paris(ch, org, "live_in")
    await _edges_like_triples(ch, org)
    client = ch_mod.get_client()
    insert = client.insert

    async def _triples_fail(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_triples" and rows[0]["adapter_name"] == "row-fallback":
            raise ConnectionError("transient")
        return await insert(table, rows, **kw)

    monkeypatch.setattr(client, "insert", _triples_fail)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    monkeypatch.setattr(client, "insert", insert)
    failure = await _recorded_reconcile(ch, org)
    later = naive_utc_now() + timedelta(seconds=30)
    monkeypatch.setattr(entity_projections, "naive_utc_now", lambda: later)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    starts = await ch.query_all(
        "SELECT 'triple' AS plane, valid_from FROM loom.entity_triples"
        " WHERE org = {org:String} AND adapter_name = 'row-fallback'"
        " UNION ALL SELECT 'edge', valid_from FROM loom.entity_edges"
        " WHERE org = {org:String} AND restore_id IN (SELECT restore_id"
        " FROM loom.entity_triples WHERE org = {org:String} AND adapter_name = 'row-fallback')"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert sorted(r["plane"] for r in starts) == ["edge", "triple"]
    assert len({r["valid_from"] for r in starts}) == 1


async def test_a_replayed_fallback_finishes_the_repair_its_markers_missed(
    ch, org, monkeypatch
) -> None:
    # The move to Berlin is ahead. London's row is revised away and Paris is stated
    # again; both its markers land but its interval repair fails. The replay finds the
    # markers and must still repair: Paris ends when Berlin starts.
    from server.memory.app import graph_undo
    from server.memory.app.projection_failures import retry_projection_failure

    live = ("user", "live_in")
    london = await _london_undone_over_paris(ch, org, "live_in")
    ahead = naive_utc_now().replace(microsecond=0) + timedelta(days=45)
    await _stated(
        ch, org, "s3", "I am moving to Berlin", (*live, "berlin"), memory_key="move", valid_at=ahead
    )
    await _edges_like_triples(ch, org)
    repair = graph_undo.repair_intervals

    async def _repair_fails(*a: Any, **k: Any) -> Any:
        raise ConnectionError("transient")

    monkeypatch.setattr(graph_undo, "repair_intervals", _repair_fails)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    monkeypatch.setattr(graph_undo, "repair_intervals", repair)
    failure = await _recorded_reconcile(ch, org)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    after = ahead + timedelta(days=1)
    want = ["berlin", "rome"]
    assert await _walked(org, want, labeled=True, as_of=after) == want
    # Rome came after the edges were copied: it has no edge.
    assert await _walked(org, ["berlin"], labeled=False, as_of=after) == ["berlin"]


async def test_a_replayed_fallback_closes_the_edge_its_repair_missed(ch, org, monkeypatch) -> None:
    # As above, but the repair closes Paris's triple at Berlin's start and then fails
    # to close its edge. The replay closes the edge too: after Dec 1 neither walk
    # reaches Paris.
    from server.core import ch as ch_mod
    from server.memory.app.projection_failures import retry_projection_failure

    live = ("user", "live_in")
    london = await _london_undone_over_paris(ch, org, "live_in")
    ahead = naive_utc_now().replace(microsecond=0) + timedelta(days=45)
    await _stated(
        ch, org, "s3", "I am moving to Berlin", (*live, "berlin"), memory_key="move", valid_at=ahead
    )
    await _edges_like_triples(ch, org)
    client = ch_mod.get_client()
    execute, failed = client.execute, []

    async def _edge_close_fails(sql: str, *a: Any, **kw: Any) -> Any:
        # The repair's edge close names each edge by its key (char 31 joins its parts).
        if not failed and "ALTER TABLE loom.entity_edges" in sql and "char(31)" in sql:
            failed.append(sql)
            raise ConnectionError("transient")
        return await execute(sql, *a, **kw)

    monkeypatch.setattr(client, "execute", _edge_close_fails)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    monkeypatch.setattr(client, "execute", execute)
    assert failed
    failure = await _recorded_reconcile(ch, org)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    after = ahead + timedelta(days=1)
    want = ["berlin", "rome"]
    assert await _walked(org, want, labeled=True, as_of=after) == want
    assert await _walked(org, ["berlin"], labeled=False, as_of=after) == ["berlin"]


async def test_a_second_replay_closes_the_edge_the_first_replays_repair_missed(
    ch, org, monkeypatch
) -> None:
    # The revision's reconcile fails before its fallback runs, so the failure records no
    # markers. Its first replay states Paris again and fails to close Paris's edge: the
    # closes it made must ride the failure, or the second replay finds the markers,
    # finds Paris's triple already closed, repairs nothing and leaves the edge open.
    from server.core import ch as ch_mod
    from server.memory.app import entity_projections
    from server.memory.app.projection_failures import retry_projection_failure

    live = ("user", "live_in")
    london = await _london_undone_over_paris(ch, org, "live_in")
    ahead = naive_utc_now().replace(microsecond=0) + timedelta(days=45)
    await _stated(
        ch, org, "s3", "I am moving to Berlin", (*live, "berlin"), memory_key="move", valid_at=ahead
    )
    await _edges_like_triples(ch, org)

    async def _fallback_fails(*a: Any, **k: Any) -> None:
        raise ConnectionError("transient")

    with monkeypatch.context() as patched:
        patched.setattr(entity_projections, "_keep_one_current", _fallback_fails)
        await _revise(
            "memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),)
        )
    client = ch_mod.get_client()
    execute, failed = client.execute, []

    async def _edge_close_fails(sql: str, *a: Any, **kw: Any) -> Any:
        # The repair's edge close names each edge by its key (char 31 joins its parts).
        if not failed and "ALTER TABLE loom.entity_edges" in sql and "char(31)" in sql:
            failed.append(sql)
            raise ConnectionError("transient")
        return await execute(sql, *a, **kw)

    with monkeypatch.context() as patched:
        patched.setattr(client, "execute", _edge_close_fails)
        first = await retry_projection_failure(await _recorded_reconcile(ch, org), ch=ch)
    assert failed and not first.resolved
    second = await retry_projection_failure(await _recorded_reconcile(ch, org), ch=ch)
    assert second.resolved
    await _mutations_done(ch, org)
    after = ahead + timedelta(days=1)
    assert await _walked(org, ["berlin"], labeled=False, as_of=after) == ["berlin"]


async def test_a_retired_rows_edge_ends_with_its_triple(ch, org) -> None:
    # London's row retires behind Paris's: both walks reach Paris only. A favourite
    # city may have many values, so Paris's own write does not close London.
    london = await _stated(ch, org, "s1", "I like London")
    paris = await _stated(ch, org, "s2", "I like Paris")
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    assert await _walked(org, ["paris"], labeled=True) == ["paris"]
    assert await _walked(org, ["paris"], labeled=False) == ["paris"]


async def _replay_follow(ch: Any, org: str, loser: dict[str, Any], winner: dict[str, Any]) -> None:
    """Replay the follow of `loser` retired behind `winner`, as the repair replays the
    pairs a failed follow recorded."""
    from server.memory.app.projection_failures import (
        FOLLOW_ROWS,
        ProjectionFailureRow,
        retry_projection_failure,
    )

    failure = ProjectionFailureRow(
        failure_id=str(uuid.uuid4()),
        org=org,
        namespace=NS,
        agent="bot",
        projection=FOLLOW_ROWS,
        source_operation="memory.bulk_set",
        source_resource_id="memory.bulk_set:replay",
        memory_keys=[KEY],
        error_type="RuntimeError",
        error_message="follow failed",
        payload={"pairs": [[str(loser["memory_id"]), str(winner["memory_id"])]]},
    )
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)


async def test_a_replayed_follow_ends_the_edge_its_failure_left_open(ch, org, monkeypatch) -> None:
    # London's close lands and its edge end fails: the replay plans no close, as
    # London is closed, and still ends the edge.
    from server.memory.app import entity_projections

    london = await _stated(ch, org, "s1", "I like London")
    paris = await _stated(ch, org, "s2", "I like Paris")
    await _edges_like_triples(ch, org)

    async def _fails(*a: Any, **k: Any) -> None:
        raise RuntimeError("edges down")

    with monkeypatch.context() as patched:
        patched.setattr(entity_projections, "end_edges", _fails)
        with pytest.raises(RuntimeError):
            await _follow_retirement(ch, org, london, paris)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert await _walked(org, ["london", "paris"], labeled=False) == ["london", "paris"]
    await _replay_follow(ch, org, london, paris)
    assert await _walked(org, ["paris"], labeled=False) == ["paris"]


async def test_a_replayed_follow_keeps_one_value_current(ch, org, monkeypatch) -> None:
    # London undone over Paris, then retired behind Rome: the follow closes London and
    # fails to state Paris again. The replay finds London closed and still does.
    from server.memory.app import graph_undo

    london = await _london_undone_over_paris(ch, org, "live_in")
    rome = await _stated(ch, org, "s3", "I like Rome", ("user", "like", "rome"))

    async def _fails(*a: Any, **k: Any) -> None:
        raise RuntimeError("restate down")

    with monkeypatch.context() as patched:
        patched.setattr(graph_undo, "restate_facts", _fails)
        with pytest.raises(RuntimeError):
            await _follow_retirement(ch, org, london, rome)
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("like", "rome")}) == {("like", "rome")}
    await _replay_follow(ch, org, london, rome)
    want = {("live_in", "paris"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want


async def test_a_retired_rows_edge_ends_when_a_winner_dated_ahead_starts(ch, org) -> None:
    # Paris's row retires behind Berlin's, dated next month: Paris holds until then on
    # both walks, and only Berlin after.
    from server.memory import store as M

    ahead = naive_utc_now().replace(microsecond=0) + timedelta(days=30)
    paris = await _stated(ch, org, "s1", "I like Paris")
    berlin = await _stated(ch, org, "s2", "I like Berlin", valid_at=ahead)
    berlin = {**berlin, "valid_at": ahead, "version": M.next_version()}
    await M.insert_memory_versions([berlin], ch=ch)
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, paris, berlin)
    both = ["berlin", "paris"]
    assert await _walked(org, both, labeled=True) == both
    assert await _walked(org, both, labeled=False) == both
    later = ahead + timedelta(days=1)
    assert await _walked(org, ["berlin"], labeled=True, as_of=later) == ["berlin"]
    assert await _walked(org, ["berlin"], labeled=False, as_of=later) == ["berlin"]


async def test_a_retirement_ends_now_a_fact_its_row_stated_until_later(ch, org) -> None:
    # "I work at Acme" until next month, retired behind "I like Rome": Acme ends now on
    # both walks, as a revision away ends it.
    until = naive_utc_now() + timedelta(days=20)
    job = await _stated(
        ch, org, "s1", "I work at Acme", ("user", "work_at", "acme"), fact_valid_to=until
    )
    rome = await _stated(ch, org, "s2", "I like Rome")
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, job, rome)
    assert await _walked(org, ["rome"], labeled=True) == ["rome"]
    assert await _walked(org, ["rome"], labeled=False) == ["rome"]


async def test_a_retirement_ends_a_fact_stated_until_later_when_a_dated_winner_starts(
    ch, org
) -> None:
    # Paris until next month, retired behind Berlin from ten days on: Paris holds
    # until Berlin starts, as an open Paris would.
    from server.memory import store as M

    now = naive_utc_now().replace(microsecond=0)
    live, starts = ("user", "live_in"), now + timedelta(days=10)
    paris = await _stated(
        ch, org, "s1", "I live in Paris", (*live, "paris"), fact_valid_to=now + timedelta(days=30)
    )
    berlin = await _stated(ch, org, "s2", "I live in Berlin", (*live, "berlin"), valid_at=starts)
    berlin = {**berlin, "valid_at": starts, "version": M.next_version()}
    await M.insert_memory_versions([berlin], ch=ch)
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, paris, berlin)
    await asyncio.sleep(1.1)  # the walk counts a fact ended this second as current
    between = now + timedelta(days=5)
    assert await _walked(org, ["paris"], labeled=True, as_of=between) == ["paris"]
    assert await _walked(org, ["paris"], labeled=False, as_of=between) == ["paris"]
    later = starts + timedelta(days=1)
    assert await _walked(org, ["berlin"], labeled=True, as_of=later) == ["berlin"]


async def test_a_revision_ends_now_a_value_still_valid_until_a_later_move(ch, org) -> None:
    # After the Undo, a move to Berlin dated ahead ends London then. London's row is then
    # revised away: London ends now, and Paris holds until Berlin starts.
    london = await _london_undone_over_paris(ch, org, "live_in")
    moving = naive_utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    moving += timedelta(days=45)
    await _stated(
        ch,
        org,
        "s4",
        "I am moving to Berlin",
        ("user", "live_in", "berlin"),
        memory_key="k4",
        valid_at=moving,
    )
    await _mutations_done(ch, org)
    await _revise("memory.update_by_id", org, london, "I like Rome")
    await _mutations_done(ch, org)
    soon = naive_utc_now() + timedelta(seconds=2)
    assert await _walked(org, ["paris"], labeled=True, as_of=soon) == ["paris"]
    after = moving + timedelta(days=1)
    assert await _walked(org, ["berlin"], labeled=True, as_of=after) == ["berlin"]


async def test_a_value_written_while_a_revision_restates_another_stays_current(
    ch, org, monkeypatch
) -> None:
    # The revision drops London and finds no current value; a move to Berlin dated
    # ahead lands before Paris is stated again. Paris then ends when Berlin starts.
    from server.memory.app import graph_undo

    london = await _london_undone_over_paris(ch, org, "live_in")
    restate = graph_undo.restate_facts
    moving = naive_utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    moving += timedelta(days=45)

    async def _berlin_first(*a: Any, **k: Any) -> Any:
        monkeypatch.setattr(graph_undo, "restate_facts", restate)
        await _stated(
            ch,
            org,
            "s4",
            "I am moving to Berlin",
            ("user", "live_in", "berlin"),
            memory_key="k4",
            valid_at=moving,
        )
        return await restate(*a, **k)

    monkeypatch.setattr(graph_undo, "restate_facts", _berlin_first)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    want = {("live_in", "berlin"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want
    soon = naive_utc_now() + timedelta(seconds=2)
    assert await _walked(org, ["paris", "rome"], labeled=True, as_of=soon) == ["paris", "rome"]


async def test_a_batch_and_a_racing_writer_leave_one_current_value(ch, org, monkeypatch) -> None:
    # One write: London from Oct 15, then Berlin from Oct 14. Rome from Oct 14 arrives
    # later from another writer. Both repairs read all three rows before any close.
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
    from server.memory.app import projections

    repair = projections._repair_contradictory_triples
    arrived: list[int] = []
    both = asyncio.Event()

    async def _together(*a: Any, **k: Any) -> Any:
        arrived.append(1)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 10)
        return await repair(*a, **k)

    monkeypatch.setattr(projections, "_repair_contradictory_triples", _together)
    live = ("user", "live_in")
    batch = MemoryBulkSetInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        entries=[
            {
                "memory_key": f"k-{city}",
                "value": f"I live in {city}",
                "relations": [(*live, city)],
                "valid_at": day,
            }
            for city, day in (
                ("london", datetime(2026, 10, 15)),
                ("berlin", datetime(2026, 10, 14)),
            )
        ],
    )
    results = [
        _BulkSetResult(memory_key=e.memory_key, version=1, memory_id="") for e in batch.entries
    ]
    await asyncio.gather(
        projections.write_entity_triples(batch, results, ch=ch, entity_map={}),
        _stated(
            ch,
            org,
            "s3",
            "I live in Rome",
            (*live, "rome"),
            memory_key="k3",
            valid_at=datetime(2026, 10, 14),
        ),
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_two_undated_writers_racing_in_one_second_leave_one_current_value(
    ch, org, monkeypatch
) -> None:
    # Both writes insert before either repairs, so each repair reads the other's row.
    from server.memory.app import projections

    repair = projections._repair_contradictory_triples
    arrived: list[int] = []
    both = asyncio.Event()

    async def _together(*a: Any, **k: Any) -> Any:
        arrived.append(1)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 10)
        return await repair(*a, **k)

    monkeypatch.setattr(projections, "_repair_contradictory_triples", _together)
    live = ("user", "live_in")
    await asyncio.gather(
        _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1"),
        _stated(ch, org, "s2", "I live in Paris", (*live, "paris"), memory_key="k2"),
    )
    await _mutations_done(ch, org)
    # The later arrival, to the microsecond, is the one current value.
    later = await ch.query_one(
        "SELECT argMax(object, seen_at) AS city FROM loom.entity_triples"
        " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert await _open_cities(ch, org, {later["city"]}) == {later["city"]}


async def _edges_like_triples(ch: Any, org: str) -> None:
    """One edge per triple row, with its interval, as a row naming both would write."""
    # A triple whose close is still pending would be copied open, as a live edge.
    await _mutations_done(ch, org)
    triples = await ch.query_all(
        "SELECT subject, object, valid_from, valid_to, invalid_at, last_seen"
        " FROM loom.entity_triples"
        " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    edges = [
        {
            "org": org,
            "namespace": NS,
            "entity_a": min(t["subject"], t["object"]),
            "entity_b": max(t["subject"], t["object"]),
            "co_occur": 1,
            "last_seen": t["last_seen"],
            "valid_from": t["valid_from"],
            "valid_to": t["valid_to"],
            "invalid_at": t["invalid_at"],
        }
        for t in triples
    ]
    await ch.insert("loom.entity_edges", edges, settings={"wait_for_async_insert": 1})


async def _open_edges(ch: Any, org: str, want: set[str]) -> list[str]:
    """The open edges of `user`, by the other entity, waiting up to ten seconds for `want`."""
    for _ in range(50):
        got = await ch.query_all(
            "SELECT if(entity_a = 'user', entity_b, entity_a) AS other FROM loom.entity_edges"
            " WHERE org = {org:String} AND 'user' IN (entity_a, entity_b) AND valid_to IS NULL"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        others = sorted(str(r["other"]) for r in got)
        if set(others) == want:
            break
        await asyncio.sleep(0.2)
    return others


async def test_every_start_of_a_closed_value_closes_its_edge(ch, org) -> None:
    # London from January and from March, Paris from June: both London edges close.
    from types import SimpleNamespace

    from server.memory.app.projections import entity_triple_row, repair_intervals

    rows = [
        entity_triple_row(
            org,
            NS,
            "user",
            "live_in",
            "london",
            provenance="t",
            confidence=1.0,
            adapter_name="t",
            last_seen=datetime(2026, 9, 1),
            seen_at=datetime(2026, 9, 1),
            valid_from=start,
        )
        for start in (datetime(2026, 1, 1), datetime(2026, 3, 1))
    ]
    await ch.insert("loom.entity_triples", rows, settings={"wait_for_async_insert": 1})
    await _edges_like_triples(ch, org)
    paris = {
        "subject": "user",
        "relation": "live_in",
        "object": "paris",
        "valid_from": datetime(2026, 6, 1),
        "seen_at": datetime(2026, 9, 2),
    }
    scope = SimpleNamespace(org=org, namespace=NS)
    repairs = await repair_intervals(scope, [paris], ch=ch, at=datetime(2026, 9, 2))
    assert sorted(r.valid_from for r in repairs) == [datetime(2026, 1, 1), datetime(2026, 3, 1)]
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()
    assert await _open_edges(ch, org, set()) == []


async def test_an_undo_gives_a_conflicted_row_back_its_conflict(ch, org) -> None:
    # Retired while under review: its peer still lists it, so it returns to the queue.
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    paris = _row(org, "s1", "User lives in Paris", datetime(2023, 5, 30))
    peer = _row(org, "s9", "User lives in Rome", datetime(2023, 5, 30), memory_key="city")
    peer |= {"status": "conflicted", "conflict_with": [str(paris["memory_id"])]}
    await M.insert_memory_versions([peer], ch=ch)
    paris |= {"status": "conflicted", "conflict_with": [str(peer["memory_id"])]}
    london = _row(org, "s2", "User lives in London", datetime(2023, 6, 30))
    retired = {**paris, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(london["memory_id"])
    await M.insert_memory_versions([paris, london, retired], ch=ch)
    await _retired_embedding(ch, paris)
    assert (await handle_memory_restore(_undo_of(london))).restored
    head = await M.get_by_id(str(paris["memory_id"]), org=org, ch=ch, scope=WHOLE_NAMESPACE)
    assert (head.status, head.conflict_with) == ("conflicted", [str(peer["memory_id"])])
    # Search ranks on the embedding row's status: it carries the conflict too.
    assert await _embedding_status(ch, paris) == "conflicted"


@pytest.mark.parametrize("peer_end", ["deleted", "resolved"])
async def test_an_undo_after_its_peer_left_review_restores_active(ch, org, peer_end) -> None:
    # The peer was deleted or resolved while the row was retired: no live peer
    # lists it, so the Undo has nothing to be in conflict with.
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    paris = _row(org, "s1", "User lives in Paris", datetime(2023, 5, 30))
    peer = _row(org, "s9", "User lives in Rome", datetime(2023, 5, 30), memory_key="city")
    peer |= {"status": "conflicted", "conflict_with": [str(paris["memory_id"])]}
    paris |= {"status": "conflicted", "conflict_with": [str(peer["memory_id"])]}
    london = _row(org, "s2", "User lives in London", datetime(2023, 6, 30))
    retired = {**paris, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(london["memory_id"])
    if peer_end == "deleted":
        ended = {"status": "tombstoned", "tombstoned_at": datetime.now(UTC).replace(tzinfo=None)}
    else:
        ended = {"status": "active", "conflict_with": []}
    peer_ended = {**peer, "version": M.next_version(), **ended}
    await M.insert_memory_versions([paris, peer, london, retired, peer_ended], ch=ch)
    await _retired_embedding(ch, paris)
    assert (await handle_memory_restore(_undo_of(london))).restored
    head = await M.get_by_id(str(paris["memory_id"]), org=org, ch=ch, scope=WHOLE_NAMESPACE)
    assert head.status == "active"
    assert await _embedding_status(ch, paris) == "active"


async def _retired_embedding(ch: Any, row: dict[str, Any]) -> None:
    """`row`'s search row as its retirement left it: superseded, with a vector."""
    from server.memory import store as M

    cols = ("org", "namespace", "agent", "user_id", "session_id", "memory_key", "content_sha256")
    emb = M.build_embedding_row(
        **{c: row[c] for c in cols},
        embedding_model="itest",
        embedding_dim=3072,
        embedding=[0.01] * 3072,
        status="superseded",
        invalid_at=naive_utc_now(),
    )
    await M.insert_embedding_rows([emb], ch=ch)


async def _embedding_status(ch: Any, row: dict[str, Any]) -> str:
    found = await ch.query_one(
        "SELECT status FROM loom.memory_embeddings FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND session_id = {s:String} "
        "AND memory_key = {k:String} SETTINGS select_sequential_consistency = 1",
        params={"org": row["org"], "ns": NS, "s": row["session_id"], "k": row["memory_key"]},
    )
    return str(found["status"])


def _undo_of(winner: dict[str, Any]) -> Any:
    from server.memory.api.models_crud import MemoryRestoreInput

    return MemoryRestoreInput(
        org=winner["org"],
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        memory_key=KEY,
        if_superseded_by=str(winner["memory_id"]),
    )


async def _recorded_reconcile(ch: Any, org: str, projection: str = "triples_reconcile") -> Any:
    """The org's one recorded failure of `projection`, waiting for it to land."""
    from server.memory.app.projection_failures import list_projection_failures

    for _ in range(100):
        failures = await list_projection_failures(org=org, projection=projection, ch=ch)
        if failures:
            break
        await asyncio.sleep(0.1)
    (failure,) = failures
    return failure


async def test_a_replayed_undo_leaves_a_revision_since_to_its_own_reconcile(
    ch, org, monkeypatch
) -> None:
    # The Undo of London fails; the row is then revised to Berlin. The replay never
    # dates Berlin at the Undo: that revision's own reconcile owns the row.
    from server.memory.app import graph_undo
    from server.memory.app.projection_failures import retry_projection_failure
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    reopen = graph_undo.reopen_held_facts

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("reconcile down")

    monkeypatch.setattr(graph_undo, "reopen_held_facts", _fails)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    monkeypatch.setattr(graph_undo, "reopen_held_facts", reopen)
    failure = await _recorded_reconcile(ch, org)
    await _revise("memory.update_by_id", org, london, "I live in Berlin", ((*live, "berlin"),))
    await _mutations_done(ch, org)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    restores = await ch.query_all(
        "SELECT object FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert restores == []


async def test_a_second_undo_in_the_second_of_the_first_makes_its_value_current(
    ch, org, monkeypatch
) -> None:
    # Undo London, Rome supersedes it, Undo London again: all in one stored second.
    # The second Undo has its own instant: its own assertion, not a replay of the first.
    from server.memory.app import projections
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    second = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(seconds=1)
    first_undo, rome_at, second_undo = (second.replace(microsecond=us) for us in (1, 2, 3))
    _date_undos_at(monkeypatch, lambda: first_undo)
    monkeypatch.setattr(projections, "naive_utc_now", lambda: rome_at)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    rome = await _stated(ch, org, "s3", "I moved to Rome", (*live, "rome"), memory_key="k3")
    await _follow_retirement(ch, org, london, rome)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    _date_undos_at(monkeypatch, lambda: second_undo)
    assert (await handle_memory_restore(_undo_of(rome))).restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_dated_and_an_undated_writer_racing_leave_one_current_value(
    ch, org, monkeypatch
) -> None:
    # Undated London and Rome dated yesterday: both insert before either repairs, and
    # both agree on London, the later day, whichever arrived first.
    from server.memory.app import projections

    repair = projections._repair_contradictory_triples
    arrived: list[int] = []
    both = asyncio.Event()

    async def _together(*a: Any, **k: Any) -> Any:
        arrived.append(1)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 10)
        return await repair(*a, **k)

    monkeypatch.setattr(projections, "_repair_contradictory_triples", _together)
    live = ("user", "live_in")
    yesterday = _today() - timedelta(days=1)
    await asyncio.gather(
        _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1"),
        _stated(
            ch, org, "s2", "I live in Rome", (*live, "rome"), memory_key="k2", valid_at=yesterday
        ),
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_three_values_each_repaired_against_all_three_leave_one_current(
    ch, org, monkeypatch
) -> None:
    # A cycle under the old pairwise rules: A dated Oct 13 noon, B undated, C dated
    # Oct 14, arriving in the order C, B, A. Each repair reads all three open before
    # any close lands; by (start day, seen_at) all three agree that B is current.
    from types import SimpleNamespace

    from server.memory.app import projections

    def row(obj: str, valid_from: datetime | None, second: int) -> dict[str, Any]:
        seen = datetime(2026, 10, 15, 0, 0, second)
        return {
            "org": org,
            "namespace": NS,
            "subject": "user",
            "relation": "live_in",
            "object": obj,
            "provenance": "extracted",
            "co_occur": 1,
            "last_seen": seen,
            "seen_at": seen,
            "confidence": 1.0,
            "adapter_name": "itest",
            "valid_from": valid_from,
            "valid_to": None,
            "invalid_at": None,
        }

    rows = [
        row("a", datetime(2026, 10, 13, 12), 3),
        row("b", None, 2),
        row("c", datetime(2026, 10, 14), 1),
    ]
    await ch.insert("loom.entity_triples", rows, settings={"wait_for_async_insert": 1})
    close = projections.close_triples
    read: list[int] = []
    all_read = asyncio.Event()

    async def _after_every_read(*a: Any, **k: Any) -> Any:
        read.append(1)
        if len(read) == len(rows):
            all_read.set()
        await asyncio.wait_for(all_read.wait(), 10)
        return await close(*a, **k)

    monkeypatch.setattr(projections, "close_triples", _after_every_read)
    scope = SimpleNamespace(org=org, namespace=NS)
    await asyncio.gather(
        *(projections.repair_intervals(scope, [r], ch=ch, at=r["seen_at"]) for r in rows)
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"b"}) == {"b"}


async def test_a_replayed_undo_leaves_a_value_asserted_later_in_its_second_current(
    ch, org, monkeypatch
) -> None:
    # The Undo of London at T lands, then fails. Rome, dated T, is asserted later in
    # the same second and is current. Replayed with its own instant, the Undo keeps
    # its place: before Rome, so Rome stays current.
    from server.memory.app import graph_undo, projections
    from server.memory.app.projection_failures import retry_projection_failure
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    second = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(seconds=1)
    undo_at, rome_at = second.replace(microsecond=100), second.replace(microsecond=200)
    _date_undos_at(monkeypatch, lambda: undo_at)
    repair = graph_undo.repair_intervals

    async def _lands_then_fails(*a: Any, **k: Any) -> Any:
        await repair(*a, **k)
        raise RuntimeError("reconcile down after it landed")

    monkeypatch.setattr(graph_undo, "repair_intervals", _lands_then_fails)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    monkeypatch.setattr(graph_undo, "repair_intervals", repair)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    monkeypatch.setattr(projections, "naive_utc_now", lambda: rome_at)
    await _stated(
        ch, org, "s3", "I live in Rome", (*live, "rome"), memory_key="k3", valid_at=second
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    assert (await retry_projection_failure(await _recorded_reconcile(ch, org), ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}


async def test_a_replayed_insert_of_an_older_value_leaves_the_newer_one_current(
    ch, org, monkeypatch
) -> None:
    # London's insert fails, then Paris is stated. The replay inserts London dated at
    # its own write, so it lands behind Paris instead of closing it.
    from server.memory.app import projections
    from server.memory.app.projection_errors import bulk_projection_payload
    from server.memory.app.projection_failures import ProjectionFailureRow, retry_projection_failure

    lives = ("user", "live_in", "london")
    _row, write, results = await _row_and_write(
        ch, org, "s1", "I currently live in London", [lives]
    )
    insert = projections._insert_triples

    async def _insert_fails(*args: Any, **kw: Any) -> None:
        raise RuntimeError("insert timed out")

    monkeypatch.setattr(projections, "_insert_triples", _insert_fails)
    with pytest.raises(Exception) as raised:
        await projections.write_entity_triples(write, results, ch=ch, entity_map={})
    monkeypatch.setattr(projections, "_insert_triples", insert)
    await _stated(ch, org, "s2", "I currently live in Paris", ("user", "live_in", "paris"))
    payload = bulk_projection_payload(write, results, error=raised.value)
    failure = ProjectionFailureRow(
        failure_id=str(uuid.uuid4()),
        org=org,
        namespace=NS,
        agent="bot",
        projection="entity_triples",
        source_operation="memory.bulk_set",
        source_resource_id="",
        memory_keys=[results[0].memory_key],
        error_type="RuntimeError",
        error_message="insert timed out",
        payload=json.loads(json.dumps(payload)),
    )
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "paris")}) == {("live_in", "paris")}


async def _mentions(ch: Any, org: str, row: dict[str, Any], *entities: str) -> None:
    """The co-occurrence edges `row` writes for `entities`, as its write would."""
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
    from server.memory.app.projections import write_entity_edges

    key = row["memory_key"]
    write = MemoryBulkSetInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id=row["session_id"],
        entries=[{"memory_key": key, "value": json.loads(row["value"]), "entities": entities}],
    )
    result = _BulkSetResult(memory_key=key, version=row["version"], memory_id=str(row["memory_id"]))
    await write_entity_edges(write, [result], ch=ch, entity_map={}, wait_visible=True)


async def _walks_to(org: str, entity: str, *, seed: str = "alice") -> bool:
    """Whether one unlabelled hop from `seed` reaches `entity`, waiting up to two
    seconds for it."""
    return entity in await _walked(
        org, [entity], labeled=False, seed=seed, until=lambda w: entity in w
    )


async def test_a_dropped_facts_edge_stays_open_while_another_row_names_both(ch, org) -> None:
    # A trip row names Alice and London and states no relation. London's row is
    # revised away: its fact ends, the trip row's edge does not.
    likes = ("alice", "like", "london")
    london = await _stated(ch, org, "s1", "Alice likes London", likes)
    trip = await _stated(
        ch, org, "s2", "Alice's trip to London", extracted=False, memory_key="trip"
    )
    await _mentions(ch, org, london, "alice", "london")
    await _mentions(ch, org, trip, "alice", "london")
    await _revise("memory.update_by_id", org, london, "Alice likes Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walked(org, [], labeled=True, seed="alice") == []
    assert await _walks_to(org, "london")


async def test_a_dropped_facts_edge_stays_open_while_a_previous_build_row_names_both(
    ch, org
) -> None:
    # Mid-rollout the previous build writes the trip row's edge with a key link and no
    # row link (it links no rows). London's row is revised away: the trip row's edge stays.
    likes = ("alice", "like", "london")
    london = await _stated(ch, org, "s1", "Alice likes London", likes)
    trip = await _stated(
        ch, org, "s2", "Alice's trip to London", extracted=False, memory_key="trip"
    )
    await _mentions(ch, org, london, "alice", "london")
    await _mentions(ch, org, {**trip, "memory_id": ""}, "alice", "london")
    await _revise("memory.update_by_id", org, london, "Alice likes Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, "london")


async def test_a_dropped_facts_edge_stays_open_while_the_same_key_in_another_session_names_both(
    ch, org
) -> None:
    # Two sessions keep one key: s1 likes London, s2 is a trip there. s1's row is
    # revised away; s2's row still names Alice and London, so the edge stays open.
    likes = ("alice", "like", "london")
    london = await _stated(ch, org, "s1", "Alice likes London", likes)
    trip = await _stated(ch, org, "s2", "Alice's trip to London", extracted=False)
    await _mentions(ch, org, london, "alice", "london")
    await _mentions(ch, org, trip, "alice", "london")
    await _revise("memory.update_by_id", org, london, "Alice likes Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, "london")


async def test_a_key_that_no_longer_names_both_keeps_no_edge_open(ch, org) -> None:
    # Two keys say Alice lives in Paris. The second is revised to jazz, then the first
    # to blues: the second key's old link names the pair, but its value no longer does.
    lives = ("alice", "live_in", "paris")
    first = await _stated(ch, org, "s1", "Alice lives in Paris", lives, memory_key="k1")
    second = await _stated(ch, org, "s2", "Alice lives in Paris", lives, memory_key="k2")
    await _mentions(ch, org, first, "alice", "paris")
    await _mentions(ch, org, second, "alice", "paris")
    await _revise("memory.update_by_id", org, second, "Alice likes jazz")
    await _mutations_done(ch, org)
    await _revise("memory.update_by_id", org, first, "Alice likes blues")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walked(org, [], labeled=True, seed="alice") == []
    # The revisions' own edges (jazz, blues) are walked; Paris is not.
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "paris" not in w
    )
    assert "jazz" in walked and "paris" not in walked


@pytest.mark.parametrize(
    ("city", "trip_value"),
    [("new york city", "Alice's trip to NYC"), ("北京市", "Alice去北京市旅行")],
)
async def test_a_dropped_facts_edge_stays_open_while_a_row_states_the_pair_in_other_words(
    ch, org, city, trip_value
) -> None:
    # The trip row's entities are Alice and the city, though its text spells the city
    # another way, or with no space around it. The city's row is revised away: the
    # trip row still states the pair, so the edge stays open.
    liked = await _stated(ch, org, "s1", f"Alice likes {city}", ("alice", "like", city))
    trip = await _stated(ch, org, "s2", trip_value, extracted=False, memory_key="trip")
    await _mentions(ch, org, liked, "alice", city)
    await _mentions(ch, org, trip, "alice", city)
    await _revise("memory.update_by_id", org, liked, "Alice likes Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, city)


@pytest.mark.parametrize("path", ["memory.update_by_id", "memory.set"])
async def test_a_revision_that_still_names_the_pair_keeps_its_edge(
    ch, org, path, monkeypatch
) -> None:
    # No other row names Alice and London. The revision drops the fact but its new
    # value names both: its own edge stays open, with the weight of both mentions.
    from server.memory.app.extract import handler

    project = handler.project_memory_set_entities

    async def slow_projection(*a: Any, **kw: Any) -> Any:
        # memory.set's first-write projection runs beside the response; slowed here.
        await asyncio.sleep(1.5)
        return await project(*a, **kw)

    monkeypatch.setattr(handler, "project_memory_set_entities", slow_projection)
    london = await _stated(ch, org, "s1", "Alice likes London", ("alice", "like", "london"))
    await _mentions(ch, org, london, "alice", "london")
    await _mentions(ch, org, london, "alice", "london")
    await _revise(path, org, london, "Alice took a trip to London")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, "london")
    rows = await ch.query_all(
        "SELECT co_occur, valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'alice' AND entity_b = 'london'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [(r["co_occur"], r["valid_to"]) for r in rows] == [(1, None)] * 3


async def test_a_retired_rows_revision_projects_no_edges(ch, org) -> None:
    # London's row retired behind Paris's and is then revised; not embedded, it is not
    # judged again and stays retired. It holds no graph facts, so its new value's edges
    # are not written.
    london = await _stated(
        ch, org, "s1", "Alice likes London", ("alice", "like", "london"), memory_type="episodic"
    )
    paris = await _stated(ch, org, "s2", "Alice likes Paris", ("alice", "like", "paris"))
    await _mentions(ch, org, paris, "alice", "paris")  # what the walk does reach
    await _follow_retirement(ch, org, london, paris)
    await _revise("memory.update_by_id", org, london, "Alice works at Umbrella")
    await _mutations_done(ch, org)
    walked = await _walked(org, [], labeled=False, seed="alice", until=lambda w: "paris" in w)
    assert "paris" in walked and "umbrella" not in walked


async def test_a_row_naming_the_pair_while_its_edge_ends_keeps_it_open(ch, org) -> None:
    # The trip row's edge lands after the close checked the pair and before its UPDATE:
    # the check after the UPDATE finds the row's link and opens the edge again.
    from server.memory.app.projections import end_edges, entity_edge_row

    trip = await _stated(
        ch, org, "s2", "Alice's trip to London", extracted=False, memory_key="trip"
    )
    edge = entity_edge_row(org, NS, "alice", "london", last_seen=naive_utc_now())
    await ch.insert("loom.entity_edges", [edge], settings={"wait_for_async_insert": 1})

    class _Racing:
        """`ch`, with the trip row written right after the first read."""

        def __init__(self) -> None:
            self.reads = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(ch, name)

        async def query_all(self, *a: Any, **k: Any) -> Any:
            found = await ch.query_all(*a, **k)
            self.reads += 1
            if self.reads == 1:
                await _mentions(ch, org, trip, "alice", "london")
            return found

    await end_edges(_Racing(), org=org, namespace=NS, ends=[("alice", "london", naive_utc_now())])
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, "london")


@pytest.mark.parametrize("racing", [True, False])
async def test_a_row_naming_the_pair_while_its_edge_ends_keeps_its_stated_end(
    ch, org, racing: bool
) -> None:
    # The edge is stated to hold until next year. The trip row names the pair while the
    # close runs: the edge keeps that end, it is neither cut short nor opened forever.
    # With no row naming it, the close ends it now.
    from server.memory.app.projections import end_edges, entity_edge_row

    trip = await _stated(
        ch, org, "s2", "Alice's trip to London", extracted=False, memory_key="trip"
    )
    until = naive_utc_now().replace(microsecond=0) + timedelta(days=365)
    edge = entity_edge_row(org, NS, "alice", "london", last_seen=naive_utc_now())
    edge["valid_to"] = until
    await ch.insert("loom.entity_edges", [edge], settings={"wait_for_async_insert": 1})

    class _Racing:
        """`ch`, with the trip row written right after the first read."""

        def __init__(self) -> None:
            self.reads = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(ch, name)

        async def query_all(self, *a: Any, **k: Any) -> Any:
            found = await ch.query_all(*a, **k)
            self.reads += 1
            if self.reads == 1 and racing:
                await _mentions(ch, org, trip, "alice", "london")
            return found

    now = naive_utc_now().replace(microsecond=0)
    await end_edges(_Racing(), org=org, namespace=NS, ends=[("alice", "london", now)])
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'alice' AND entity_b = 'london' AND valid_to IS NOT NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r["valid_to"] for r in rows] == [until if racing else now]


async def test_a_fact_that_ended_keeps_no_edge_of_a_dropped_one_open(ch, org) -> None:
    # A live row says London was home until 2020. London's liked row is revised
    # away: the pair's only fact valid now is gone, so the edge ends.
    lived = ("user", "live_in", "london")
    await _stated(
        ch, org, "s1", "I lived in London", lived, memory_key="home", fact_valid_to=_past()
    )
    london = await _stated(ch, org, "s2", "I like London")
    await _edges_like_triples(ch, org)
    await _revise("memory.update_by_id", org, london, "I like Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walked(org, [], labeled=False) == []


class _Counted:
    """`ch`, counting the statements sent through it."""

    def __init__(self, ch: Any) -> None:
        self._ch, self.sent = ch, 0

    def __getattr__(self, name: str) -> Any:
        call = getattr(self._ch, name)
        if name not in ("query_all", "execute", "insert"):
            return call

        async def counted(*a: Any, **k: Any) -> Any:
            self.sent += 1
            return await call(*a, **k)

        return counted


async def test_a_follow_of_one_retirement_sends_few_statements(ch, org) -> None:
    # London's row retires behind Paris's: on the bulk write's response path.
    from types import SimpleNamespace

    from server.memory import store as M
    from server.memory.app.entity_projections import follow_row_supersessions

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _edges_like_triples(ch, org)
    retired = {**london, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(paris["memory_id"])
    await M.insert_memory_versions([retired], ch=ch)
    counted = _Counted(ch)
    scope = SimpleNamespace(org=org, namespace=NS)
    pair = (str(london["memory_id"]), str(paris["memory_id"]))
    await follow_row_supersessions(scope, [pair], ch=counted)
    # Three to plan, six after: the facts the loser linked and which a live row holds
    # (two), the later ends, one read of the pair's and the relation's triples, and the
    # edges to end. Twelve when the edge check asked again what was just asked.
    assert counted.sent == 9


@pytest.mark.parametrize("n", [2, 8])
async def test_a_bulk_checking_its_revised_rows_edges_reads_once(ch, org, n) -> None:
    # A re-ingest revising every key asks once which rows still hold their values,
    # not once per row; an entry naming fewer than two entities wrote no edge to end.
    from server.core.hashing import sha256_value
    from server.memory import store as M
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
    from server.memory.app.bulk_set import _close_overtaken_edges

    entries, results = [], []
    for i in range(n):
        value = f"Alice met Bob in city {i}"
        row = _row(
            org,
            "s1",
            value,
            datetime(2026, 5, 1),
            memory_key=f"k{i}",
            content_sha256=sha256_value(value),
        )
        await M.insert_memory_versions([row], ch=ch)
        entities = ["alice", "bob"] if i % 2 else []
        entries.append({"memory_key": f"k{i}", "value": value, "entities": entities})
        results.append(
            _BulkSetResult(
                memory_key=f"k{i}", version=row["version"], memory_id=str(row["memory_id"])
            )
        )
    write = MemoryBulkSetInput(org=org, namespace=NS, agent="bot", user_id=USER, entries=entries)
    counted = _Counted(ch)
    revised = {r.memory_id for r in results}
    await _close_overtaken_edges(write, results, revised, entity_map={}, ch=counted)
    assert counted.sent == 1


async def _bulk_beside_a_pin(org: str, entries: list[dict[str, Any]]) -> Any:
    """A pinned row says the deploy window, then one bulk writes `entries` in a fresh
    session. Returns the bulk's output."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, bulk_set
    from server.memory.app.write import handle_memory_set

    ids = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER}
    await handle_memory_set(
        MemorySetInput(**ids, session_id="s0", memory_key="window", value=_PINNED, pinned=True)
    )
    out = await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(**ids, session_id="s1", entries=entries)
    )
    await after_write.drain()
    return out


_PINNED = "The deploy window is Tuesday and Thursday only, never Friday."
# Close to the pinned row, not its bytes (those would be an identity duplicate).
_CLOSE = "The deploy window is Tuesday and Thursday only, never on a Friday."


async def _stored_under(ch: Any, org: str, key: str) -> list[dict[str, Any]]:
    return await ch.query_all(
        "SELECT value, pinned FROM loom.memory FINAL WHERE org = {org:String}"
        " AND memory_key = {key:String} AND tombstoned_at IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "key": key},
    )


async def test_a_restatement_of_a_rejected_bulk_entry_is_rejected_too(ch, org) -> None:
    # Both entries are close to the pinned row: memory.set would refuse each one, so the
    # second must not answer an unchanged no-op the SDK reports as a clear write.
    same = {"memory_key": "plan", "value": _CLOSE}
    out = await _bulk_beside_a_pin(org, [same, same])
    assert [(r.version, r.conflict_verdict) for r in out.written] == [(0, "rejected")] * 2
    assert out.rejected_pinned == ["plan", "plan"]
    assert await _stored_under(ch, org, "plan") == []


async def test_a_pin_on_a_rejected_bulk_entry_does_not_gate_the_next_value(ch, org) -> None:
    # The first entry asks for a pin and is refused, so the key still does not exist:
    # the second, unrelated value lands as its own unpinned write.
    out = await _bulk_beside_a_pin(
        org,
        [
            {"memory_key": "plan", "value": _CLOSE, "pinned": True},
            {"memory_key": "plan", "value": "Ship the release notes on Monday."},
        ],
    )
    written = out.written
    assert (written[0].version, written[0].conflict_verdict) == (0, "rejected")
    assert written[1].version > 0 and written[1].conflict_verdict != "pinned"
    stored = await _stored_under(ch, org, "plan")
    assert [(json.loads(r["value"]), r["pinned"]) for r in stored] == [
        ("Ship the release notes on Monday.", 0)
    ]


class _HolderLandsAfterTheRead:
    """`ch`, where a row naming user-london lands right after end_edges' first read:
    a write racing the close."""

    def __init__(self, ch: Any, holder: Callable[[], Any]) -> None:
        self._ch, self._holder, self._reads = ch, holder, 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ch, name)

    async def query_all(self, *a: Any, **k: Any) -> Any:
        out = await self._ch.query_all(*a, **k)
        self._reads += 1
        if self._reads == 1:
            await self._holder()
        return out


@pytest.mark.parametrize("with_open_edge", [False, True], ids=["finite-only", "open-too"])
async def test_a_racing_holder_reopens_only_the_edges_the_close_ended(
    ch, org, with_open_edge
) -> None:
    # An old London edge already ended in the very second this close ends at. A row
    # naming the pair lands during the close, so what the close ended opens again; the
    # old edge was not ended by it and keeps its end.
    from server.memory import store as M
    from server.memory.app.projections import end_edges

    end = datetime(2026, 6, 1, 12, 0, 0)
    edge = {"org": org, "namespace": NS, "entity_a": "london", "entity_b": "user"}
    edges = [
        {**edge, "valid_from": datetime(2026, 1, 1), "valid_to": end},
        {**edge, "valid_from": datetime(2026, 5, 1), "valid_to": end + timedelta(days=30)},
    ]
    if with_open_edge:
        edges.append({**edge, "valid_from": datetime(2026, 5, 15), "valid_to": None})
    await ch.insert("loom.entity_edges", edges)
    holder = _row(org, "s9", "I live in London", datetime(2026, 5, 20), memory_key="home")

    async def lands() -> None:
        await M.insert_memory_versions([holder], ch=ch)
        link = {k: holder[k] for k in ("org", "namespace", "memory_id", "user_id")}
        sha = holder["content_sha256"]
        await ch.insert("loom.entity_edge_rows", [{**link, "content_sha256": sha, **edge}])

    racing = _HolderLandsAfterTheRead(ch, lands)
    await end_edges(racing, org=org, namespace=NS, ends=[("london", "user", end)])
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT valid_from, valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " ORDER BY valid_from SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    got = [(r["valid_from"].replace(tzinfo=None), r["valid_to"]) for r in rows]
    assert got[0] == (datetime(2026, 1, 1), end)
    assert got[1] == (datetime(2026, 5, 1), end + timedelta(days=30))
    if with_open_edge:
        assert got[2] == (datetime(2026, 5, 15), None)


@pytest.mark.parametrize("racing", [False, True], ids=["no-holder", "racing-holder"])
async def test_end_edges_closes_a_pair_seen_thousands_of_times(ch, org, racing) -> None:
    # One edge row per observation: a pair seen 3000 times has 3000 open rows, more
    # than one query parameter holds. The close ends all of them, and a row naming
    # the pair that lands during the close opens all of them again.
    from server.memory import store as M
    from server.memory.app.projections import end_edges

    end = datetime(2026, 6, 1, 12, 0, 0)
    edge = {"org": org, "namespace": NS, "entity_a": "london", "entity_b": "user"}
    start = datetime(2026, 1, 1)
    await ch.insert(
        "loom.entity_edges",
        [{**edge, "valid_from": start + timedelta(minutes=i)} for i in range(3000)],
    )
    holder = _row(org, "s9", "I live in London", datetime(2026, 5, 20), memory_key="home")

    async def lands() -> None:
        await M.insert_memory_versions([holder], ch=ch)
        link = {k: holder[k] for k in ("org", "namespace", "memory_id", "user_id")}
        sha = holder["content_sha256"]
        await ch.insert("loom.entity_edge_rows", [{**link, "content_sha256": sha, **edge}])

    closing = _HolderLandsAfterTheRead(ch, lands) if racing else ch
    await end_edges(closing, org=org, namespace=NS, ends=[("london", "user", end)])
    await _mutations_done(ch, org)
    (row,) = await ch.query_all(
        "SELECT countIf(valid_to IS NULL) AS open, countIf(valid_to = {end:DateTime}) AS ended"
        " FROM loom.entity_edges WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "end": end},
    )
    assert (row["open"], row["ended"]) == ((3000, 0) if racing else (0, 3000))


async def test_the_skip_log_counts_an_entry_a_landed_pin_refused(ch, org, caplog) -> None:
    # The first entry lands pinned, so the second is refused: the skip log, written once
    # the verdicts settle the batch, names it as pinned.
    import logging

    caplog.set_level(logging.INFO, logger="server.memory.app.bulk_set")
    out = await _bulk_beside_a_pin(
        org,
        [
            {"memory_key": "plan", "value": "Ship the release notes on Monday.", "pinned": True},
            {"memory_key": "plan", "value": "Ship the release notes on Wednesday."},
        ],
    )
    assert out.written[1].conflict_verdict == "pinned"
    assert "skipped 1 entry targeting pinned key(s): ['plan']" in caplog.text
