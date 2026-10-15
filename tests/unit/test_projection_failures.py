from typing import Any

import pytest

from server.memory.app.projection_failures import (
    ProjectionFailureRow,
    _session_from_source_resource_id,
    retry_projection_failure,
)


def test_session_from_source_resource_id_parses_message_ingest_id() -> None:
    assert _session_from_source_resource_id("memory.set_from_messages:s-1:r-1") == "s-1"
    assert _session_from_source_resource_id("other") == "-"


async def test_retry_projection_failure_reports_unsupported_projection() -> None:
    result = await retry_projection_failure(
        ProjectionFailureRow(
            failure_id="f1",
            org="o",
            namespace="n",
            agent="a",
            projection="memory_evidence",
            source_operation="memory.set_from_messages",
            source_resource_id="memory.set_from_messages:s:r",
            memory_keys=[],
            error_type="RuntimeError",
            error_message="boom",
        ),
        ch=object(),
    )

    assert result.retried is False
    assert result.resolved is False
    assert result.reason == "unsupported_projection"


class _GraphCH:
    """Records every insert's table and settings; every row asked about still holds
    its value (`held`), and the repair reads find nothing."""

    def __init__(self, held: bool = True) -> None:
        self.inserts: list[tuple[str, dict]] = []
        self.executed: list[str] = []
        self.held = held
        self.asked: list = []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, dict(settings or {})))

    async def query_all(self, sql, params=None):
        versions = (params or {}).get("versions") or []
        self.asked += versions
        return [{"id": mid} for mid, _sha in versions] if self.held else []

    async def query_one(self, sql, params=None):
        return None

    async def execute(self, sql, params=None):
        self.executed.append(sql)


def _graph_failure(projection: str) -> ProjectionFailureRow:
    entry = {
        "memory_key": "k1",
        "memory_type": "semantic",
        "value": "alice works with bob at acme",
        "entities": ["alice", "bob", "acme"],
        "relations": [["alice", "works_with", "bob"]],
    }
    return ProjectionFailureRow(
        failure_id="11111111-1111-1111-1111-111111111111",
        org="o",
        namespace="n",
        agent="a",
        projection=projection,
        source_operation="memory.bulk_set",
        source_resource_id="",
        memory_keys=["k1"],
        error_type="RuntimeError",
        error_message="boom",
        payload={
            "entries": [entry],
            "results": [{"memory_key": "k1", "version": 1, "memory_id": _ROW_ID}],
        },
    )


_ROW_ID = "22222222-2222-2222-2222-222222222222"


@pytest.mark.parametrize(
    ("projection", "tables"),
    [
        ("entity_edges", {"loom.entity_edges", "loom.entity_edge_memories"}),
        ("entity_triples", {"loom.entity_triples", "loom.entity_triple_memories"}),
    ],
)
async def test_a_graph_replay_inserts_under_the_failures_dedup_token(projection, tables) -> None:
    """The plain-MergeTree graph tables count every row as an observation.
    The resolve is an ALTER UPDATE that may apply after the next pass, so a
    replay can run twice; the second insert carries the same token per table
    and ClickHouse drops it instead of counting the co-occurrence again."""
    ch = _GraphCH()
    first = await retry_projection_failure(_graph_failure(projection), ch=ch)
    second = await retry_projection_failure(_graph_failure(projection), ch=ch)
    assert first.resolved and second.resolved, (first, second)
    seen = [(t, s) for t, s in ch.inserts if t in tables]
    assert {t for t, _ in seen} == tables and len(seen) == 4
    for table, s in seen:
        short = table.removeprefix("loom.")
        assert s == {
            "async_insert": 0,
            "insert_deduplicate": 1,
            "insert_deduplication_token": (
                f"projection-repair:11111111-1111-1111-1111-111111111111:{short}"
            ),
        }


