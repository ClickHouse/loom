"""Same-key retirements against ClickHouse: a later row of a key retires the
earlier one, the record of what retired, its Undo, and the intents the repair replays."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core.datetimes import naive_utc_now
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from tests.integration.same_key._rows import (
    _CLOSE,
    KEY,
    NS,
    USER,
    _bulk_beside_a_pin,
    _bulk_in,
    _Counted,
    _edges_like_triples,
    _live_values,
    _newest_wins_two_offices,
    _open_facts,
    _owed,
    _records,
    _replay_owed,
    _restate_after_a_rewrite,
    _retire,
    _row,
    _stated,
    _status,
    _stored_under,
    _undo,
)


async def test_undo_restores_only_the_retirement_it_lists(ch, org) -> None:
    # Irish retired behind Japanese, rewritten by its own session, then retired again
    # behind Korean: the first record no longer describes it.
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    irish = _row(org, "s2", "User is Irish", datetime(2023, 5, 1))
    await M.insert_memory_versions([irish], ch=ch)
    japanese = await _retire(ch, org, "User is Japanese", datetime(2023, 6, 1), "s3")
    head = await M.get_by_id(
        irish["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE, namespace=NS
    )
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
    ch, org, monkeypatch, revised
) -> None:
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
        str(irish["memory_id"]), org=org, ch=ch, strong=True, scope=WHOLE_NAMESPACE, namespace=NS
    )
    assert "Brazilian" in str(head.value)
    if revised == "under_the_lock":
        assert undone.restored and undone.version < head.version
    else:  # the revision made the row live again: nothing to undo
        assert undone.restored is False
    assert not undone.restored or undone.version < head.version


async def test_undo_survives_a_re_emit_and_refuses_a_later_delete(ch, org) -> None:
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    irish = _row(org, "s2", "User is Irish", datetime(2023, 5, 1))
    await M.insert_memory_versions([irish], ch=ch)
    winner = await _retire(ch, org, "User is Japanese", datetime(2023, 6, 1), "s3")
    # The entity tagger re-emits a retired row under a new version, retirement kept.
    head = await M.get_by_id(
        irish["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE, namespace=NS
    )
    await M.insert_memory_versions(
        [M.build_memory_version_row(head, version=M.next_version(), entities=["irish"])], ch=ch
    )
    assert (await handle_memory_restore(_undo(org, winner))).restored is True

    # Retired again, then deleted by hand: the Undo leaves the delete alone.
    winner = await _retire(ch, org, "User is Korean", datetime(2023, 8, 1), "s4")
    head = await M.get_by_id(
        irish["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE, namespace=NS
    )
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
        head = await M.get_by_id(
            row["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE, namespace=NS
        )
        revision = M.build_memory_version_row(head, version=M.next_version(), lock_mode="no_delete")
        await insert([revision], ch=ch)
        await insert(rows, ch=ch)

    monkeypatch.setattr(lifecycle, "insert_memory_versions", revision_first)
    tracker = RetirementTracker()
    victims = [{**row, "superseded_by": "winner"}]
    n = await M.supersede_duplicates(
        org,
        victims,
        ch=ch,
        skip=tracker.skip,
        strong=True,
        on_written=tracker.written,
        namespace=NS,
    )
    assert n == 0 and tracker.retired(victims) == []
    head = await M.get_by_id(row["memory_id"], org=org, ch=ch, scope=WHOLE_NAMESPACE, namespace=NS)
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
        org,
        victims,
        ch=ch,
        skip=tracker.skip,
        strong=True,
        on_written=tracker.written,
        namespace=NS,
    )
    assert n == 0 and tracker.retired(victims) == []


async def test_a_retirement_chain_is_read_to_its_end(ch, org) -> None:
    # a -> b -> c with b's link in capitals; d retired behind an erased row; e and f
    # behind ids ClickHouse can't parse as UUIDs, which end their chains.
    import uuid
    from types import SimpleNamespace

    from server.memory import store as M
    from server.memory.app.triple_follow import _chain_head, _rows_along_chains

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


async def test_a_restated_merge_lands_once_its_target_was_rewritten_in_between(
    ch, org, monkeypatch
) -> None:
    # alias merges into target's Berlin; target is then rewritten; alias is said again.
    # Serially the second alias meets the bicycle, not Berlin, and lands: the store must
    # keep the Berlin fact.
    from server.memory.app import bulk_set
    from server.memory.domain.conflicts import ConflictPeer, ConflictResult

    first = await _bulk_in(org, "s1", {"memory_key": "target", "value": "Alice lives in Berlin."})
    target_id = first.written[0].memory_id
    peer = ConflictPeer(
        memory_id=target_id,
        memory_key="target",
        similarity=0.97,
        pinned=False,
        canonical="",
        value="Alice lives in Berlin.",
    )
    merge = ConflictResult(verdict="merge", peers=[peer], existing_memory_id=target_id)

    async def alias_merges(*args: Any, **kw: Any) -> dict[int, Any]:
        return {0: merge}  # rows: alias, target; the second alias builds none

    monkeypatch.setattr(bulk_set, "_bulk_conflict_verdicts", alias_merges)
    out = await _bulk_in(
        org,
        "s1",
        {"memory_key": "alias", "value": "Alice lives in Berlin"},
        {"memory_key": "target", "value": "Alice owns a red bicycle."},
        {"memory_key": "alias", "value": "Alice lives in Berlin"},
    )
    verdicts = [(r.conflict_verdict, r.version > 0) for r in out.written]
    assert verdicts == [("merge", False), ("", True), ("clear", True)]
    assert [p.memory_id for p in out.written[0].peers] == [target_id]
    assert out.written[2].peers == []
    live = await _live_values(ch, org)
    assert live[("s1", "alias")] == ("Alice lives in Berlin", 0)
    assert live[("s1", "target")] == ("Alice owns a red bicycle.", 0)


async def test_a_restated_merge_that_lands_keeps_the_last_entrys_high_label(
    ch, org, monkeypatch
) -> None:
    # The last alias asks for 'high': serially it lands high, unembedded, out of search.
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search

    await _restate_after_a_rewrite(org, monkeypatch, sensitivity="high")
    (row,) = await ch.query_all(
        "SELECT sensitivity FROM loom.memory FINAL WHERE org = {org:String}"
        " AND memory_key = 'alias' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert row["sensitivity"] == "high"
    found = await handle_memory_search(
        MemorySearchInput(org=org, namespace=NS, agent="*", query="Where does Alice live?")
    )
    assert "alias" not in [h.memory_key for h in found.results]


async def test_a_plain_bulk_repeat_of_a_high_row_keeps_it_high_and_unembedded(ch, org) -> None:
    # set_from_messages names no sensitivity: a re-extracted 'high' fact stays 'high',
    # with no live vector, and is skipped as unchanged.
    fact = {"memory_key": "secret", "value": "Alice's door code is 4417"}
    await _bulk_in(org, "s1", {**fact, "sensitivity": "high"})
    out = await _bulk_in(org, "s1", fact)
    assert [(r.version, r.conflict_verdict) for r in out.written] == [(0, "")]
    (row,) = await ch.query_all(
        "SELECT sensitivity FROM loom.memory FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert row["sensitivity"] == "high"
    (count,) = await ch.query_all(
        "SELECT count() AS n FROM loom.memory_embeddings FINAL WHERE org = {org:String}"
        " AND tombstoned_at IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert count["n"] == 0


async def test_a_restated_merge_that_lands_keeps_the_last_entrys_pin(ch, org, monkeypatch) -> None:
    await _restate_after_a_rewrite(org, monkeypatch, pinned=True)
    assert (await _live_values(ch, org))[("s1", "alias")] == ("Alice lives in Berlin", 1)


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


async def test_a_follow_of_one_retirement_sends_few_statements(ch, org) -> None:
    # London's row retires behind Paris's: on the bulk write's response path.
    from types import SimpleNamespace

    from server.memory import store as M
    from server.memory.app.triple_follow import follow_row_supersessions

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
    await _close_overtaken_edges(write, results, revised, high=set(), entity_map={}, ch=counted)
    assert counted.sent == 1


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


async def test_a_pass_that_repaired_4000_failures_marks_them_all(ch: Any, org: str) -> None:
    """The ids of a mark go as one HTTP parameter, which ClickHouse caps at 128 KiB:
    4,000 UUIDs in one go is refused and every failure would stay active."""
    from server.memory.app.projection_failures import (
        FOLLOW_ROWS,
        ProjectionFailureRetryResult,
        list_projection_failures,
        mark_retried,
    )

    now = naive_utc_now()
    ids = [str(uuid.uuid4()) for _ in range(4000)]
    await ch.insert(
        "loom.memory_projection_failures",
        [
            {
                "failure_id": fid,
                "org": org,
                "namespace": NS,
                "agent": "bot",
                "projection": FOLLOW_ROWS,
                "source_operation": "",
                "source_resource_id": "",
                "memory_keys": [KEY],
                "error_type": "Error",
                "error_message": "",
                "payload": "{}",
                "status": "active",
                "created_at": now,
                "resolved_at": None,
            }
            for fid in ids
        ],
        settings={"async_insert": 0},
    )
    failures = await list_projection_failures(org=org, limit=5000, ch=ch)
    assert len(failures) == 4000
    await mark_retried(
        [
            (f, ProjectionFailureRetryResult(f.failure_id, f.projection, True, True))
            for f in failures
        ],
        ch=ch,
    )
    for _ in range(60):
        if not await list_projection_failures(org=org, limit=1, ch=ch):
            break
        await asyncio.sleep(0.5)
    assert not await list_projection_failures(org=org, limit=1, ch=ch)
    resolved = await list_projection_failures(org=org, status="resolved", limit=5000, ch=ch)
    assert len(resolved) == 4000


class _OnlyOrgs:
    """`ch` for the repair scan, limited to this test's orgs (the server holds others')."""

    def __init__(self, ch: Any, orgs: list[str]) -> None:
        self._ch, self._orgs = ch, orgs

    async def query_all(self, sql: str, params: Any = None) -> Any:
        sql = sql.replace("WHERE ", "WHERE org IN {only_orgs:Array(String)} AND ", 1)
        return await self._ch.query_all(sql, params={**(params or {}), "only_orgs": self._orgs})


async def test_failures_a_retry_would_leave_pending_hold_no_place_in_the_scan(
    ch: Any, org: str
) -> None:
    """A pass's oldest failures all from an org whose erasure failed (due a retry), or
    all follow intents within their grace, would come back `pending` on every pass;
    the scan leaves them out, so another org's failure is reached."""
    from server.memory.app.projection_failures import (
        EDGES_DROP_UNDECLARED,
        FOLLOW_OWED,
        projection_failure_row,
        scan_active_projection_failures,
    )

    erasing, young, other = f"{org}-x", f"{org}-y", f"{org}-z"
    await ch.insert(
        "loom.erasure_requests",
        [
            {
                "id": str(uuid.uuid4()),
                "org": erasing,
                "scope_type": "user",
                "scope_value": USER,
                "requester": "test",
                "status": "failed",
                "failure_reason": "boom",
                "version": 1,
            }
        ],
    )
    now = naive_utc_now()
    rows = []
    for i in range(3):
        r = projection_failure_row(
            org=erasing, namespace=NS, projection=EDGES_DROP_UNDECLARED, error="e", payload={}
        )
        r["created_at"] = datetime(2001, 1, 1, 0, i)
        rows.append(r)
        r = projection_failure_row(
            org=young, namespace=NS, projection=FOLLOW_OWED, error="e", payload={}
        )
        r["created_at"] = now - timedelta(seconds=60 - i)
        rows.append(r)
    r = projection_failure_row(
        org=other, namespace=NS, projection=EDGES_DROP_UNDECLARED, error="e", payload={}
    )
    r["created_at"] = now
    rows.append(r)
    await ch.insert("loom.memory_projection_failures", rows, settings={"async_insert": 0})
    try:
        scan = _OnlyOrgs(ch, [erasing, young, other])
        scanned = await scan_active_projection_failures(limit=3, ch=scan)
        assert [f.org for f in scanned] == [other]
    finally:
        await ch.execute(
            "DELETE FROM loom.erasure_requests WHERE org = {o:String}", params={"o": erasing}
        )


