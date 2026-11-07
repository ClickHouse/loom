import dataclasses
import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from server.memory.app.projection_failures import (
    ProjectionFailureRow,
    _session_from_source_resource_id,
    retry_failures,
)


@pytest.fixture(autouse=True)
def _no_erasure(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.claims_fake import no_erasure

    no_erasure(monkeypatch)


def test_session_from_source_resource_id_parses_message_ingest_id() -> None:
    assert _session_from_source_resource_id("memory.set_from_messages:s-1:r-1") == "s-1"
    assert _session_from_source_resource_id("other") == "-"


async def test_a_retry_reports_an_unsupported_projection() -> None:
    (result,) = await retry_failures(
        [
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
            )
        ],
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
        return [{"id": mid, "sha": sha} for mid, sha in versions] if self.held else []

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
    (first,) = await retry_failures([_graph_failure(projection)], ch=ch)
    (second,) = await retry_failures([_graph_failure(projection)], ch=ch)
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
    (result,) = await retry_failures([failure], ch=ch)
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
    (result,) = await retry_failures([failure], ch=ch)
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

    async def write_entity_triples(
        input, results, *, ch, dedup_token, arrived_at, inserted_at, repaired, entity_map
    ):
        seen.append(request_scope())

    monkeypatch.setattr(projections, "write_entity_triples", write_entity_triples)
    failure = _graph_failure("entity_triples")
    scope = {"scope": "own_user", "user_id": "u1", "session_id": None}
    failure.payload[READ_SCOPE_KEY] = scope
    assert (await retry_failures([failure], ch=_GraphCH()))[0].resolved
    # Recorded before the scope rode the payload: the whole namespace, as then.
    del failure.payload[READ_SCOPE_KEY]
    assert (await retry_failures([failure], ch=_GraphCH()))[0].resolved
    assert seen == [ReadFilters(**scope), WHOLE_NAMESPACE]
    # A scope refused or incomplete is never replayed wider, nor retried again.
    for stored, reason in (
        ({"scope": "own_user", "user_id": None}, "incomplete_read_scope"),
        ({"scope": "refused"}, "refused_read_scope"),
    ):
        failure.payload[READ_SCOPE_KEY] = stored
        ch = _GraphCH()
        (result,) = await retry_failures([failure], ch=ch)
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

    async def write_entity_triples(
        input, results, *, ch, dedup_token, arrived_at, inserted_at, repaired, entity_map
    ):
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
        assert (await retry_failures([failure], ch=_GraphCH()))[0].resolved
    finally:
        set_current_auth(None)
        bind_request_read_filters(None, WHOLE_NAMESPACE)
    assert seen == [ReadFilters(**scope)]


async def test_only_a_triples_replay_reads_the_stored_scope() -> None:
    # Vectors and edges retire nothing: a refused scope does not hold them back.
    from server.identity.domain.read_scope import READ_SCOPE_KEY

    failure = _graph_failure("entity_edges")
    failure.payload[READ_SCOPE_KEY] = {"scope": "refused"}
    assert (await retry_failures([failure], ch=_GraphCH()))[0].resolved


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
    assert (await retry_failures([failure], ch=ch))[0].resolved
    assert not [t for t, _ in ch.inserts if t.startswith("loom.entity_triple")]
    # Dated at the write, not at the replay.
    assert repaired == [("alice", "bob", arrived.replace(microsecond=0), arrived, arrived)]


class _OpenCH(_GraphCH):
    """Every triple asked about is open."""

    async def query_all(self, sql, params=None):
        triples = (params or {}).get("triples") or []
        return [{"subject": s, "relation": r, "object": o} for s, r, o in triples]


async def test_a_repair_replay_resolves_names_as_the_write_did(monkeypatch) -> None:
    # The write resolved `bob` to itself (resolution failed open) and inserted
    # (alice, works_with, bob). If the replay resolved again and got `robert`, it
    # would look for a triple that was never written and repair nothing.
    import json

    from server.memory.app import projections
    from server.memory.app.projection_errors import TriplesRepairFailed, bulk_projection_payload

    answers = [{}, {"bob": "robert"}]

    async def resolver(input, texts, *, ch):
        return answers.pop(0) if answers else {"bob": "robert"}

    async def failing_repair(input, triples, *, ch, at):
        raise RuntimeError("repair read timed out")

    monkeypatch.setattr(projections, "resolve_projection_entity_map", resolver)
    monkeypatch.setattr(projections, "_repair_contradictory_triples", failing_repair)
    failure = _graph_failure("entity_triples")
    write = projections.MemoryBulkSetInput(
        org="o", namespace="n", agent="a", entries=failure.payload["entries"]
    )
    results = [projections._BulkSetResult(**r) for r in failure.payload["results"]]
    with pytest.raises(TriplesRepairFailed) as raised:
        await projections.write_entity_triples(write, results, ch=_GraphCH())
    failure.payload.update(
        json.loads(json.dumps(bulk_projection_payload(write, results, error=raised.value)))
    )

    repaired: list = []

    async def repair(input, triples, *, ch, at):
        repaired.extend((t["subject"], t["object"]) for t in triples)
        return []

    monkeypatch.setattr(projections, "_repair_contradictory_triples", repair)
    assert (await retry_failures([failure], ch=_OpenCH()))[0].resolved
    assert repaired == [("alice", "bob")]


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
    assert (await retry_failures([failure], ch=_GraphCH()))[0].resolved
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
    assert not (await retry_failures([failure], ch=ch))[0].resolved
    assert not any("status = 'resolved'" in sql for sql in ch.executed)

    async def no_close(input, triples, *, ch, at):
        raise AssertionError("the recorded closes are finished, not derived again")

    async def retire_ok(input, repairs, triple_memories, *, ch):
        retired.extend(repairs)

    monkeypatch.setattr(projections, "_repair_contradictory_triples", no_close)
    monkeypatch.setattr(projections, "_retire_memories_behind_repairs", retire_ok)
    assert (await retry_failures([failure], ch=ch))[0].resolved
    assert retired == [made]


async def test_a_failed_replay_keeps_only_the_entries_it_wrote(monkeypatch) -> None:
    # k2's row was revised since, so the replay writes only k1, then its repair fails:
    # the failure then keeps only k1, so the next replay finishes k1's repair and never
    # acts on k2.
    from datetime import datetime

    from server.memory.app import graph_holders, projections
    from server.memory.app.projection_errors import TriplesRepairFailed
    from server.memory.domain.graph_quality import IntervalRepair

    failure = _graph_failure("entity_triples")
    other = "33333333-3333-3333-3333-333333333333"
    failure.payload["entries"].append({**failure.payload["entries"][0], "memory_key": "k2"})
    failure.payload["results"].append({"memory_key": "k2", "version": 1, "memory_id": other})

    async def holding(org, namespace, pairs, *, ch, holder):
        return {(mid, sha) for mid, sha in pairs if mid == _ROW_ID}

    made = IntervalRepair("alice", "works_with", "carol", "bob", datetime(2026, 10, 1, 9))

    async def fails(*a, **k):
        raise TriplesRepairFailed(datetime(2026, 10, 1, 9), RuntimeError("retire down"), [made])

    monkeypatch.setattr(graph_holders, "versions_held", holding)
    monkeypatch.setattr(projections, "write_entity_triples", fails)
    assert not (await retry_failures([failure], ch=_FailureRowCH(failure)))[0].resolved
    payload = failure.payload
    assert [e["memory_key"] for e in payload["triples_landed_entries"]] == ["k1"]
    assert [r["memory_key"] for r in payload["triples_landed_results"]] == ["k1"]
    assert payload["triples_inserted_at"] == "2026-10-01T09:00:00"


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
    assert not (await retry_failures([failure], ch=ch))[0].resolved
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


class _InsertFailsCH(_GraphCH):
    """The triples insert fails; everything else succeeds."""

    async def insert(self, table, rows, settings=None):
        if table == "loom.entity_triples":
            raise RuntimeError("insert timed out")
        await super().insert(table, rows, settings)


async def test_a_replayed_triples_insert_is_dated_at_the_write(monkeypatch) -> None:
    # W1 states london and its insert fails; W2 states paris later. A replay dated at
    # its own clock would key london after paris and close paris. It inserts the
    # triples as seen when W1 arrived, and repairs them as of then.
    import json
    from datetime import datetime

    from server.memory.app import projections
    from server.memory.app.projection_errors import bulk_projection_payload

    arrived = datetime(2026, 10, 15, 9, 37, 51, 910300)
    monkeypatch.setattr(projections, "naive_utc_now", lambda: arrived)
    failure = _graph_failure("entity_triples")
    write = projections.MemoryBulkSetInput(
        org="o", namespace="n", agent="a", entries=failure.payload["entries"]
    )
    results = [projections._BulkSetResult(**r) for r in failure.payload["results"]]
    with pytest.raises(Exception) as raised:
        await projections.write_entity_triples(write, results, ch=_InsertFailsCH(), entity_map={})
    payload = json.loads(json.dumps(bulk_projection_payload(write, results, error=raised.value)))
    assert payload["triples_arrived_at"] == arrived.isoformat()
    # Nothing landed: the replay still inserts.
    assert "triples_inserted_at" not in payload

    inserted: list = []
    repaired: list = []

    class _Recording(_GraphCH):
        async def insert(self, table, rows, settings=None):
            if table == "loom.entity_triples":
                inserted.extend((r["object"], r["seen_at"], r["last_seen"]) for r in rows)
            await super().insert(table, rows, settings)

    async def repair(input, triples, *, ch, at):
        repaired.append(at)
        return []

    monkeypatch.setattr(projections, "_repair_contradictory_triples", repair)
    monkeypatch.setattr(projections, "naive_utc_now", lambda: datetime(2026, 10, 16))
    failure.payload.update(payload)
    assert (await retry_failures([failure], ch=_Recording()))[0].resolved
    assert inserted == [("bob", arrived, arrived.replace(microsecond=0))]
    assert repaired == [arrived]


class _KeyLinkFailsCH(_GraphCH):
    """The triples land; the key-link insert beside them fails."""

    async def insert(self, table, rows, settings=None):
        if table == "loom.entity_triple_memories":
            raise RuntimeError("insert timed out")
        await super().insert(table, rows, settings)


@pytest.mark.parametrize("resolved", [{}, {"bob": "robert"}])
async def test_a_failed_triples_insert_replays_the_names_the_write_resolved(
    monkeypatch, resolved
) -> None:
    # The triples may have landed: the replay names them as the write did, even when
    # the write resolved no name ({}), not as names resolve by the replay.
    import json

    from server.memory.app import projections
    from server.memory.app.projection_errors import bulk_projection_payload

    def resolve_as(names):
        async def resolve(input, entity_texts, *, ch):
            return names

        return resolve

    monkeypatch.setattr(projections, "resolve_projection_entity_map", resolve_as(resolved))
    failure = _graph_failure("entity_triples")
    write = projections.MemoryBulkSetInput(
        org="o", namespace="n", agent="a", entries=failure.payload["entries"]
    )
    results = [projections._BulkSetResult(**r) for r in failure.payload["results"]]
    with pytest.raises(Exception) as raised:
        await projections.write_entity_triples(write, results, ch=_KeyLinkFailsCH())
    payload = json.loads(json.dumps(bulk_projection_payload(write, results, error=raised.value)))
    assert payload["triples_entity_map"] == resolved

    objects: list = []

    class _Recording(_GraphCH):
        async def insert(self, table, rows, settings=None):
            if table == "loom.entity_triples":
                objects.extend(r["object"] for r in rows)
            await super().insert(table, rows, settings)

    async def repair(input, triples, *, ch, at):
        return []

    monkeypatch.setattr(projections, "_repair_contradictory_triples", repair)
    monkeypatch.setattr(projections, "resolve_projection_entity_map", resolve_as({"bob": "bobby"}))
    failure.payload.update(payload)
    assert (await retry_failures([failure], ch=_Recording()))[0].resolved
    assert objects == [resolved.get("bob", "bob")]


async def test_an_insert_failure_recorded_without_its_arrival_replays_as_it_did(
    monkeypatch,
) -> None:
    # Recorded before failures carried the arrival: the replay dates it at its own clock.
    from datetime import datetime

    from server.memory.app import projections

    now = datetime(2026, 10, 16, 8, 0, 0, 5)
    monkeypatch.setattr(projections, "naive_utc_now", lambda: now)
    repaired: list = []

    async def repair(input, triples, *, ch, at):
        repaired.append(at)
        return []

    monkeypatch.setattr(projections, "_repair_contradictory_triples", repair)
    (result,) = await retry_failures([_graph_failure("entity_triples")], ch=_GraphCH())
    assert result.resolved
    assert repaired == [now]


async def test_a_follow_replay_first_finishes_a_fallbacks_recorded_repair(monkeypatch) -> None:
    # The follow's fallback closed Paris and failed to close its edge: the replay
    # finishes that repair from the recorded closes before following the pairs again.
    from server.memory.app import graph_undo, triple_follow
    from server.memory.app.projection_errors import TriplesRepairFailed, restated_repair_payload
    from server.memory.domain.graph_quality import IntervalRepair

    at = datetime(2026, 10, 16, 12, 0, 0)
    paris = IntervalRepair("user", "live_in", "paris", "berlin", valid_to=datetime(2026, 12, 1))
    failed = TriplesRepairFailed(at, ConnectionError("edge close"), [paris], restore_id="r1")
    calls: list[tuple] = []

    async def finish(input, restore_id, *, restored_at, repaired, ch):
        calls.append(("finish", restore_id, restored_at, repaired))

    async def follow(input, pairs, *, ch, hops):
        calls.append(("follow", pairs))
        return triple_follow.FollowOutcome(0, 0, [])

    monkeypatch.setattr(graph_undo, "finish_restated_repair", finish)
    monkeypatch.setattr(triple_follow, "follow_row_supersessions", follow)
    payload = json.loads(json.dumps({"pairs": [["a", "b"]], **restated_repair_payload(failed)}))
    failure = ProjectionFailureRow(
        failure_id="f1",
        org="o",
        namespace="n",
        agent="a",
        projection="triples_follow_rows",
        source_operation="memory.bulk_set",
        source_resource_id="",
        memory_keys=[],
        error_type="RuntimeError",
        error_message="boom",
        payload=payload,
    )
    (result,) = await retry_failures([failure], ch=_GraphCH())
    assert result.resolved
    assert calls == [("finish", "r1", at, [paris]), ("follow", [("a", "b")])]


async def test_a_follow_replay_whose_fallback_fails_records_its_repair(monkeypatch) -> None:
    # The follow's failure was recorded with no markers; its replay's fallback states
    # Paris again and fails to close the edge. The next replay must finish that repair.
    from server.memory.app import triple_follow
    from server.memory.app.projection_errors import TRIPLES_RESTORE_ID, TriplesRepairFailed
    from server.memory.app.projection_failures import _recorded_repairs
    from server.memory.domain.graph_quality import IntervalRepair

    at = datetime(2026, 10, 16, 12, 0, 0)
    paris = IntervalRepair("user", "live_in", "paris", "berlin", valid_to=datetime(2026, 12, 1))

    async def follow(input, pairs, *, ch, hops):
        raise TriplesRepairFailed(at, ConnectionError("edge close"), [paris], restore_id="r1")

    monkeypatch.setattr(triple_follow, "follow_row_supersessions", follow)
    failure = ProjectionFailureRow(
        failure_id="f1",
        org="o",
        namespace="n",
        agent="a",
        projection="triples_follow_rows",
        source_operation="memory.bulk_set",
        source_resource_id="",
        memory_keys=[],
        error_type="RuntimeError",
        error_message="boom",
        payload={"pairs": [["a", "b"]]},
    )
    assert not (await retry_failures([failure], ch=_FailureRowCH(failure)))[0].resolved
    assert failure.payload[TRIPLES_RESTORE_ID] == "r1"
    assert _recorded_repairs(failure.payload) == [paris]


async def test_a_replay_whose_finish_of_a_recorded_repair_fails_records_its_closes(
    monkeypatch,
) -> None:
    # Recorded with the markers' id and no closes. The replay's finish closes Paris and
    # fails to close its edge: the next replay must find Paris in the payload, since
    # Paris's triple is no longer open for a fresh repair to find.
    from server.memory.app import graph_undo
    from server.memory.app.projection_errors import (
        TRIPLES_INSERTED_AT,
        TRIPLES_REPAIRS,
        TRIPLES_RESTORE_ID,
        TriplesRepairFailed,
    )
    from server.memory.app.projection_failures import _recorded_repairs
    from server.memory.domain.graph_quality import IntervalRepair

    at = datetime(2026, 10, 16, 12, 0, 0)
    paris = IntervalRepair("user", "live_in", "paris", "berlin", valid_to=datetime(2026, 12, 1))

    async def finish(input, triples, *, at, repaired, ch):
        raise TriplesRepairFailed(at, ConnectionError("edge close"), [paris])

    monkeypatch.setattr(graph_undo, "finish_interval_repair", finish)
    failure = ProjectionFailureRow(
        failure_id="f1",
        org="o",
        namespace="n",
        agent="a",
        projection="triples_follow_rows",
        source_operation="memory.bulk_set",
        source_resource_id="",
        memory_keys=[],
        error_type="RuntimeError",
        error_message="boom",
        payload={
            "pairs": [["a", "b"]],
            TRIPLES_RESTORE_ID: "r1",
            TRIPLES_INSERTED_AT: at.isoformat(),
            TRIPLES_REPAIRS: [],
        },
    )
    assert not (await retry_failures([failure], ch=_FailureRowCH(failure)))[0].resolved
    assert failure.payload[TRIPLES_RESTORE_ID] == "r1"
    assert _recorded_repairs(failure.payload) == [paris]


async def test_a_failure_row_is_waited_for_not_left_in_a_buffer() -> None:
    """The ledger row is the only record of a lost projection: the insert
    waits for the async-insert flush."""
    from server.core.ch_writes import async_insert_settings
    from server.memory.app.projection_failures import record_projection_failure

    seen: list[tuple[str, dict]] = []

    class _CH:
        async def insert(self, table, rows, settings=None):
            seen.append((table, settings or {}))

    await record_projection_failure(
        org="o", namespace="n", projection="entity_edges", error="boom", ch=_CH()
    )
    assert seen == [("loom.memory_projection_failures", async_insert_settings())]


@pytest.mark.parametrize(
    ("payload_end", "ended_at"),
    [("2026-05-16T12:00:00", datetime(2026, 5, 16, 12, 0, 0)), (None, None)],
    ids=["recorded", "recorded-before-it-was-known"],
)
async def test_a_late_edges_cleanup_replays_with_the_revisions_end(
    monkeypatch, payload_end, ended_at
) -> None:
    # The replay ends the edges when the revision ended the value, as the cleanup it
    # repeats would have; without a recorded end, `close_late_edges` reads it again.
    from server.memory.app import edge_repair

    calls: list[tuple] = []

    async def close_late_edges(org, namespace, memory_id, sha, *, ended_at, entities, ch):
        assert entities == [], "recorded with no entities"
        calls.append((org, namespace, memory_id, sha, ended_at))

    monkeypatch.setattr(edge_repair, "close_late_edges", close_late_edges)
    payload = {"memory_id": _ROW_ID, "content_sha256": "a" * 64}
    if payload_end:
        payload["ended_at"] = payload_end
    failure = dataclasses.replace(_graph_failure("entity_edges_close"), payload=payload)
    assert (await retry_failures([failure], ch=_GraphCH()))[0].resolved
    assert calls == [("o", "n", _ROW_ID, "a" * 64, ended_at)]


async def test_a_failed_late_edges_cleanup_records_when_the_value_ended(monkeypatch) -> None:
    # The end it read is recorded, so the repair ends the edges then (for memory.set,
    # memory.bulk_set and the by-id updates alike: they share this cleanup).
    from types import SimpleNamespace

    from server.memory.app import edge_repair, graph_holders

    ended = datetime(2026, 3, 1)
    recorded: list[dict] = []

    async def none_held(*a: Any, **k: Any) -> set[tuple[str, str]]:
        return set()

    async def value_ended_at(*a: Any, **k: Any) -> datetime:
        return ended

    async def end_fails(*a: Any, **k: Any) -> None:
        raise RuntimeError("boom")

    async def record(rows: list, **k: Any) -> None:
        recorded.extend(json.loads(r["payload"]) for r in rows)

    monkeypatch.setattr(graph_holders, "versions_held", none_held)
    monkeypatch.setattr(edge_repair, "value_ended_at", value_ended_at)
    monkeypatch.setattr(edge_repair, "_end_edges_of", end_fails)
    monkeypatch.setattr(edge_repair, "insert_projection_failures", record)
    scope = SimpleNamespace(org="o", namespace="n", agent="a")
    versions = [(_ROW_ID, "k", "a" * 64)]
    await edge_repair.end_overtaken_edges(scope, versions, op="memory.set", ch=None)
    payload = {"memory_id": _ROW_ID, "content_sha256": "a" * 64, "ended_at": ended.isoformat()}
    assert recorded == [payload]


class _ValuesCH(_GraphCH):
    """The value-less close's page reads over `shas`, each value linking one pair of its
    own: a page after the hash cursor."""

    def __init__(self, shas: list[str]) -> None:
        super().__init__()
        self.shas = shas

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "GROUP BY content_sha256" in sql:
            after, page = params["after"], params["page"]
            later = [s for s in self.shas if s > after][:page]
            return [{"sha": s, "pairs": [("alice", s)]} for s in later]
        return []


def _record_value_less_closes(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[list]]:
    """Every value asked and every end_edges call's ends; values that hold none stay
    in the fake's read set, so each pass reads them again."""
    from server.memory.app import edge_repair, projections

    asked: list[str] = []
    calls: list[list] = []

    async def value_ended(org: str, ns: str, memory_id: str, sha: str, pairs, **kw: Any):
        asked.append(sha)
        return datetime(2026, 1, 1) + timedelta(seconds=int(sha, 16))

    async def end_edges(ch: Any, **kw: Any) -> list[str]:
        calls.append(kw["ends"])
        return []

    monkeypatch.setattr(edge_repair, "_value_ended", value_ended)
    monkeypatch.setattr(projections, "end_edges", end_edges)
    return asked, calls


async def test_a_value_less_close_reads_every_page_of_values_once(monkeypatch) -> None:
    """A 'high' row with 250 values: the repair reads them in pages by hash, each value
    once, ends each page's pairs with one end_edges at their own ends, and resolves."""
    shas = [f"{i:064x}" for i in range(250)]
    asked, calls = _record_value_less_closes(monkeypatch)
    failure = dataclasses.replace(
        _graph_failure("entity_edges_close"), payload={"memory_id": _ROW_ID}
    )
    (out,) = await retry_failures([failure], ch=_ValuesCH(shas))
    assert out.resolved and asked == shas
    assert [len(ends) for ends in calls] == [100, 100, 50]
    assert calls[0][0] == ("alice", shas[0], datetime(2026, 1, 1))


async def test_a_value_less_close_reads_at_most_twenty_pages_and_stays_active(monkeypatch) -> None:
    """More values than one repair reads (20 pages of 100): the failure stays active for
    the next pass instead of resolving with values unread."""
    shas = [f"{i:064x}" for i in range(2500)]
    asked, calls = _record_value_less_closes(monkeypatch)
    failure = dataclasses.replace(
        _graph_failure("entity_edges_close"), payload={"memory_id": _ROW_ID}
    )
    (out,) = await retry_failures([failure], ch=_ValuesCH(shas))
    assert not out.resolved and not out.abandoned and asked == shas[:2000]
    assert len(calls) == 20


async def test_a_value_less_page_ends_a_shared_pair_at_its_earliest_end(monkeypatch) -> None:
    """Two values of one page link the same pair: it ends once, at the earlier end; a
    value the row holds again ends nothing."""
    from server.memory.app import edge_repair, projections

    ended = {"a" * 64: datetime(2026, 3, 1), "b" * 64: datetime(2026, 2, 1), "c" * 64: None}
    calls: list[list] = []

    async def value_ended(org: str, ns: str, memory_id: str, sha: str, pairs, **kw: Any):
        return ended[sha]

    async def end_edges(ch: Any, **kw: Any) -> list[str]:
        calls.append(kw["ends"])
        return []

    monkeypatch.setattr(edge_repair, "_value_ended", value_ended)
    monkeypatch.setattr(projections, "end_edges", end_edges)
    values = [
        {"sha": "a" * 64, "pairs": [("alice", "bob")]},
        {"sha": "b" * 64, "pairs": [("alice", "bob"), ("alice", "carol")]},
        {"sha": "c" * 64, "pairs": [("alice", "dave")]},
    ]
    await edge_repair._close_page("o", "n", _ROW_ID, values, ch=None)
    assert calls == [
        [("alice", "bob", datetime(2026, 2, 1)), ("alice", "carol", datetime(2026, 2, 1))]
    ]


@pytest.mark.parametrize(
    "payload",
    [{"rows": {_ROW_ID: ["alice"]}}, {"versions": {}}, {"versions": {_ROW_ID: "1"}}],
    ids=["rows-only", "no-versions", "a-version-not-a-number"],
)
async def test_a_drop_payload_without_its_versions_is_malformed(payload) -> None:
    """A drop is dated by the owing write's version: a payload without one per row is
    abandoned, not replayed."""
    failure = dataclasses.replace(_graph_failure("entity_edges_drop_undeclared"), payload=payload)
    (out,) = await retry_failures([failure], ch=_GraphCH())
    assert out.abandoned and out.reason == "malformed_payload"


_OWED_KEY = ["o", "n", "a", "-", "-", "k1"]


@pytest.mark.parametrize(
    "payload",
    [
        {"memory_id": _ROW_ID, "version": 5},
        {"key": _OWED_KEY[:5], "memory_id": _ROW_ID, "version": 5},
        {"key": _OWED_KEY, "memory_id": _ROW_ID, "version": "5"},
        {"key": _OWED_KEY, "version": 5},
    ],
    ids=["no-key", "a-short-key", "a-version-not-a-number", "no-memory-id"],
)
async def test_an_owed_facts_payload_without_its_key_or_version_is_malformed(payload) -> None:
    """The sweep's owed close names the key, the row and the relabel's version: a payload
    missing one is abandoned, not replayed."""
    failure = dataclasses.replace(_graph_failure("sensitivity_facts_owed"), payload=payload)
    (out,) = await retry_failures([failure], ch=_GraphCH())
    assert out.abandoned and out.reason == "malformed_payload"


@pytest.mark.parametrize(
    ("age_s", "settled", "fails", "outcome"),
    [
        (3600, True, False, "resolved"),
        (3600, False, False, "pending"),
        (60, True, False, "pending"),
        (3600, True, True, "active"),  # a failed close: the debt stays for the next pass
    ],
)
async def test_owed_facts_are_closed_once_the_relabel_is_in_clickhouse(
    monkeypatch, age_s, settled, fails, outcome
) -> None:
    """The replay resolves the owed close once `close_owed_facts` settles it; until then,
    and while the record is younger than its grace (its relabel may still be written), it
    stays active as pending, with no error."""
    from datetime import timedelta

    from server.core.datetimes import naive_utc_now
    from server.memory.app import sensitivity_sweep

    calls: list[Any] = []

    async def _close(key, memory_id, version, *, ch):
        calls.append((key, memory_id, version))
        if fails:
            raise RuntimeError("triples unreachable")
        return settled

    monkeypatch.setattr(sensitivity_sweep, "close_owed_facts", _close)
    payload = {"key": _OWED_KEY, "memory_id": _ROW_ID, "version": 5}
    failure = dataclasses.replace(
        _graph_failure("sensitivity_facts_owed"),
        payload=payload,
        created_at=naive_utc_now() - timedelta(seconds=age_s),
    )
    (out,) = await retry_failures([failure], ch=_GraphCH())
    assert (out.resolved, out.reason == "pending", out.abandoned) == (
        outcome == "resolved",
        outcome == "pending",
        False,
    )
    assert bool(out.error) == (outcome == "active")
    assert calls == ([] if age_s < 600 else [(tuple(_OWED_KEY), _ROW_ID, 5)])


async def test_a_restate_the_sweep_owed_is_finished_by_the_reconcile_replay(monkeypatch) -> None:
    """The triples_reconcile the sweep records for a failed fallback restate carries what
    the replay finishes it with: the restore id, when the markers were stated, and the
    closes made."""
    from datetime import datetime

    from server.memory import store as memory_repo
    from server.memory.app import (
        edge_links,
        graph_undo,
        projection_failures,
        sensitivity_sweep,
        triple_projections,
    )
    from server.memory.app.projection_errors import TriplesRepairFailed
    from server.memory.domain.graph_quality import IntervalRepair

    key = ("o", "n", "a", "-", "-", "k1")
    repair = IntervalRepair(
        subject="alice",
        relation="lives_in",
        old_object="oslo",
        new_object="bergen",
        valid_to=datetime(2026, 1, 1, 12),
    )
    recorded: list[dict[str, Any]] = []
    finished: list[Any] = []
    closes: list[int] = []

    async def _versions(keys, **_k):
        return {key: 5}

    async def _quiet(*_a, **_k):
        return None

    async def _close(*_a, **_k):
        closes.append(1)
        if len(closes) == 1:  # the sweep's close; the replay's lands
            raise TriplesRepairFailed(
                datetime(2026, 1, 1), RuntimeError("timed out"), [repair], restore_id="r1"
            )

    async def _record(**kw):
        recorded.append(kw)
        return "f2"

    async def _finish(scope, restore_id, *, restored_at, repaired, ch):
        finished.append((restore_id, restored_at, repaired))

    monkeypatch.setattr(memory_repo, "clickhouse_head_versions", _versions)
    monkeypatch.setattr(edge_links, "drop_undeclared_links", _quiet)
    monkeypatch.setattr(triple_projections, "close_dropped_facts", _close)
    monkeypatch.setattr(projection_failures, "record_projection_failure", _record)
    monkeypatch.setattr(graph_undo, "finish_restated_repair", _finish)
    with pytest.raises(TriplesRepairFailed):
        await sensitivity_sweep.close_owed_facts(key, _ROW_ID, 5, ch=_GraphCH())
    (kw,) = recorded
    failure = dataclasses.replace(
        _graph_failure(kw["projection"]),
        payload=kw["payload"],
        source_operation="sensitivity_sweep",
    )
    (out,) = await retry_failures([failure], ch=_GraphCH())
    assert out.resolved and finished == [("r1", datetime(2026, 1, 1), [repair])]
    assert closes == [1, 1]  # the sweep's close, then the replay's after the repair


@pytest.mark.parametrize(
    "payload",
    [
        {"links": []},
        {"links": [["m", "sha", "a", "b"]], "close_ids": []},
        {"links": "m", "close_ids": []},
    ],
    ids=["no-close-ids", "a-link-without-its-deleted-flag", "links-not-a-list"],
)
async def test_a_restore_payload_without_its_links_is_malformed(payload) -> None:
    """A restore replays the links and closes it records: a payload missing them is
    abandoned, not replayed."""
    failure = dataclasses.replace(_graph_failure("entity_edges_restore"), payload=payload)
    (out,) = await retry_failures([failure], ch=_GraphCH())
    assert out.abandoned and out.reason == "malformed_payload"


def test_the_list_shows_the_error_kind_and_an_abandon_reason_never_the_text() -> None:
    """The stored message is the exception's text, which can carry a statement or the
    server's address: `ops.projection_failures.list` shows `error_type` and the reason
    a retry abandoned the row with."""
    from types import SimpleNamespace

    from server.insights.app import ops as ops_app

    text = "Code: 210. Connection refused (ch-internal-7:9440) INSERT INTO loom.memory_chunks"

    def row(message: str, status: str = "abandoned") -> Any:
        return SimpleNamespace(
            failure_id="f",
            org="o",
            namespace="n",
            agent="a",
            projection="memory_chunks",
            source_operation="memory.set",
            source_resource_id="k",
            memory_keys=["k"],
            error_type="RuntimeError",
            error_message=message,
            payload={},
            status=status,
            created_at=None,
            resolved_at=None,
        )

    assert ops_app._projection_failure_row(row(text, "active")).error_message == ""
    abandoned = ops_app._projection_failure_row(row(f"{text}; abandoned: model_calls_held"))
    assert abandoned.error_message == "abandoned: model_calls_held"
    assert abandoned.error_type == "RuntimeError"
    # The stored text can hold the separator itself: the reason is after the last one.
    echoed = f"{text} value '; abandoned: x' (ch-internal-7:9440); abandoned: model_calls_held"
    assert ops_app._projection_failure_row(row(echoed)).error_message == (
        "abandoned: model_calls_held"
    )
    # An active row's text is the exception's alone, whatever it holds.
    assert ops_app._projection_failure_row(row(echoed, "active")).error_message == ""
    # A reason Loom recorded as text (`error_type` "Error") is shown whole.
    own = row("conflict_resolver: retirement to follow", "active")
    own.error_type = "Error"
    assert ops_app._projection_failure_row(own).error_message == (
        "conflict_resolver: retirement to follow"
    )


async def test_a_retry_that_raises_reports_the_kind_only(monkeypatch) -> None:
    """`ops.projection_failures.retry` returns each result's `error`: the kind; the
    driver's text is logged."""
    from server.memory.app import projection_failures as pf

    async def _boom(failure: Any, *, ch: Any) -> None:
        raise RuntimeError("Code: 210. Connection refused (ch-internal-7:9440) INSERT INTO x")

    monkeypatch.setattr(pf, "_retry_memory_chunks", _boom)
    (result,) = await retry_failures(
        [
            ProjectionFailureRow(
                failure_id="f1",
                org="o",
                namespace="n",
                agent="a",
                projection="memory_chunks",
                source_operation="memory.set",
                source_resource_id="k",
                memory_keys=["k"],
                error_type="RuntimeError",
                error_message="boom",
            )
        ],
        ch=object(),
    )
    assert result.retried is True and result.resolved is False
    assert result.error == "RuntimeError"