@pytest.mark.parametrize("projection", ["entity_vectors", "entity_edges", "entity_triples"])
async def test_a_replay_writes_nothing_for_a_row_that_no_longer_holds_its_value(
    projection, monkeypatch
) -> None:
    # Revised, retired or made 'high' since the failure: the row keeps what it holds.
    from server.core.hashing import sha256_value

    monkeypatch.setattr("server.embedding.get_embedder", lambda: object())
    ch = _GraphCH(held=False)
    failure = _graph_failure(projection)
    result = await retry_projection_failure(failure, ch=ch)
    assert result.resolved and not result.abandoned
    assert not [t for t, _ in ch.inserts if t.startswith("loom.entity")]
    # Asked by the row and the version its entry wrote, as its triple links are keyed.
    assert ch.asked == [(_ROW_ID, sha256_value(failure.payload["entries"][0]["value"]))]


@pytest.mark.parametrize(
    ("projection", "table"),
    [("entity_edges", "loom.entity_edges"), ("entity_triples", "loom.entity_triples")],
)
async def test_a_failure_recorded_before_results_named_their_row_replays_as_it_did(
    projection, table
) -> None:
    # Queued before results carried a row id: nothing to check it against, so the
    # replay writes what it wrote then instead of resolving with nothing done.
    ch = _GraphCH(held=False)
    failure = _graph_failure(projection)
    failure.payload["results"] = [{"memory_key": "k1", "version": 1}]
    result = await retry_projection_failure(failure, ch=ch)
    assert result.resolved and not result.abandoned
    assert table in {t for t, _ in ch.inserts}


def test_the_write_path_keeps_its_async_inserts() -> None:
    from server.memory.app.projections import graph_insert_settings

    assert graph_insert_settings("entity_edges", wait=False, dedup_token="") == {
        "async_insert": 1,
        "wait_for_async_insert": 0,
    }


async def test_a_triples_replay_retires_only_within_the_writers_scope(monkeypatch) -> None:
    # A replay runs outside the request: the scope it retires within is the one the
    # failed write recorded, not the worker's whole namespace.
    from server.identity.app.request_scope import request_scope
    from server.identity.domain.read_scope import READ_SCOPE_KEY, WHOLE_NAMESPACE, ReadFilters
    from server.memory.app import projections

    seen: list = []

    async def write_entity_triples(input, results, *, ch, dedup_token, inserted_at, repaired):
        seen.append(request_scope())

    monkeypatch.setattr(projections, "write_entity_triples", write_entity_triples)
    failure = _graph_failure("entity_triples")
    scope = {"scope": "own_user", "user_id": "u1", "session_id": None}
    failure.payload[READ_SCOPE_KEY] = scope
    assert (await retry_projection_failure(failure, ch=_GraphCH())).resolved
    # Recorded before the scope rode the payload: the whole namespace, as then.
    del failure.payload[READ_SCOPE_KEY]
    assert (await retry_projection_failure(failure, ch=_GraphCH())).resolved
    assert seen == [ReadFilters(**scope), WHOLE_NAMESPACE]
    # A scope refused or incomplete is never replayed wider, nor retried again.
    for stored, reason in (
        ({"scope": "own_user", "user_id": None}, "incomplete_read_scope"),
        ({"scope": "refused"}, "refused_read_scope"),
    ):
        failure.payload[READ_SCOPE_KEY] = stored
        ch = _GraphCH()
        result = await retry_projection_failure(failure, ch=ch)
        assert result.abandoned and result.reason == reason
        assert any("status = 'abandoned'" in sql for sql in ch.executed)
    assert len(seen) == 2


async def test_a_retry_an_admin_runs_replays_under_the_writers_scope(monkeypatch) -> None:
    # Run from the ops op, the admin's own namespace-wide scope is bound: the replay
    # still retires only within the scope the failed write recorded.
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.app.request_scope import request_scope
    from server.identity.domain.read_scope import (
        READ_SCOPE_KEY,
        WHOLE_NAMESPACE,
        ReadFilters,
        bind_request_read_filters,
    )
    from server.memory.app import projections

    seen: list = []

    async def write_entity_triples(input, results, *, ch, dedup_token, inserted_at, repaired):
        seen.append(request_scope())

    monkeypatch.setattr(projections, "write_entity_triples", write_entity_triples)
    failure = _graph_failure("entity_triples")
    scope = {"scope": "own_user", "user_id": "u1", "session_id": None}
    failure.payload[READ_SCOPE_KEY] = scope
    admin = AuthInfo(
        subject="admin", org="o", namespaces=("n",), roles=(), is_service_account=False
    )
    set_current_auth(admin)
    bind_request_read_filters(admin, WHOLE_NAMESPACE)
    try:
        assert (await retry_projection_failure(failure, ch=_GraphCH())).resolved
    finally:
        set_current_auth(None)
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    assert seen == [ReadFilters(**scope)]


