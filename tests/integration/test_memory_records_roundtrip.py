"""memory.record.* end-to-end against real ClickHouse.

GATE: the schema-records loop must actually round-trip — register a schema,
write twice under one memory_key, and read back exactly ONE active row with
the newest values. The unit tests exercise the SQL builder and write-side
validation only; alias shadowing (a hydration alias over `record_id`, the
supersede INSERT..SELECT's `'superseded' AS status` over the
`status = 'active'` predicate) is invisible without a real CH round-trip, because a same-name alias silently empties the result
instead of erroring.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from server.memory.api.models_records import (  # noqa: E402
    MemoryRecordHistoryInput,
    MemoryRecordQueryInput,
    MemoryRecordWriteInput,
    MemorySchemaRegisterInput,
    RecordFieldInput,
    RecordPredicate,
    SchemaFieldInput,
)
from server.memory.app.records import (  # noqa: E402
    handle_memory_record_history,
    handle_memory_record_query,
    handle_memory_record_write,
    handle_memory_schema_register,
)
from tests.integration._resources import unique_org  # noqa: E402


@pytest.fixture(autouse=True)
async def _ch_client_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


def _write_input(org: str, ns: str, key: str, status: str, fix_pr: int):
    return MemoryRecordWriteInput(
        org=org,
        namespace=ns,
        schema_name="flaky_tests",
        memory_key=key,
        agent="records-roundtrip-test",
        fields={
            "test_name": RecordFieldInput(value=key),
            "status": RecordFieldInput(value=status),
            "fix_pr": RecordFieldInput(value=fix_pr),
        },
    )


async def _register_and_write_twice(org: str, ns: str, key: str) -> tuple[Any, Any]:
    """Register the flaky_tests schema and write `key` open, then fixed."""
    await handle_memory_schema_register(
        MemorySchemaRegisterInput(
            org=org,
            namespace=ns,
            name="flaky_tests",
            fields=[
                SchemaFieldInput(name="test_name", type="string", required=True),
                SchemaFieldInput(
                    name="status",
                    type="string",
                    required=True,
                    enum_values=["open", "fixed"],
                ),
                SchemaFieldInput(name="fix_pr", type="integer"),
            ],
            created_by="records-roundtrip-test",
        )
    )

    first = await handle_memory_record_write(_write_input(org, ns, key, "open", 0))
    second = await handle_memory_record_write(_write_input(org, ns, key, "fixed", 115898))
    return first, second


async def test_record_write_upserts_by_key_and_query_reads_back() -> None:
    org, ns, key = unique_org("records"), "records", "test_storage_flaky_probe"
    first, second = await _register_and_write_twice(org, ns, key)
    assert first.record_id != second.record_id

    # The logical key must resolve to exactly ONE active row — the newest.
    out = await handle_memory_record_query(
        MemoryRecordQueryInput(org=org, namespace=ns, schema_name="flaky_tests")
    )
    assert [r.memory_key for r in out.rows] == [key]
    assert out.rows[0].record_id == second.record_id
    assert out.rows[0].record["status"] == "fixed"
    assert out.rows[0].field_states.get("status") == "value"

    # Exact-string filter: newest value matches, superseded value must not.
    hit = await handle_memory_record_query(
        MemoryRecordQueryInput(
            org=org,
            namespace=ns,
            schema_name="flaky_tests",
            filters={"status": "fixed"},
        )
    )
    assert [r.record_id for r in hit.rows] == [second.record_id]
    stale = await handle_memory_record_query(
        MemoryRecordQueryInput(
            org=org,
            namespace=ns,
            schema_name="flaky_tests",
            filters={"status": "open"},
        )
    )
    assert stale.rows == []

    # Typed predicate over the numeric column.
    num = await handle_memory_record_query(
        MemoryRecordQueryInput(
            org=org,
            namespace=ns,
            schema_name="flaky_tests",
            predicates=[RecordPredicate(field="fix_pr", op="gte", number=115898)],
        )
    )
    assert [r.record_id for r in num.rows] == [second.record_id]

    # The supersede INSERT..SELECT landed: the first record's history holds a
    # row closed by the second write. (Whether the active row is still there
    # depends on merges; see the xfail below.)
    hist = await handle_memory_record_history(
        MemoryRecordHistoryInput(org=org, namespace=ns, record_id=first.record_id)
    )
    assert any(r.valid_to is not None for r in hist.rows), hist.rows


@pytest.mark.destructive
async def test_record_history_keeps_both_versions_after_a_merge(ch_client) -> None:
    org, ns, key = unique_org("records"), "records", "history_after_merge"
    first, _ = await _register_and_write_twice(org, ns, key)
    ch_client.command("OPTIMIZE TABLE loom.memory_records FINAL")
    hist = await handle_memory_record_history(
        MemoryRecordHistoryInput(org=org, namespace=ns, record_id=first.record_id)
    )
    assert len(hist.rows) >= 2, hist.rows


@pytest.mark.journal
async def test_a_query_at_k_matches_typed_values_and_states_as_today() -> None:
    # The probes that matched today and not at K: a boolean filter on "true", a
    # number accepted as "10", and an explicit rejected state.
    import asyncio
    from datetime import UTC, datetime

    org, ns = unique_org("rec-at-k"), "rec-at-k"
    await handle_memory_schema_register(
        MemorySchemaRegisterInput(
            org=org,
            namespace=ns,
            name="probe",
            fields=[
                SchemaFieldInput(name="active", type="boolean"),
                SchemaFieldInput(name="count", type="number"),
                SchemaFieldInput(name="source", type="string"),
            ],
        )
    )
    await handle_memory_record_write(
        MemoryRecordWriteInput(
            org=org,
            namespace=ns,
            schema_name="probe",
            memory_key="p1",
            agent="records-at-k-test",
            fields={
                "active": RecordFieldInput(value=True),  # stored as "true", raw JSON true
                "count": RecordFieldInput(value="10"),
                "source": RecordFieldInput(value=None, state="rejected"),
            },
        )
    )
    await asyncio.sleep(0.05)
    k = datetime.now(UTC)
    probes: list[tuple[dict[str, str], list[RecordPredicate]]] = [
        ({"active": "true"}, []),
        ({}, [RecordPredicate(field="count", op="gte", number=10)]),
        ({}, [RecordPredicate(field="source", value_state="rejected")]),
    ]
    for filters, predicates in probes:
        args = dict(
            org=org, namespace=ns, schema_name="probe", filters=filters, predicates=predicates
        )
        now = await handle_memory_record_query(MemoryRecordQueryInput(**args))
        then = await handle_memory_record_query(MemoryRecordQueryInput(**args, known_as_of=k))
        assert [r.memory_key for r in now.rows] == [r.memory_key for r in then.rows] == ["p1"], (
            filters,
            predicates,
        )
