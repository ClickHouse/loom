"""The same-key judge against ClickHouse: the peer read, its limits and the winner
re-read run as SQL on real `loom.memory` rows; only the LLM is stubbed."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
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
    ch: Any, org: str, loser: dict[str, Any], winner: dict[str, Any]
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


async def _legacy_stated(ch: Any, org: str, value: str, triple: tuple, key: str) -> dict[str, Any]:
    """A row written before rows were linked to triples: its triple is linked to its
    key only. Returns the row."""
    from server.memory import store as M
    from server.memory.app.projections import entity_triple_row

    now = naive_utc_now()
    row = _row(org, "s0", value, now, memory_key=key)
    row["created_at"] = row["updated_at"] = datetime(2020, 1, 1)  # before row links
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
    await ch.insert("loom.entity_triple_memories", [{**link, "memory_key": key}])
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


async def test_undo_brings_back_the_facts_a_row_predating_row_links_holds(ch, org) -> None:
    # The follow closes a legacy row's facts by what its value names; the Undo has no
    # row link to find them by, and reopens them by the same rule.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore

    jazz, rock = ("alice", "like", "jazz"), ("alice", "like", "rock")
    legacy = await _legacy_stated(ch, org, "Alice likes jazz", jazz, KEY)
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
    want = {("live_in", "london"), ("work_in", "london"), ("live_in", "berlin")}
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
    want = {"компания", "initech"}
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
        # The same words, and no relation saying otherwise: the successor holds.
        ("I currently live in London.", (), {("live_in", "london"), ("live_in", "berlin")}),
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
    berlin = await _stated(ch, org, "s4", "I currently live in Berlin", extracted=False)
    await _follow_retirement(ch, org, other, berlin)
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, want) == want
    # What a reader is told: London is still where the user lives, or no longer. The
    # wait above for `valid_to IS NULL` comes first: facts.list alone still lists a
    # fact closed this very second.
    lives = {o for r, o in want if r == "live_in"}
    assert ("london" in await _listed(org, "live_in", lives)) == ("london" in lives)


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
    want = {("resid_in", "london"), ("live_in", "london"), ("live_in", "berlin")}
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
    org: str, want: list[str], *, labeled: bool, as_of: datetime | None = None
) -> list[str]:
    """The entities one hop of `facts.traverse` from `user` reaches (at `as_of`, when
    given), waiting up to two seconds for `want`: it counts a fact closed this very
    second as current."""
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app.facts import handle_facts_traverse

    walk = FactsTraverseInput(
        org=org, namespace=NS, seed_entity="user", max_hops=1, labeled=labeled, as_of=as_of
    )
    for _ in range(10):
        out = await handle_facts_traverse(walk)
        walked = sorted(n.entity for n in out.nodes)
        if walked == want:
            break
        await asyncio.sleep(0.2)
    return walked


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
        " AND adapter_name = 'row-restore' ORDER BY object"
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
        " AND adapter_name = 'row-restore' ORDER BY object"
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
        " AND adapter_name = 'row-restore' SETTINGS select_sequential_consistency = 1",
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
    assert len(await _open_cities(ch, org, {"paris"})) == 1


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


async def _recorded_reconcile(ch: Any, org: str) -> Any:
    from server.memory.app.projection_failures import TRIPLES_RECONCILE, list_projection_failures

    for _ in range(100):
        failures = await list_projection_failures(org=org, projection=TRIPLES_RECONCILE, ch=ch)
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