async def test_only_a_triples_replay_reads_the_stored_scope() -> None:
    # Vectors and edges retire nothing: a refused scope does not hold them back.
    from server.identity.domain.read_scope import READ_SCOPE_KEY

    failure = _graph_failure("entity_edges")
    failure.payload[READ_SCOPE_KEY] = {"scope": "refused"}
    assert (await retry_projection_failure(failure, ch=_GraphCH())).resolved


def _payload_under(scope: Any) -> dict[str, Any]:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, bind_request_read_filters
    from server.memory.app.projection_errors import bulk_projection_payload

    auth = AuthInfo(subject="sa:bot", org="o", namespaces=("n",), roles=(), is_service_account=True)
    set_current_auth(auth)
    bind_request_read_filters(auth, scope)
    try:
        return bulk_projection_payload(object(), [])
    finally:
        set_current_auth(None)
        bind_request_read_filters(None, WHOLE_NAMESPACE)


def test_a_bulk_failure_payload_records_the_writers_scope() -> None:
    from server.identity.domain.read_scope import READ_SCOPE_KEY, ReadFilters

    payload = _payload_under(ReadFilters(scope="own_user", user_id="u1", session_id=None))
    assert payload[READ_SCOPE_KEY] == {"scope": "own_user", "user_id": "u1", "session_id": None}


def test_a_refused_scope_is_recorded_as_one_no_replay_runs_under() -> None:
    from server.core.errors import IdentityIncompleteError
    from server.identity.domain.read_scope import READ_SCOPE_KEY, replayed_read_scope

    payload = _payload_under(IdentityIncompleteError("read_scope=own_user requires user_id"))
    with pytest.raises(ValueError, match="was refused"):
        replayed_read_scope(payload[READ_SCOPE_KEY])


async def test_a_failed_interval_repair_replays_only_the_repair(monkeypatch) -> None:
    # The triples landed before the repair failed, and their insert carried no dedup
    # token: a replay that inserted them again would count each one twice and leave a
    # second open row. It repairs the triples the write inserted, as of that write.
    from server.memory.app import projections
    from server.memory.app.projection_errors import TriplesRepairFailed, bulk_projection_payload

    async def failing_repair(input, triples, *, ch, at):
        raise RuntimeError("repair read timed out")

    monkeypatch.setattr(projections, "_repair_contradictory_triples", failing_repair)
    from datetime import datetime

    arrived = datetime(2026, 10, 15, 9, 37, 51, 910300)
    monkeypatch.setattr(projections, "naive_utc_now", lambda: arrived)
    failure = _graph_failure("entity_triples")
    write = projections.MemoryBulkSetInput(
        org="o", namespace="n", agent="a", entries=failure.payload["entries"]
    )
    results = [projections._BulkSetResult(**r) for r in failure.payload["results"]]
    with pytest.raises(TriplesRepairFailed) as raised:
        await projections.write_entity_triples(write, results, ch=_GraphCH(), entity_map={})
    inserted_at = raised.value.inserted_at
    # To the microsecond, as the rows' `seen_at` is: the replay reuses that instant.
    assert inserted_at == arrived
    payload = bulk_projection_payload(write, results, error=raised.value)
    assert payload["triples_inserted_at"] == inserted_at.isoformat()

    repaired: list = []

    async def repair(input, triples, *, ch, at):
        repaired.extend(
            (t["subject"], t["object"], t["last_seen"], t["seen_at"], at) for t in triples
        )
        return []

    monkeypatch.setattr(projections, "_repair_contradictory_triples", repair)
    failure.payload.update(payload)
    ch = _OpenCH()
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    assert not [t for t, _ in ch.inserts if t.startswith("loom.entity_triple")]
    # Dated at the write, not at the replay.
    assert repaired == [("alice", "bob", arrived.replace(microsecond=0), arrived, arrived)]


