"""The full stored-row proof accepts native JSON without weakening other fields."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bench import cell_trigger_gate as gate
from tests.unit.test_bench_cell_trigger_gate import (
    args,
    stored_bfloat16,
)
from tests.unit.test_bench_cell_trigger_gate import (
    no_external_io as no_external_io,
)


def native_json(monkeypatch, value):
    from clickhouse_connect.datatypes import dynamic
    from clickhouse_connect.datatypes.base import TypeDef
    from clickhouse_connect.driver.context import BaseQueryContext

    # A fresh type leaves the installed registry and global format map untouched.
    dtype = dynamic.JSON(TypeDef())
    context = BaseQueryContext()
    assert dtype.read_format(context) == "native"
    dtype.typed_paths = list(value)
    dtype.typed_types = [
        SimpleNamespace(read_column_data=lambda *_args, v=v: [v]) for v in value.values()
    ]
    monkeypatch.setattr(
        dynamic,
        "SHARED_DATA_TYPE",
        SimpleNamespace(read_column_data=lambda *_args: [{}]),
    )
    state = dynamic.JSONState(2, [], [None] * len(value), [], None)
    decoded = dtype._read_column_binary(object(), 1, context, state)[0]
    assert type(decoded) is dict
    assert decoded == value
    return decoded


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"n": 1},
        {"flag": True},
        {"nullable": None},
        {"nested": {"b": [1, False, None], "a": "text"}},
        {"when": "2026-01-02T03:04:05.123456+01:00"},
        {"text": '\u03bb\n"quoted"'},
        {"z": 0, "a": "1"},
    ],
)
def test_installed_native_json_matches_serialized_authority(monkeypatch, value):
    decoded = native_json(monkeypatch, value)
    gate._compare(
        {"value_json": json.dumps(value, indent=2)},
        {"value_json": decoded},
        ("value_json",),
        "full memory projection",
    )


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ({}, {"extra": 1}),
        ({"n": 1}, {"n": True}),
        ({"n": 1}, {"n": "1"}),
        ({"v": None}, {}),
        ({"v": [1, 2]}, {"v": [2, 1]}),
        ({"v": {}}, {"v": []}),
        ({"v": "2026-01-02T03:04:05.123456"}, {"v": "2026-01-02T03:04:05.123999"}),
        ({"v": "2026-01-02T03:04:05+01:00"}, {"v": "2026-01-02T03:04:05+00:00"}),
    ],
)
def test_native_json_rejects_changed_json_value(monkeypatch, expected, actual):
    with pytest.raises(gate.Invalid, match="full memory projection value_json"):
        gate._compare(
            {"value_json": json.dumps(expected)},
            {"value_json": native_json(monkeypatch, actual)},
            ("value_json",),
            "full memory projection",
        )


@pytest.mark.parametrize("side", ["expected", "actual"])
def test_malformed_json_is_an_invalid_projection(side):
    expected, actual = {"value_json": "{}"}, {"value_json": {}}
    (expected if side == "expected" else actual)["value_json"] = "{broken"
    with pytest.raises(gate.Invalid, match="full memory projection value_json"):
        gate._compare(expected, actual, ("value_json",), "full memory projection")


def test_json_normalization_applies_only_to_value_json():
    with pytest.raises(gate.Invalid, match="full memory projection value"):
        gate._compare({"value": "{}"}, {"value": {}}, ("value",), "full memory projection")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["", "value_json", "value", "version", "vector", "audit_hash"],
    ids=["healthy", "value_json", "value", "version", "vector", "audit_hash"],
)
async def test_actual_verify_keeps_full_proof_with_native_json(monkeypatch, fault):
    import server.embedding
    from server.core.polled_copy import PolledCopy
    from server.embedding.fake import FakeNGramEmbedder
    from server.llm import customer
    from server.memory.store import pg_memory
    from server.memory.store.rows import MEMORY_PERSISTED_COLS

    monkeypatch.setattr(
        customer,
        "_switch",
        PolledCopy("switch", AsyncMock(return_value=frozenset()), logging.getLogger(__name__)),
    )
    monkeypatch.setattr(server.embedding, "get_embedder", lambda: FakeNGramEmbedder(dim=16))
    block = await gate._build_block(args(), 1)
    block.seqs = [("org", "ns", i + 1) for i in range(5)]
    assert len(MEMORY_PERSISTED_COLS) == 62
    assert all(set(row) == set(MEMORY_PERSISTED_COLS) for row in block.rows)
    records = [
        {
            "head": json.dumps(pg_memory.encode_row(row)),
            "revision": json.dumps(pg_memory.encode_row(row)),
            "audit": json.dumps(pg_memory.encode_audit(audit)),
            "seq": i + 1,
            "targets": 0,
            "attempts": 0,
            "last_error": "",
        }
        for i, (row, audit) in enumerate(zip(block.rows, block.audits, strict=True))
    ]
    projections = [
        {**row, "value_json": native_json(monkeypatch, json.loads(row["value_json"]))}
        for row in block.rows
    ]
    documents = [dict(row) for row in block.documents]
    for document in documents:
        for field in ("embedding", "memory_key_embedding"):
            document[field] = stored_bfloat16(document[field])
    audits = [dict(row) for row in block.audits]
    if fault == "value_json":
        projections[0]["value_json"] = native_json(monkeypatch, {"wrong": True})
    elif fault == "value":
        projections[0]["value"] = '"wrong"'
    elif fault == "version":
        projections[0]["version"] -= 1
    elif fault == "vector":
        documents[0]["embedding"] = [0.0] * 16
    elif fault == "audit_hash":
        audits[0]["row_hash"] = "bad"
    conn = SimpleNamespace(
        fetch=AsyncMock(return_value=records),
        fetchrow=AsyncMock(return_value={"pending": 0, "audits": 0, "errors": 0}),
    )
    phases = []
    compare = gate._compare

    def full_compare(expected, actual, fields, phase):
        if phase == "full memory projection":
            assert set(fields) == set(block.rows[0]) and len(fields) == 62
        phases.append(phase)
        return compare(expected, actual, fields, phase)

    monkeypatch.setattr(gate, "_compare", full_compare)

    async def query(sql, params):
        assert params["org"] == "org" and params["ns"] == "ns"
        if "FROM loom.memory FINAL" in sql:
            return projections
        if "FROM loom.memory_embeddings FINAL" in sql:
            return documents
        if "FROM loom.audit_log" in sql:
            return audits
        raise AssertionError(sql)

    if fault:
        reason = {
            "value_json": "full memory projection value_json",
            "value": "full memory projection value$",
            "version": "full memory projection version",
            "vector": "full search document vector",
            "audit_hash": "exact stored audit hashes",
        }[fault]
        with pytest.raises(gate.Invalid, match=reason):
            await gate._verify(conn, SimpleNamespace(query_all=query), args(), block)
    else:
        await gate._verify(conn, SimpleNamespace(query_all=query), args(), block)
        assert phases.count("full memory projection") == 5
        assert phases.count("full search document") == 5
        assert phases.count("full stored audit") == 5
        conn.fetchrow.assert_awaited_once()
