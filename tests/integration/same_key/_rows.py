"""Rows, writes and reads the same-key and graph tests share."""

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
from tests.integration._resources import unique_org

NS = "same-key"


USER = "u1"


KEY = "user_ethnicity"


async def _retry_one(failure: Any, ch: Any) -> Any:
    """One failure retried and marked, as a repair pass does."""
    from server.memory.app.projection_failures import retry_failures

    (out,) = await retry_failures([failure], ch=ch)
    return out


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


async def _retire(ch: Any, org: str, value: str, valid_at: datetime, session: str) -> str:
    """Write `value` from `session` and retire every other live row of the key behind
    it, as an automatic retirement does; returns the new row's id."""
    from server.memory import store as M

    new = _row(org, session, value, valid_at)
    await M.insert_memory_versions([new], ch=ch)
    live = await ch.query_all(
        "SELECT toString(memory_id) AS memory_id, namespace, agent, user_id, session_id, "
        "memory_key FROM loom.memory FINAL WHERE org = {org:String} AND namespace = {ns:String} "
        "AND memory_key = {k:String} AND status = 'active' AND tombstoned_at IS NULL "
        "AND toString(memory_id) != {id:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org, "ns": NS, "k": KEY, "id": str(new["memory_id"])},
    )
    victims = [{**r, "superseded_by": str(new["memory_id"])} for r in live]
    await M.supersede_duplicates(org, victims, ch=ch, strong=True)
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
    from server.memory.app.triple_follow import follow_row_supersessions

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


async def _link_inferred(ch: Any, org: str, row: dict[str, Any], triple: tuple) -> None:
    """Link `row` to `triple` as a follow links a successor to its winner."""
    from server.memory.app.projections import triple_row_link

    link = triple_row_link(
        org, NS, str(row["memory_id"]), row["content_sha256"], triple, user_id=USER, inferred=True
    )
    await ch.insert("loom.entity_triple_rows", [link])


def _today() -> datetime:
    # The day a session dates its facts by.
    return datetime.now(UTC).replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)


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


def _past() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)


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


def _written_earlier() -> dict[str, datetime]:
    """A first write dated seconds ago, so what ends its value now is a later second (the
    DateTime columns store whole seconds) without waiting for one."""
    earlier = naive_utc_now().replace(microsecond=0) - timedelta(seconds=10)
    return {"updated_at": earlier, "created_at": earlier, "last_reinforced_at": earlier}


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


def _date_undos_at(monkeypatch: Any, clock: Any) -> None:
    """Date the next Undo by `clock`: the restore dates its facts by its version's time,
    and a direct reconcile reads the projections' clock."""
    from server.memory.app import edge_repair, graph_holders, restore, triple_projections

    monkeypatch.setattr(restore, "version_time", lambda version: clock())
    monkeypatch.setattr(restore, "naive_utc_now", clock)
    monkeypatch.setattr(graph_holders, "naive_utc_now", clock)
    monkeypatch.setattr(edge_repair, "naive_utc_now", clock)
    monkeypatch.setattr(triple_projections, "naive_utc_now", clock)


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