@pytest.mark.parametrize("bulk", [False, True])
async def test_a_retirement_closes_the_retired_rows_facts_and_opens_one_successor(
    ch, org, monkeypatch, bulk: bool
) -> None:
    """Under newest_wins s2's "Alice likes tennis" retires s1's "Alice likes chess" under
    the same key. `likes` holds many values, so no interval repair closes chess: the
    retirement's follow does, after the write's own relations, so tennis is open once."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, conflicts
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictResult

    async def _clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    monkeypatch.setattr(conflicts, "check_on_commit", _clear)
    now = datetime.now(UTC)
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": NS,
                "created_at": now,
                "updated_at": now,
                "active": True,
                "default_conflict_policy": "newest_wins",
            }
        ],
    )
    for session, game in (("s1", "chess"), ("s2", "tennis")):
        identity = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER}
        entry = {
            "memory_key": "alice_likes",
            "value": f"Alice likes {game}",
            "relations": [("alice", "likes", game)],
        }
        if bulk:
            await handle_memory_bulk_set(
                MemoryBulkSetInput(**identity, session_id=session, entries=[entry])
            )
        else:
            await handle_memory_set(MemorySetInput(**identity, session_id=session, **entry))
        await after_write.drain()
    assert await _open_facts(ch, org, {("like", "tennis")}) == {("like", "tennis")}
    rows = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'tennis' AND valid_to IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert int(rows[0]["n"]) == 1, "one open row of the successor fact"


async def test_a_point_write_retirement_whose_record_failed_gets_it_from_the_repair(
    ch, org, monkeypatch
) -> None:
    """The record of a newest_wins same-key retirement fails: the write leaves it owed
    (an intent with the entry and its reason), and the repair writes it, once."""
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, conflicts, projection_failures
    from server.memory.app.projection_failures import retry_projection_failures
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictResult
    from server.observability import audit

    async def _clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    monkeypatch.setattr(conflicts, "check_on_commit", _clear)
    now = datetime.now(UTC)
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": NS,
                "created_at": now,
                "updated_at": now,
                "active": True,
                "default_conflict_policy": "newest_wins",
            }
        ],
    )
    real = audit.emit

    async def no_record(*a: Any, **kw: Any) -> Any:
        if kw.get("event_type", a[0] if a else "") == "memory.auto_supersede":
            raise RuntimeError("audit insert failed")
        return await real(*a, **kw)

    ids = {}
    for session, city in (("s1", "Paris"), ("s2", "Rome")):
        if session == "s2":
            monkeypatch.setattr(audit, "emit", no_record)
        out = await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id=session,
                memory_key="office_city",
                value=f"The office is in {city}",
            )
        )
        ids[session] = out.memory_id
        await after_write.drain()
    monkeypatch.setattr(audit, "emit", real)

    async def records() -> list[dict[str, Any]]:
        rows = await ch.query_all(
            "SELECT subject FROM loom.audit_log WHERE org = {org:String}"
            " AND event_type = 'memory.auto_supersede' SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        return [json.loads(r["subject"]) for r in rows]

    assert await records() == []
    monkeypatch.setattr(projection_failures, "OWED_GRACE_S", 0)
    for _ in range(2):  # a second replay writes no second record
        await retry_projection_failures(org=org, projection="triples_follow_owed", ch=ch)
    (record,) = await records()
    assert record["reason"] == "same_key_newest_wins"
    assert [(e["memory_id"], e["superseded_by"]) for e in record["retired"]] == [
        (ids["s1"], ids["s2"])
    ]


async def test_a_point_write_leaves_its_retirement_intent_to_the_repair(
    ch, org, monkeypatch
) -> None:
    """The intent goes before the supersede and the record names it; the write marks
    nothing (no mutation in the request), and the repair resolves it, writing no
    second record."""
    ids = await _newest_wins_two_offices(ch, org, monkeypatch)
    (intent,) = await _owed(ch, org, "active")
    assert json.loads(intent["payload"])["pairs"] == [[ids["s1"], ids["s2"]]]
    assert intent["status"] == "active", "no mark in the request path"
    assert [r["request_id"] for r in await _records(ch, org)] == [intent["id"]]
    await _replay_owed(ch, org, monkeypatch)
    (intent,) = await _owed(ch, org, "resolved")
    assert intent["status"] == "resolved"
    assert len(await _records(ch, org)) == 1


async def test_a_retirement_stopped_before_its_record_is_recorded_by_the_repair(
    ch, org, monkeypatch
) -> None:
    ids = await _newest_wins_two_offices(ch, org, monkeypatch, stop="record")
    assert await _records(ch, org) == []
    await _replay_owed(ch, org, monkeypatch)
    (intent,) = await _owed(ch, org, "resolved")
    assert intent["status"] == "resolved"
    assert [r["request_id"] for r in await _records(ch, org)] == [intent["id"]]
    assert json.loads(intent["payload"])["pairs"] == [[ids["s1"], ids["s2"]]]


async def test_a_retirement_stopped_before_its_follow_is_followed_by_the_repair(
    ch, org, monkeypatch
) -> None:
    """The record landed but the follow beside the response did not run: the repair
    follows the intent's pair, then resolves it, and a second follow changes nothing."""
    from types import SimpleNamespace

    from server.memory.app import triple_follow

    ids = await _newest_wins_two_offices(ch, org, monkeypatch, stop="follow")
    followed: list[Any] = []
    real = triple_follow.follow_row_supersessions

    async def spy(scope: Any, pairs: Any, **kw: Any) -> Any:
        followed.append([tuple(p) for p in pairs])
        return await real(scope, pairs, **kw)

    monkeypatch.setattr(triple_follow, "follow_row_supersessions", spy)
    await _replay_owed(ch, org, monkeypatch)
    assert followed == [[(ids["s1"], ids["s2"])]]
    (intent,) = await _owed(ch, org, "resolved")
    assert intent["status"] == "resolved"

    async def facts() -> list[dict[str, Any]]:
        return await ch.query_all(
            "SELECT subject, relation, object, valid_to FROM loom.entity_triples"
            " WHERE org = {org:String} ORDER BY object, seen_at"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )

    before = await facts()
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await real(scope, [(ids["s1"], ids["s2"])], ch=ch)
    assert await facts() == before, "a second follow changes nothing"


