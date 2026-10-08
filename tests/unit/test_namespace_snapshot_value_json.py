"""A snapshot carries `value_json` as text and the `value`
bytes verbatim.

`loom.memory.value_json` is a ClickHouse JSON column; the client returns it
parsed — `{}` for the placeholder `memory.set` writes when the value is not an
object — while the payload model declares it `str`, so a raw read answers
`namespace.snapshot` with a 500 (`value_json: Input should be a valid string
… input_type=dict`). The snapshot SELECT reads `toString(value_json)` like
the repository projection, the
payload model dumps a parsed object it is still handed (an older payload, a
raw-column read), and a restore writes the same text and the same `value`
bytes back. CH-free.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from server.core.hashing import sha256_value_json
from server.identity.api.models import (
    NamespaceRestoreInput,
    NamespaceSnapshotInput,
    _SnapshotMemoryRow,
)
from server.identity.app import namespace as registry
from server.identity.app import namespace_snapshot as ns
from server.memory import store as mr
from server.memory.app import snapshot_rows
from server.memory.store import MEMORY_PERSISTED_COLS, MemoryRow

_NOW = datetime(2026, 9, 19, 8, 40, 0)

# The three value shapes: `stored` is the `value`
# column as the writer left it, `parsed` what a read hydrates it to, and
# `value_json` what the JSON column holds — the parsed object for an object
# value, the `{}` placeholder otherwise. The client returns the column as a
# dict; the wire-shape text is what `toString(value_json)` renders.
_JSON_STRING = '{\n  "available_at": null,\n  "host": "ch-3",\n  "minutes": 42\n}'
_PLAIN = "hello, world"
_OBJECT = {"b": [1, 2], "a": {"c": None}}
_CASES: dict[str, tuple[str, Any, dict[str, Any]]] = {
    "json_string": (json.dumps(_JSON_STRING), _JSON_STRING, {}),
    "plain_string": (json.dumps(_PLAIN), _PLAIN, {}),
    "object": ('{\n  "b": [1, 2],\n  "a": {\n    "c": null\n  }\n}', _OBJECT, _OBJECT),
}


def _memory_row(stored: str, parsed: Any, **over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "clickhouse",
        "namespace": "ops-alerts",
        "agent": "sre",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "alert_3f9c",
        "memory_type": "semantic",
        "value": parsed,
        "value_text": stored,
        "confidence": 0.9,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=3),
        "expires_at": None,
        "tags": ["alert"],
        "entities": ["ch-3"],
        "content_sha256": sha256_value_json(stored),
    }
    base.update(over)
    return MemoryRow(**base)


def _ch_row(name: str, *, raw_json_column: bool) -> dict[str, Any]:
    """One `loom.memory` row as the fake client hands it to the snapshot
    handler: every persisted column, `value` the stored bytes. With
    `raw_json_column` the JSON column arrives parsed (a dict), as the client
    returns it for a bare `value_json` read; otherwise as the text
    `toString(value_json)` renders."""
    stored, parsed, obj = _CASES[name]
    row = mr.build_memory_version_row(_memory_row(stored, parsed))
    row["value_json"] = dict(obj) if raw_json_column else json.dumps(obj, separators=(",", ":"))
    return row


class _CH:
    """Answers `query_all` by the first marker found in the SQL; records inserts."""

    def __init__(self, routes: list[tuple[str, list[dict[str, Any]]]]) -> None:
        self.routes = routes
        self.sqls: list[str] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        self.sqls.append(sql)
        for needle, result in self.routes:
            if needle in sql:
                return [dict(r) for r in result]
        return []

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        self.sqls.append(sql)
        return None

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, [dict(r) for r in rows]))

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows in self.inserts if t == table for r in rows]


def _wire(monkeypatch: pytest.MonkeyPatch, memory_rows: list[dict[str, Any]]) -> _CH:
    from server.memory.app import namespace_types as _types
    from server.observability import audit as _audit

    ch = _CH(routes=[("FROM loom.memory FINAL", memory_rows)])
    monkeypatch.setattr(ns, "get_client", lambda: ch)
    monkeypatch.setattr(registry, "resolve_subject", lambda *_a, **_k: "human:tester")

    async def _no_types(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []

    async def _no_audit(*_a: Any, **_k: Any) -> str:
        return ""

    monkeypatch.setattr(_types, "list_type_configs", _no_types)
    monkeypatch.setattr(_audit, "emit", _no_audit)
    return ch


def _snapshot_input() -> NamespaceSnapshotInput:
    return NamespaceSnapshotInput(
        org="clickhouse",
        namespace="ops-alerts",
        include_embeddings=True,
        include_audit_tail_n=0,
    )


# ------------------------------------------------------------- the SELECT


def test_the_snapshot_select_reads_the_json_column_as_text() -> None:
    """Consistent with `_MEMORY_FINAL_PROJECTION`: the payload never sees the
    client's parsed form of the JSON column."""
    assert "toString(value_json) AS value_json" in snapshot_rows.SNAPSHOT_SELECT
    assert "toString(value_json) AS value_json" in mr._MEMORY_FINAL_PROJECTION
    # the raw `value` column, not the projection's rendering of value_json
    assert "value" in snapshot_rows.SNAPSHOT_SELECT.split(",\n            ")


# ------------------------------------------------------------- the model