async def _restore_id(ch: Any, org: str) -> str:
    """The id of the org's one Undo, from the markers it wrote."""
    (row,) = await ch.query_all(
        "SELECT DISTINCT restore_id FROM loom.entity_triples WHERE org = {org:String}"
        " AND restore_id != '' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return str(row["restore_id"])


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


async def _restate_after_a_rewrite(org: str, monkeypatch: pytest.MonkeyPatch, **last: Any) -> Any:
    """alias merges into target's Berlin, target is rewritten, then alias says Berlin
    again with the extra fields `last`. Every alias row is judged a merge into target."""
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

    async def alias_merges(
        _input: Any, rows: list[dict[str, Any]], *a: Any, **_kw: Any
    ) -> dict[int, Any]:
        return {i: merge for i, r in enumerate(rows) if r["memory_key"] == "alias"}

    monkeypatch.setattr(bulk_set, "_bulk_conflict_verdicts", alias_merges)
    berlin = {"memory_key": "alias", "value": "Alice lives in Berlin"}
    return await _bulk_in(
        org,
        "s1",
        berlin,
        {"memory_key": "target", "value": "Alice owns a red bicycle."},
        {**berlin, **last},
    )


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


async def _row_saying(ch: Any, org: str, value: str) -> dict[str, Any]:
    (row,) = await ch.query_all(
        "SELECT toString(memory_id) AS memory_id FROM loom.memory FINAL"
        " WHERE org = {org:String} AND value = {value:String}",
        params={"org": org, "value": json.dumps(value)},
    )
    return row


async def _replay_follow(ch: Any, org: str, loser: dict[str, Any], winner: dict[str, Any]) -> None:
    """Replay the follow of `loser` retired behind `winner`, as the repair replays the
    pairs a failed follow recorded."""
    from server.memory.app.projection_failures import (
        FOLLOW_ROWS,
        ProjectionFailureRow,
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
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)


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


# A 40-character Japanese place name: three UTF-8 bytes a character.
_LONG_JAPANESE_NAME = "東京都千代田区丸の内一丁目" * 3 + "駅"


async def _owed(ch: Any, org: str, want: str) -> list[dict[str, Any]]:
    """The org's FOLLOW_OWED intents, polled until one has status `want` (marks are
    mutations, and the intent an async insert)."""
    rows: list[dict[str, Any]] = []
    for _ in range(50):
        rows = await ch.query_all(
            "SELECT toString(failure_id) AS id, toString(status) AS status, payload"
            " FROM loom.memory_projection_failures WHERE org = {org:String}"
            " AND projection = 'triples_follow_owed'"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        if any(r["status"] == want for r in rows):
            break
        await asyncio.sleep(0.2)
    return rows


async def _newest_wins_two_offices(
    ch: Any, org: str, monkeypatch: Any, *, stop: str = ""
) -> dict[str, str]:
    """Paris, then Rome under one key in a newest_wins namespace, each with its fact.
    `stop`: "record" skips the retirement's record, "follow" its follow (a process
    stopped there)."""
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, conflicts, key_supersession, set_commit
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictResult

    async def _clear(**kw: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    async def _stopped(**kw: Any) -> None:
        return None

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
    ids = {}
    for session, city in (("s1", "paris"), ("s2", "rome")):
        if session == "s2" and stop == "record":
            monkeypatch.setattr(key_supersession, "settle_retirements", _stopped)
        if session == "s2" and stop == "follow":
            monkeypatch.setattr(set_commit, "follow_beside_response", lambda **kw: None)
        out = await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id=session,
                memory_key="office_city",
                value=f"The office is in {city.title()}",
                relations=[("office", "located_in", city)],
            )
        )
        ids[session] = out.memory_id
        await after_write.drain()
    return ids


async def _replay_owed(ch: Any, org: str, monkeypatch: Any) -> None:
    from server.memory.app import projection_failures
    from server.memory.app.projection_failures import retry_projection_failures

    monkeypatch.setattr(projection_failures, "FOLLOW_OWED_GRACE_S", 0)
    await retry_projection_failures(org=org, projection="triples_follow_owed", ch=ch)


async def _records(ch: Any, org: str) -> list[dict[str, Any]]:
    return await ch.query_all(
        "SELECT request_id FROM loom.audit_log WHERE org = {org:String}"
        " AND event_type = 'memory.auto_supersede' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )


async def _open_edge_pairs(ch: Any, org: str) -> set[tuple[str, str]]:
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT entity_a, entity_b FROM loom.entity_edges WHERE org = {org:String}"
        " AND valid_to IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return {(str(r["entity_a"]), str(r["entity_b"])) for r in rows}


async def _high_set(org: str, session: str, key: str, value: str, **kw: Any) -> Any:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.write import handle_memory_set

    out = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id=session,
            memory_key=key,
            value=value,
            memory_type="semantic",
            **kw,
        )
    )
    await after_write.drain()
    return out


async def _edge_links(ch: Any, org: str) -> int:
    rows = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_edge_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return int(rows[0]["n"])


async def _drop_intents(ch: Any, org: str) -> list[str]:
    rows = await ch.query_all(
        "SELECT toString(status) AS s FROM loom.memory_projection_failures WHERE org = {org:String}"
        " AND projection = 'entity_edges_drop_undeclared' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    return sorted(r["s"] for r in rows)


async def _recorded_closes(monkeypatch: Any) -> list[dict[str, Any]]:
    """Capture the edge-close failures written while the test runs (and write them)."""
    from server.memory.app import edge_repair

    captured: list[dict[str, Any]] = []
    real_insert = edge_repair.insert_projection_failures

    async def capture(rows: list[dict[str, Any]], **kw: Any) -> None:
        captured.extend(rows)
        await real_insert(rows, **kw)

    monkeypatch.setattr(edge_repair, "insert_projection_failures", capture)
    return captured


async def _made_high_with_the_drop_lost(
    org: str, value: str, monkeypatch: Any, entities: list[str] | None = None
) -> None:
    """The row `contact` re-set high (declaring `entities`, Alice Smith and Bob Jones by
    default) with its drop failing: the drop is left to the repair."""
    from server.memory.app import edge_links

    async def down(*a: Any, **kw: Any) -> None:
        raise RuntimeError("DELETE refused")

    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", down)
        await _high_set(
            org,
            "s1",
            "contact",
            value,
            sensitivity="high",
            entities=entities or ["Alice Smith", "Bob Jones"],
        )


def _long_names(count: int, repeat: int) -> list[str]:
    """Entity names of a few hundred characters, under the 500 an entity may have."""
    return [f"Committee {i} " + "for the Coordination of Freight " * repeat for i in range(count)]