async def test_a_retirement_intent_holds_no_value_and_leaves_with_its_user(
    ch, org, monkeypatch
) -> None:
    """The intent a newest_wins same-key retirement owes holds the retired row's ids and
    identity, not its value, and a user erasure deletes it."""
    from server.erasure.store import steps_ledgers
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, conflicts
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictResult

    async def _clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    monkeypatch.setattr(conflicts, "check_on_commit", _clear)
    now = datetime.now(UTC)
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": NS,
                "created_at": now,
                "updated_at": now,
                "active": True,
                "default_conflict_policy": "newest_wins",
            }
        ],
    )
    secret = "The office door code is plum-kestrel"
    for session, value in (("s1", secret), ("s2", "The office door code changed")):
        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id=session,
                memory_key="door_code",
                value=value,
            )
        )
        await after_write.drain()
    intents = await _owed(ch, org, "active")
    # Words of the value that are neither hex (so in no id) nor in the key: a hit is a leak.
    words = ("office", "plum", "kestrel")
    assert intents and not any(w in i["payload"] for i in intents for w in words)
    await steps_ledgers.delete_projection_failures_by_user(org, USER, ch=ch)
    left = await ch.query_all(
        "SELECT count() AS n FROM loom.memory_projection_failures WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert int(left[0]["n"]) == 0


async def test_a_retirement_whose_supersede_answer_was_lost_is_recorded_by_the_repair(
    ch, org, monkeypatch
) -> None:
    """The supersede lands but its answer is lost: the write records nothing, and its
    intent stays for the repair, which writes the record."""
    from server.memory.store import lifecycle

    real = lifecycle.insert_memory_versions

    async def lost(rows: Any, **kw: Any) -> Any:
        if any(str(r.get("status")) == "superseded" for r in rows):
            await real(rows, **kw)
            raise TimeoutError("answer lost after the insert landed")
        return await real(rows, **kw)

    monkeypatch.setattr(lifecycle, "insert_memory_versions", lost)
    await _newest_wins_two_offices(ch, org, monkeypatch)
    assert await _records(ch, org) == []
    (intent,) = await _owed(ch, org, "active")
    assert intent["status"] == "active"
    await _replay_owed(ch, org, monkeypatch)
    assert [r["request_id"] for r in await _records(ch, org)] == [intent["id"]]


async def test_point_write_intents_are_resolved_by_a_batched_flush(ch, org, monkeypatch) -> None:
    """Eight writes of one key from eight sessions each retire the one before: every
    intent whose record and follow landed is resolved by one flush, none left for the
    repair, and resolved rows leave by the table's TTL."""
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, conflicts, intent_marks
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictResult

    async def _clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    monkeypatch.setattr(conflicts, "check_on_commit", _clear)
    now = datetime.now(UTC)
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": NS,
                "created_at": now,
                "updated_at": now,
                "active": True,
                "default_conflict_policy": "newest_wins",
            }
        ],
    )
    for i in range(8):
        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id=f"s{i}",
                memory_key="home_city",
                value=f"The user lives in city number {i}",
            )
        )
    await after_write.drain()
    await intent_marks.drain()

    async def by_status() -> dict[str, int]:
        rows = await ch.query_all(
            "SELECT toString(status) AS status, count() AS n FROM loom.memory_projection_failures"
            " WHERE org = {org:String} AND projection = 'triples_follow_owed'"
            " GROUP BY status SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        return {r["status"]: int(r["n"]) for r in rows}

    for _ in range(50):
        counts = await by_status()
        if counts and "active" not in counts:
            break
        await asyncio.sleep(0.2)
    assert counts == {"resolved": 7}, counts
    (table,) = await ch.query_all(
        "SELECT create_table_query AS q FROM system.tables"
        " WHERE database = 'loom' AND name = 'memory_projection_failures'"
    )
    # Only a resolved row expires: an abandoned one stays for an operator.
    assert "TTL coalesce(resolved_at, created_at) + toIntervalDay(30)" in table["q"]
    assert "WHERE status = 'resolved'" in table["q"]