class _OpenCH(_GraphCH):
    """Every triple asked about is open."""

    async def query_all(self, sql, params=None):
        triples = (params or {}).get("triples") or []
        return [{"subject": s, "relation": r, "object": o} for s, r, o in triples]


async def test_a_replay_finishes_the_closes_the_failed_repair_made(monkeypatch) -> None:
    # The close landed and the retirement failed: the replay re-derives nothing (the
    # closed rows are no longer open) and retires behind the closes the write recorded.
    import json
    from datetime import datetime

    from server.memory.app import projections
    from server.memory.app.projection_errors import TriplesRepairFailed, bulk_projection_payload
    from server.memory.domain.graph_quality import IntervalRepair

    made = IntervalRepair("alice", "works_with", "carol", "bob", datetime(2026, 10, 1, 9))

    async def close(input, triples, *, ch, at):
        return [made]

    async def edges(input, repairs, *, ch):
        closed.extend(repairs)

    async def retire(input, repairs, triple_memories, *, ch):
        raise RuntimeError("retire down")

    closed: list = []
    monkeypatch.setattr(projections, "_repair_contradictory_triples", close)
    monkeypatch.setattr(projections, "_repair_contradictory_edges", edges)
    monkeypatch.setattr(projections, "_retire_memories_behind_repairs", retire)
    failure = _graph_failure("entity_triples")
    write = projections.MemoryBulkSetInput(
        org="o", namespace="n", agent="a", entries=failure.payload["entries"]
    )
    results = [projections._BulkSetResult(**r) for r in failure.payload["results"]]
    with pytest.raises(TriplesRepairFailed) as raised:
        await projections.write_entity_triples(write, results, ch=_GraphCH(), entity_map={})
    failure.payload.update(
        json.loads(json.dumps(bulk_projection_payload(write, results, error=raised.value)))
    )

    retired: list = []

    async def no_close(input, triples, *, ch, at):
        raise AssertionError("a replay of recorded closes derives none")

    async def retire_ok(input, repairs, triple_memories, *, ch):
        retired.extend(repairs)

    monkeypatch.setattr(projections, "_repair_contradictory_triples", no_close)
    monkeypatch.setattr(projections, "_retire_memories_behind_repairs", retire_ok)
    assert (await retry_projection_failure(failure, ch=_GraphCH())).resolved
    assert closed == [made, made] and retired == [made]


class _FailureRowCH(_GraphCH):
    """Applies a payload update to the failure row, as the table would."""

    def __init__(self, failure: ProjectionFailureRow) -> None:
        super().__init__()
        self.failure = failure

    async def execute(self, sql, params=None):
        await super().execute(sql, params)
        if "UPDATE payload" in sql:
            import json

            self.failure.payload.clear()
            self.failure.payload.update(json.loads(params["payload"]))


async def test_the_closes_a_failed_replay_made_are_finished_by_the_next(monkeypatch) -> None:
    # The write's repair failed before any close. Its replay closes Carol, then fails to
    # retire: the next replay finds Carol closed, and must still finish behind her.
    from datetime import datetime

    from server.memory.app import projections
    from server.memory.domain.graph_quality import IntervalRepair

    made = IntervalRepair("alice", "works_with", "carol", "bob", datetime(2026, 10, 1, 9))
    failure = _graph_failure("entity_triples")
    failure.payload["triples_inserted_at"] = "2026-10-01T09:00:00"
    retired: list = []

    async def close(input, triples, *, ch, at):
        return [made]

    async def edges(input, repairs, *, ch):
        pass

    async def retire(input, repairs, triple_memories, *, ch):
        raise RuntimeError("retire down")

    monkeypatch.setattr(projections, "_repair_contradictory_triples", close)
    monkeypatch.setattr(projections, "_repair_contradictory_edges", edges)
    monkeypatch.setattr(projections, "_retire_memories_behind_repairs", retire)
    ch = _FailureRowCH(failure)
    assert not (await retry_projection_failure(failure, ch=ch)).resolved
    assert not any("status = 'resolved'" in sql for sql in ch.executed)

    async def no_close(input, triples, *, ch, at):
        raise AssertionError("the recorded closes are finished, not derived again")

    async def retire_ok(input, repairs, triple_memories, *, ch):
        retired.extend(repairs)

    monkeypatch.setattr(projections, "_repair_contradictory_triples", no_close)
    monkeypatch.setattr(projections, "_retire_memories_behind_repairs", retire_ok)
    assert (await retry_projection_failure(failure, ch=ch)).resolved
    assert retired == [made]


