"""A triples repair that failed after its insert is recorded as `entity_triples_repair`.

The build before reads every `entity_triples` failure as an insert that never happened
and inserts the triples again under a new dedup token, which doubles their counts. It
does not know the new name, so it leaves those failures active for this build."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
from server.memory.app.projection_errors import (
    TRIPLES_REPAIR,
    TriplesInsertFailed,
    TriplesRepairFailed,
    failed_projection,
    repair_failure_payload,
)
from server.memory.app.projection_failures import ProjectionFailureRow, retry_projection_failure

_ROW_ID = "22222222-2222-2222-2222-222222222222"
_AT = datetime(2026, 10, 1, 9)


def test_a_repair_failure_has_its_own_name() -> None:
    assert TRIPLES_REPAIR == "entity_triples_repair"
    repair = TriplesRepairFailed(_AT, RuntimeError("down"))
    insert = TriplesInsertFailed(_AT, RuntimeError("down"))
    assert failed_projection("entity_triples", repair) == TRIPLES_REPAIR
    assert failed_projection("entity_triples", insert) == "entity_triples"
    assert failed_projection("entity_triples", RuntimeError("down")) == "entity_triples"
    assert failed_projection("entity_edges", repair) == "entity_edges"


def _failure(projection: str, **payload: Any) -> ProjectionFailureRow:
    entry = {
        "memory_key": "k1",
        "memory_type": "semantic",
        "value": "alice works with bob",
        "entities": ["alice", "bob"],
        "relations": [["alice", "works_with", "bob"]],
    }
    return ProjectionFailureRow(
        failure_id="11111111-1111-1111-1111-111111111111",
        org="o",
        namespace="n",
        agent="a",
        projection=projection,
        source_operation="memory.bulk_set",
        source_resource_id="memory.bulk_set:-:-",
        memory_keys=["k1"],
        error_type="TriplesRepairFailed",
        error_message="down",
        payload={
            "entries": [entry],
            "results": [{"memory_key": "k1", "version": 1, "memory_id": _ROW_ID}],
            **payload,
        },
    )


class _CH:
    """Records inserts and statements; every row still holds its value."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.executed: list[str] = []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, list(rows)))

    async def query_all(self, sql, params=None):
        versions = (params or {}).get("versions") or []
        return [{"id": mid, "sha": sha} for mid, sha in versions]

    async def query_one(self, sql, params=None):
        return None

    async def execute(self, sql, params=None):
        self.executed.append(sql)

    def triple_inserts(self) -> list[str]:
        return [t for t, _ in self.inserts if t.startswith("loom.entity_triple")]


def _inserted_payload() -> dict[str, Any]:
    error = TriplesRepairFailed(_AT, RuntimeError("down"))
    error.entity_map = {}
    return json.loads(json.dumps(repair_failure_payload(error)))


async def _no_repair(input, triples, *, ch, at):
    return []


async def test_a_repair_failure_replays_only_the_repair(monkeypatch) -> None:
    from server.memory.app import projections

    monkeypatch.setattr(projections, "_repair_contradictory_triples", _no_repair)
    ch = _CH()
    result = await retry_projection_failure(_failure(TRIPLES_REPAIR, **_inserted_payload()), ch=ch)
    assert result.resolved, result
    assert ch.triple_inserts() == []


async def test_a_legacy_triples_failure_that_landed_still_replays_only_the_repair(
    monkeypatch,
) -> None:
    # Recorded under `entity_triples` before the new name: its payload says it landed.
    from server.memory.app import projections

    monkeypatch.setattr(projections, "_repair_contradictory_triples", _no_repair)
    ch = _CH()
    result = await retry_projection_failure(
        _failure("entity_triples", **_inserted_payload()), ch=ch
    )
    assert result.resolved, result
    assert ch.triple_inserts() == []


async def test_a_replay_that_inserted_records_it_in_the_failure_itself(monkeypatch) -> None:
    # The replay of an insert failure inserts the triples, then its repair fails. One
    # mutation of the failure says they landed: no second row, nothing left to resolve.
    # The entries move out of `entries`, which the build before would insert again.
    from server.memory.app import projections
    from server.memory.app.projection_errors import LANDED_ENTRIES, LANDED_RESULTS

    async def failing_repair(input, triples, *, ch, at):
        raise RuntimeError("repair read timed out")

    class _PayloadCH(_CH):
        def __init__(self) -> None:
            super().__init__()
            self.payloads: list[dict[str, Any]] = []

        async def execute(self, sql, params=None):
            await super().execute(sql, params)
            if "UPDATE payload" in sql:
                self.payloads.append(json.loads(params["payload"]))

    monkeypatch.setattr(projections, "_repair_contradictory_triples", failing_repair)
    ch = _PayloadCH()
    failure = _failure("entity_triples")
    result = await retry_projection_failure(failure, ch=ch)
    assert not result.resolved and "TriplesRepairFailed" in result.error
    assert not [t for t, _ in ch.inserts if t == "loom.memory_projection_failures"]
    (payload,) = ch.payloads
    assert payload["triples_inserted_at"]
    assert "entries" not in payload and "results" not in payload
    assert [e["memory_key"] for e in payload[LANDED_ENTRIES]] == ["k1"]
    assert [r["memory_id"] for r in payload[LANDED_RESULTS]] == [_ROW_ID]

    # The next replay repairs only.
    monkeypatch.setattr(projections, "_repair_contradictory_triples", _no_repair)
    landed = _failure("entity_triples")
    landed.payload.clear()
    landed.payload.update(payload)
    ch = _CH()
    assert (await retry_projection_failure(landed, ch=ch)).resolved
    assert ch.triple_inserts() == []


async def test_a_point_write_records_a_failed_repair_under_the_new_name(monkeypatch) -> None:
    from server.memory.app import entity_projections as ep

    async def fails(write, results, *, ch):
        raise TriplesRepairFailed(_AT, RuntimeError("down"))

    recorded: list[str] = []

    async def record(input, *, projection, **k):
        recorded.append(projection)

    monkeypatch.setattr(ep, "write_entity_triples", fails)
    monkeypatch.setattr(ep, "record_input_projection_failure", record)
    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_id": _ROW_ID,
        "memory_key": "k1",
        "version": 1,
        "confidence": 1.0,
    }
    await ep._write_stated_relations(
        row, "alice works with bob", [("alice", "works_with", "bob")], op="memory.update", ch=None
    )
    assert recorded == [TRIPLES_REPAIR]


async def test_a_bulk_write_records_a_failed_repair_under_the_new_name(monkeypatch) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    async def fails(*a: Any, **k: Any) -> None:
        raise TriplesRepairFailed(_AT, RuntimeError("down"))

    for name in (
        "write_entity_vectors",
        "write_entity_edges",
        "_write_derived_facts",
        "_bulk_settle_verdicts",
        "_bulk_audit",
    ):
        monkeypatch.setattr(bulk_set, name, nothing)
    monkeypatch.setattr(bulk_set, "write_entity_triples", fails)
    recorded: list[str] = []

    async def record(input, *, projection, **k):
        recorded.append(projection)

    monkeypatch.setattr(bulk_set, "record_input_projection_failure", record)
    write = MemoryBulkSetInput(
        org="o", namespace="n", agent="a", entries=[{"memory_key": "k1", "value": "v"}]
    )
    state = _BulkWriteState()
    state.results = [_BulkSetResult(memory_key="k1", version=1, memory_id=_ROW_ID)]
    await bulk_set._bulk_run_projections(None, write, state, None, None, {}, {})
    assert recorded == [TRIPLES_REPAIR]
