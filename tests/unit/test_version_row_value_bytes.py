"""A version row carries the stored `value` bytes verbatim.

Writing the PARSED value back would land a JSON-string value (a `str` whose
text is JSON) raw — one encoding level lost, JSONType String → Object, the
content hash changed — and re-dump an object value with other whitespace and
key order, so a restored row would differ from what the agent wrote. The loaded row carries
the stored text (`MemoryRow.value_text`: the FINAL projection aliases the raw
column, `SELECT *` reads carry it as `value`, a snapshot's serialized value
becomes it), the builder writes that text unchanged, and a genuinely new
value serialises exactly as memory.set does. CH-free.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core.hashing import sha256_value_json
from server.memory import store as mr
from server.memory.api.models_crud import MemoryDeleteInput, MemoryRestoreInput, MemoryTouchInput
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import restore as restore_mod
from server.memory.app import stack as _stack_mod
from server.memory.store import MEMORY_PERSISTED_COLS, MemoryRow
from tests.unit.claims_fake import fake_key_locks

_NOW = datetime(2026, 9, 19, 8, 0, 0)

# The three value shapes. `stored` is the `value` column as
# the writer left it; `parsed` is what a read hydrates it to.
_JSON_STRING = '{\n  "available_at": null,\n  "host": "ch-3",\n  "minutes": 42\n}'
_PLAIN = "hello, world"
_OBJECT = {"b": [1, 2], "a": {"c": None}}
_CASES: dict[str, tuple[str, Any]] = {
    # memory.set of a string value: json.dumps quotes and escapes it
    "json_string": (json.dumps(_JSON_STRING), _JSON_STRING),
    "plain_string": (json.dumps(_PLAIN), _PLAIN),
    # an object an older writer pretty-printed: whitespace and key order are
    # the writer's, not json.dumps's
    "object": ('{\n  "b": [1, 2],\n  "a": {\n    "c": null\n  }\n}', _OBJECT),
}
# What the FINAL projection's `value` shows for the object case: ClickHouse's
# rendering of value_json (compact, keys sorted) — not the stored bytes.
_RENDERED_OBJECT = '{"a":{"c":null},"b":[1,2]}'


def _row(stored: str, parsed: Any, **over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "test",
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


def _same_bytes_and_hash(written: str, stored: str, parsed: Any) -> None:
    assert written == stored
    assert sha256_value_json(written) == sha256_value_json(stored)
    # the encoding level survives: a string value reads back as a string,
    # an object as an object
    assert type(json.loads(written)) is type(parsed)
    assert json.loads(written) == parsed


class _CH:
    def __init__(self, routes: list[tuple[str, list[dict[str, Any]]]] | None = None) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.routes = routes or []
        self.query_one_result: dict[str, Any] | None = None

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, [dict(r) for r in rows], settings or {}))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return self.query_one_result

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        for needle, result in self.routes:
            if needle in sql:
                return [dict(r) for r in result]
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows, _ in self.inserts if t == table for r in rows]


_EMB_ROW = {
    "embedding_model": "fake-test",
    "embedding_dim": 3,
    "embedding": [0.1, 0.1, 0.1],
    "content_excerpt": "the excerpt the writer's set built (#alert_3f9c)",
    "memory_key_embedding": [0.2, 0.2, 0.2],
    "memory_key_embedding_dim": 3,
}


def _wire(monkeypatch: pytest.MonkeyPatch, existing: MemoryRow) -> _CH:
    ch = _CH(routes=[("FROM loom.memory_embeddings", [dict(_EMB_ROW)])])
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    monkeypatch.setattr(mr, "get_by_key", _get_by_key)
    return ch


# ------------------------------------------------------------- the builder


@pytest.mark.parametrize("name", sorted(_CASES))
def test_builder_writes_the_stored_bytes_verbatim(name: str) -> None:
    stored, parsed = _CASES[name]
    row = mr.build_memory_version_row(_row(stored, parsed), version=101, tombstoned_at=_NOW)
    _same_bytes_and_hash(row["value"], stored, parsed)
    assert row["content_sha256"] == sha256_value_json(stored)
    assert set(row) == set(MEMORY_PERSISTED_COLS)


@pytest.mark.parametrize("name", sorted(_CASES))
def test_lock_and_pin_shaped_version_rows_keep_the_bytes(name: str) -> None:
    stored, parsed = _CASES[name]
    src = _row(stored, parsed)
    locked = mr.build_memory_version_row(
        src, version=101, lock_mode="read_only", lock_reason="freeze", locked_by="human:o"
    )
    pinned = mr.build_memory_version_row(src, version=102, pinned=True)
    _same_bytes_and_hash(locked["value"], stored, parsed)
    _same_bytes_and_hash(pinned["value"], stored, parsed)
    assert locked["lock_mode"] == "read_only" and pinned["pinned"] == 1


def test_writing_the_parsed_value_back_loses_a_level() -> None:
    """What a version row built from the parsed value would write."""
    stored, parsed = _CASES["json_string"]
    lossy = parsed  # a raw write of the parsed str
    assert json.loads(stored) == parsed  # one level in
    assert type(json.loads(lossy)) is dict  # one level lost: String → Object
    assert sha256_value_json(lossy) != sha256_value_json(stored)


def test_a_new_value_serialises_exactly_like_memory_set() -> None:
    # No stored text: the builder serialises the Python value the way
    # memory.set does (`json.dumps(value)`: default separators, insertion
    # order, no default=str), so the bytes match a set of the same value.
    for _, parsed in _CASES.values():
        row = mr.build_memory_version_row(_row("", parsed, value_text=""))
        assert row["value"] == json.dumps(parsed)
        assert mr.serialize_value(parsed) == json.dumps(parsed)
    assert mr.build_memory_version_row(_row("", None, value_text=""))["value"] == "null"
    # a `value=` override is a serialised text the caller owns; written as given
    assert mr.build_memory_version_row(_row("x", "y"), value='"z"')["value"] == '"z"'


def test_stored_value_text_prefers_the_text_and_falls_back_to_the_set_serialisation() -> None:
    assert mr.stored_value_text(_row('"a"', "a")) == '"a"'
    assert mr.stored_value_text(_row("", "a", value_text="")) == '"a"'
    assert mr.stored_value_text(_row("", {"k": 1}, value_text="")) == '{"k": 1}'


# ------------------------------------------------------------- the reads


def _ch_row(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "test",
        "namespace": "ops-alerts",
        "agent": "sre",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "alert_3f9c",
        "memory_type": "semantic",
        "confidence": 0.9,
        "version": 100,
        "pinned": 0,
        "created_at": _NOW,
    }
    base.update(over)
    return base


def test_hydrate_takes_the_stored_text_from_the_final_projection_not_its_rendering() -> None:
    stored, parsed = _CASES["object"]
    # the FINAL projection: `value` is value_json's rendering, `value_text`
    # the raw column
    m = mr._hydrate(_ch_row(value=_RENDERED_OBJECT, value_text=stored, value_json=_RENDERED_OBJECT))
    assert m.value == parsed and m.value_text == stored
    assert mr.build_memory_version_row(m)["value"] == stored

    stored, parsed = _CASES["json_string"]
    m = mr._hydrate(_ch_row(value=stored, value_text=stored, value_json="{}"))
    assert m.value == parsed and isinstance(m.value, str)
    assert mr.build_memory_version_row(m)["value"] == stored


def test_hydrate_takes_the_raw_value_column_on_a_select_star_read() -> None:
    # `fetch_version_rows` (namespace.rollback) and every raw `value` read
    stored, parsed = _CASES["object"]
    m = mr._hydrate(_ch_row(value=stored))
    assert m.value == parsed and m.value_text == stored
    assert mr.build_memory_version_row(m, version=200)["value"] == stored


def test_hydrate_carries_no_text_for_a_preview_page() -> None:
    m = mr._hydrate(_ch_row(value='{"a": 1', value_truncated=1))
    assert m.value_text == "" and m.value_truncated is True


def test_the_final_projection_selects_the_raw_column_beside_the_rendering() -> None:
    proj = mr._MEMORY_FINAL_PROJECTION
    assert "value AS value_text" in proj
    assert "toString(value_json), value) AS value" in proj


# ------------------------------------------------------------- the handlers


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_CASES))
async def test_delete_tombstone_carries_the_stored_bytes(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    stored, parsed = _CASES[name]
    ch = _wire(monkeypatch, _row(stored, parsed))
    out = await mem_handlers.handle_memory_delete(
        MemoryDeleteInput(org="test", namespace="ops-alerts", agent="sre", memory_key="alert_3f9c")
    )
    assert out.tombstoned is True
    (tomb,) = ch.rows_for("loom.memory")
    assert tomb["tombstoned_at"] is not None
    _same_bytes_and_hash(tomb["value"], stored, parsed)
    assert tomb["content_sha256"] == sha256_value_json(stored)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_CASES))
async def test_restore_carries_the_stored_bytes_and_the_mirrors_excerpt(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    stored, parsed = _CASES[name]
    tombstoned_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    existing = _row(stored, parsed, tombstoned_at=tombstoned_at)
    ch = _wire(monkeypatch, existing)
    fake_key_locks(monkeypatch)  # an Undo takes the key's write lock
    ch.query_one_result = {
        "memory_key": "alert_3f9c",
        "memory_type": "semantic",
        "value": stored,
        "confidence": 0.9,
        "source_trace": "",
        "memory_id": existing.memory_id,
        "entities": ["ch-3"],
        "tags": ["alert"],
        "valid_at": None,
        "char_limit": None,
        "pinned": 0,
        "tombstoned_at": tombstoned_at,
        "version": 100,
        "status": "active",
        "superseded_by": "",
    }
    out = await restore_mod.handle_memory_restore(
        MemoryRestoreInput(org="test", namespace="ops-alerts", agent="sre", memory_key="alert_3f9c")
    )
    assert out.restored is True
    (restored,) = ch.rows_for("loom.memory")
    assert restored["tombstoned_at"] is None
    _same_bytes_and_hash(restored["value"], stored, parsed)
    assert restored["content_sha256"] == sha256_value_json(stored)
    # the mirror's excerpt is carried forward like its vectors, not rebuilt
    (emb,) = ch.rows_for("loom.memory_embeddings")
    assert emb["content_excerpt"] == _EMB_ROW["content_excerpt"]
    assert emb["embedding"] == [0.1, 0.1, 0.1] and emb["tombstoned_at"] is None


@pytest.mark.asyncio
async def test_restore_rebuilds_the_excerpt_from_the_stored_bytes_only_when_the_mirror_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, parsed = _CASES["json_string"]
    tombstoned_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
    existing = _row(stored, parsed, tombstoned_at=tombstoned_at)
    ch = _wire(monkeypatch, existing)
    fake_key_locks(monkeypatch)  # an Undo takes the key's write lock
    ch.routes = [("FROM loom.memory_embeddings", [{**_EMB_ROW, "content_excerpt": ""}])]
    ch.query_one_result = {
        "memory_key": "alert_3f9c",
        "memory_type": "semantic",
        "value": stored,
        "confidence": 0.9,
        "source_trace": "",
        "memory_id": existing.memory_id,
        "entities": [],
        "tags": [],
        "valid_at": None,
        "char_limit": None,
        "pinned": 0,
        "tombstoned_at": tombstoned_at,
        "version": 100,
        "status": "active",
        "superseded_by": "",
    }
    await restore_mod.handle_memory_restore(
        MemoryRestoreInput(org="test", namespace="ops-alerts", agent="sre", memory_key="alert_3f9c")
    )
    (emb,) = ch.rows_for("loom.memory_embeddings")
    assert emb["content_excerpt"].startswith(stored[:40])
    assert emb["content_excerpt"].endswith("(#alert_3f9c)")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(_CASES))
async def test_touch_version_row_carries_the_stored_bytes(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    stored, parsed = _CASES[name]
    ch = _wire(monkeypatch, _row(stored, parsed))
    out = await mem_handlers.handle_memory_touch(
        MemoryTouchInput(
            org="test",
            namespace="ops-alerts",
            agent="sre",
            memory_key="alert_3f9c",
            ttl_seconds=3600,
        )
    )
    assert out.touched is True
    (touched,) = ch.rows_for("loom.memory")
    assert touched["expires_at"] is not None
    _same_bytes_and_hash(touched["value"], stored, parsed)


# ------------------------------------------------------------- snapshots


@pytest.mark.parametrize("name", sorted(_CASES))
def test_a_snapshot_restore_writes_the_payloads_serialized_value_verbatim(name: str) -> None:
    from server.identity.app.namespace_snapshot import snapshot_memory_row
    from server.memory.app.snapshot_rows import restore_rows

    stored, parsed = _CASES[name]
    source_row = mr.build_memory_version_row(_row(stored, parsed))
    payload = snapshot_memory_row(source_row)
    assert payload.value == stored  # the payload carries the stored text
    m = MemoryRow.from_snapshot(payload, org="o", namespace="n")
    assert m.value_text == stored
    (restored,) = restore_rows(
        [payload],
        org="o",
        target_namespace="n",
        rebind_agent=None,
        version_base=1,
        actor="process:restore",
    )
    _same_bytes_and_hash(restored["value"], stored, parsed)


def test_from_snapshot_keeps_an_explicit_value_text_over_a_parsed_value() -> None:
    m = MemoryRow.from_snapshot(
        {"memory_key": "k", "agent": "a", "value": {"a": 1}, "value_text": '{"a":1}'},
        org="o",
        namespace="n",
    )
    assert m.value == {"a": 1} and m.value_text == '{"a":1}'
    assert mr.build_memory_version_row(m)["value"] == '{"a":1}'