async def test_a_failed_replay_keeps_only_the_entries_it_wrote(monkeypatch) -> None:
    # k2's row was revised since, so the replay writes only k1, then its repair fails:
    # the next replay finishes k1's repair, and never acts on k2.
    from datetime import datetime

    from server.memory.app import entity_projections, projections
    from server.memory.app.projection_errors import TriplesRepairFailed
    from server.memory.domain.graph_quality import IntervalRepair

    failure = _graph_failure("entity_triples")
    other = "33333333-3333-3333-3333-333333333333"
    failure.payload["entries"].append({**failure.payload["entries"][0], "memory_key": "k2"})
    failure.payload["results"].append({"memory_key": "k2", "version": 1, "memory_id": other})

    async def holding(org, namespace, pairs, *, ch):
        return {_ROW_ID}

    made = IntervalRepair("alice", "works_with", "carol", "bob", datetime(2026, 10, 1, 9))

    async def fails(*a, **k):
        raise TriplesRepairFailed(datetime(2026, 10, 1, 9), RuntimeError("retire down"), [made])

    monkeypatch.setattr(entity_projections, "rows_holding", holding)
    monkeypatch.setattr(projections, "write_entity_triples", fails)
    ch = _FailureRowCH(failure)
    assert not (await retry_projection_failure(failure, ch=ch)).resolved
    assert [e["memory_key"] for e in failure.payload["entries"]] == ["k1"]
    assert [r["memory_key"] for r in failure.payload["results"]] == ["k1"]
    assert failure.payload["triples_inserted_at"] == "2026-10-01T09:00:00"


async def test_a_failed_replay_that_closed_nothing_new_leaves_the_payload(monkeypatch) -> None:
    # Each payload update is a synchronous mutation: a replay that fails with the
    # closes the failure already records has nothing to write.
    import json
    from datetime import datetime

    from server.memory.app import projections
    from server.memory.app.projection_errors import TriplesRepairFailed, repair_failure_payload
    from server.memory.domain.graph_quality import IntervalRepair

    made = IntervalRepair("alice", "works_with", "carol", "bob", datetime(2026, 10, 1, 9))
    error = TriplesRepairFailed(datetime(2026, 10, 1, 9), RuntimeError("retire down"), [made])

    async def fails(*a, **k):
        raise error

    monkeypatch.setattr(projections, "_repair_triples", fails)
    failure = _graph_failure("entity_triples")
    failure.payload.update(json.loads(json.dumps(repair_failure_payload(error))))
    ch = _FailureRowCH(failure)
    assert not (await retry_projection_failure(failure, ch=ch)).resolved
    assert not any("UPDATE payload" in sql for sql in ch.executed)


def test_a_failed_repairs_closes_ride_the_payload_by_name() -> None:
    import json
    from datetime import datetime

    from server.memory.app.projection_errors import TriplesRepairFailed, repair_failure_payload
    from server.memory.app.projection_failures import _recorded_repairs
    from server.memory.domain.graph_quality import IntervalRepair

    made = IntervalRepair(
        "user", "live_in", "london", "paris", datetime(2026, 6, 1), invalid_at=datetime(2026, 10, 1)
    )
    payload = json.loads(
        json.dumps(
            repair_failure_payload(
                TriplesRepairFailed(datetime(2026, 10, 1), RuntimeError("down"), [made])
            )
        )
    )
    assert payload["triples_repairs"][0]["old_object"] == "london"
    assert payload["triples_repairs"][0]["valid_to"] == "2026-06-01T00:00:00"
    assert _recorded_repairs(payload) == [made]