def test_the_payload_row_dumps_a_parsed_json_column() -> None:
    """A dict where the model wants text."""
    kw: dict[str, Any] = dict(
        memory_id="m",
        agent="a",
        user_id="-",
        session_id="-",
        run_id="-",
        memory_key="k",
        memory_type="semantic",
        value='"v"',
        confidence=1.0,
        source_trace="",
        pinned=False,
    )
    assert _SnapshotMemoryRow(**kw, value_json={}).value_json == "{}"
    assert _SnapshotMemoryRow(**kw, value_json={"a": 1}).value_json == '{"a": 1}'
    assert _SnapshotMemoryRow(**kw, value_json=[1, 2]).value_json == "[1, 2]"
    assert _SnapshotMemoryRow(**kw, value_json=b'{"a":1}').value_json == '{"a":1}'
    assert _SnapshotMemoryRow(**kw, value_json=None).value_json == "{}"
    # text passes through unchanged, whitespace included
    assert _SnapshotMemoryRow(**kw, value_json='{"a":  1}').value_json == '{"a":  1}'
    assert _SnapshotMemoryRow(**kw).value_json == "{}"
    # the field stays a string on the wire
    assert _SnapshotMemoryRow.model_fields["value_json"].annotation is str


@pytest.mark.parametrize("name", sorted(_CASES))
def test_snapshot_memory_row_takes_the_json_column_parsed_or_as_text(name: str) -> None:
    stored, _parsed, obj = _CASES[name]
    from_dict = ns.snapshot_memory_row(_ch_row(name, raw_json_column=True))
    from_text = ns.snapshot_memory_row(_ch_row(name, raw_json_column=False))
    assert json.loads(from_dict.value_json) == obj
    assert from_text.value_json == json.dumps(obj, separators=(",", ":"))
    assert from_dict.value == from_text.value == stored


# ------------------------------------------------------------- the handler


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_json_column", [True, False], ids=["client-dict", "toString"])
async def test_a_namespace_with_rows_snapshots(
    monkeypatch: pytest.MonkeyPatch, raw_json_column: bool
) -> None:
    """A namespace mixing object and non-object values snapshots; a `{}`
    dict never answers a 500."""
    rows = [_ch_row(name, raw_json_column=raw_json_column) for name in sorted(_CASES)]
    for i, r in enumerate(rows):
        r["memory_id"] = f"00000000-0000-0000-0000-00000000000{i + 1}"
        r["memory_key"] = f"k{i + 1}"
    ch = _wire(monkeypatch, rows)
    out = await ns.handle_namespace_snapshot(_snapshot_input())
    assert out.memory_count == len(_CASES) == len(out.memories)
    by_key = {m.memory_key: m for m in out.memories}
    for i, name in enumerate(sorted(_CASES)):
        stored, _parsed, obj = _CASES[name]
        m = by_key[f"k{i + 1}"]
        assert m.value == stored  # the raw column: the stored bytes verbatim
        assert isinstance(m.value_json, str) and json.loads(m.value_json) == obj
    (memory_sql,) = [s for s in ch.sqls if "FROM loom.memory FINAL" in s]
    assert "toString(value_json) AS value_json" in memory_sql
    # the payload serialises: value_json is text on the wire
    wire = json.loads(out.model_dump_json())
    assert all(isinstance(m["value_json"], str) for m in wire["memories"])
    (snap,) = ch.rows_for("loom.namespace_snapshots")
    assert snap["memory_count"] == len(_CASES) and snap["include_embeddings"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_CASES))
async def test_a_restore_of_the_snapshot_writes_the_same_bytes(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Snapshot → restore round trip through both handlers: the target row's
    `value` is byte-identical to the source row's (same hash, same encoding
    level) and `value_json` is the same JSON text the column serves."""
    stored, parsed, obj = _CASES[name]
    source = _ch_row(name, raw_json_column=True)
    ch = _wire(monkeypatch, [source])
    payload = await ns.handle_namespace_snapshot(_snapshot_input())

    ch.inserts.clear()
    out = await ns.handle_namespace_restore(
        NamespaceRestoreInput(org="clickhouse", target_namespace="ops-alerts-copy", payload=payload)
    )
    assert out.restored is True and out.counts["memories"] == 1
    (restored,) = ch.rows_for("loom.memory")
    assert set(restored) == set(MEMORY_PERSISTED_COLS)
    assert restored["value"] == source["value"] == stored
    assert sha256_value_json(restored["value"]) == sha256_value_json(stored)
    assert type(json.loads(restored["value"])) is type(parsed)
    assert json.loads(restored["value"]) == parsed
    # the JSON column gets JSON text back (what memory.set writes), same object
    assert isinstance(restored["value_json"], str)
    assert json.loads(restored["value_json"]) == obj
    assert restored["content_sha256"] == source["content_sha256"]
    assert restored["namespace"] == "ops-alerts-copy" and restored["source_class"] == "imported"


def test_an_older_payload_with_a_parsed_value_json_restores() -> None:
    """A payload written before this record (or by hand) may carry the object
    itself; a restore still writes JSON text and the `value` bytes as given."""
    stored, _parsed, obj = _CASES["object"]
    payload = json.loads(
        ns.snapshot_memory_row(_ch_row("object", raw_json_column=False)).model_dump_json()
    )
    payload["value_json"] = obj  # the parsed form an older writer produced
    row = _SnapshotMemoryRow(**payload)
    (restored,) = snapshot_rows.restore_rows(
        [row],
        org="o",
        target_namespace="n",
        rebind_agent=None,
        version_base=1,
        actor="process:restore",
    )
    assert restored["value"] == stored
    assert isinstance(restored["value_json"], str) and json.loads(restored["value_json"]) == obj
