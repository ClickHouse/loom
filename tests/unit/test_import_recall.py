"""Unit coverage for the external-vector-recall → Loom import mapping
(server/cli/import_recall.py `_recall_*` helpers) driven end-to-end through the
schema-introspecting planner.

The source store's SQLite schema is not known ahead of time, so the importer
introspects the DB and classifies each table by its columns. These tests
build a synthetic recall-store-shaped SQLite (a memories table + a messages
table + a junk table) and assert:

  - the memories rows map to correct memory.set payloads (value, memory_type,
    tags, valid_at, source provenance);
  - the messages rows map to conversation.create/append writes grouped by
    conversation id and ordered by timestamp;
  - a table matching none of the heuristics is skipped without error;
  - the pure mapping/detection functions behave (type remap, metadata tags,
    role/timestamp normalization, defensive parsing).

The actual loom write call is mocked — no live server / ClickHouse needed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from server.cli.import_recall import (
    _cmd_import_recall,
    _recall_classify_table,
    _recall_detect_memory_columns,
    _recall_detect_message_columns,
    _recall_group_messages,
    _recall_map_memory_row,
    _recall_memory_tags,
    _recall_memory_type,
    _recall_normalize_role,
    _recall_parse_dt,
    _recall_plan_writes,
)

# ---- synthetic fixture DB ------------------------------------------------

pytestmark = pytest.mark.usefixtures("stub_model_command")


def _build_fixture(path: str) -> None:
    """A plausible recall-store shape: a `memories` table (id, content,
    metadata json, memory_type, created_at), a `messages` table (id,
    conversation_id, role, content, ts), and a junk table matching neither
    heuristic."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            content TEXT,
            metadata TEXT,
            memory_type TEXT,
            created_at TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            conversation_id TEXT,
            role TEXT,
            content TEXT,
            ts TEXT
        );
        CREATE TABLE embeddings_blob (
            vec_id INTEGER PRIMARY KEY,
            dim INTEGER,
            norm REAL
        );
        """
    )
    db.executemany(
        "INSERT INTO memories (id, content, metadata, memory_type, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            (
                1,
                "The user prefers dark mode",
                '{"tags": ["ui", "preference"], "confidence": 0.9}',
                "preference",
                "2026-01-02T03:04:05Z",
            ),
            (2, "Deploys happen on Fridays", "{}", "episodic", "2026-02-01T10:00:00Z"),
            (3, "   ", '{"tags": ["junk"]}', "semantic", "2026-03-01T00:00:00Z"),
        ],
    )
    db.executemany(
        "INSERT INTO messages (id, conversation_id, role, content, ts) VALUES (?, ?, ?, ?, ?)",
        [
            # conv B appears first but its messages are out of ts order.
            (10, "convB", "user", "second turn in B", "2026-01-01T00:00:02Z"),
            (11, "convA", "human", "hi there", "2026-01-01T00:00:00Z"),
            (12, "convB", "ai", "first turn in B", "2026-01-01T00:00:01Z"),
            (13, "convA", "assistant", "hello!", "2026-01-01T00:00:03Z"),
            (14, "convA", "user", "", "2026-01-01T00:00:04Z"),  # empty → skip
        ],
    )
    db.executemany(
        "INSERT INTO embeddings_blob (vec_id, dim, norm) VALUES (?, ?, ?)",
        [(1, 384, 1.0), (2, 384, 0.98)],
    )
    db.commit()
    db.close()


@pytest.fixture
def fixture_db(tmp_path):
    p = tmp_path / "recall.sqlite"
    _build_fixture(str(p))
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---- table classification ------------------------------------------------


def test_classify_memories_table() -> None:
    cols = [
        ("id", "INTEGER"),
        ("content", "TEXT"),
        ("metadata", "TEXT"),
        ("memory_type", "TEXT"),
        ("created_at", "TEXT"),
    ]
    kind, colmap = _recall_classify_table(cols)
    assert kind == "memories"
    assert colmap["content"] == "content"
    assert colmap["type"] == "memory_type"
    assert colmap["metadata"] == "metadata"
    assert colmap["ts"] == "created_at"
    assert colmap["id"] == "id"


def test_classify_messages_table_wins_over_memories() -> None:
    # A transcript also has a `content` column; the conv-id + role + content
    # signature must classify it as messages, not memories.
    cols = [
        ("id", "INTEGER"),
        ("conversation_id", "TEXT"),
        ("role", "TEXT"),
        ("content", "TEXT"),
        ("ts", "TEXT"),
    ]
    kind, colmap = _recall_classify_table(cols)
    assert kind == "messages"
    assert colmap["conversation_id"] == "conversation_id"
    assert colmap["role"] == "role"


def test_classify_ignores_table_with_no_text_columns() -> None:
    # embeddings_blob is all numeric — no content column, no conv/role → ignore.
    cols = [("vec_id", "INTEGER"), ("dim", "INTEGER"), ("norm", "REAL")]
    kind, colmap = _recall_classify_table(cols)
    assert kind == "ignore"
    assert colmap == {}


def test_session_id_without_role_is_not_a_transcript() -> None:
    # A memories table carrying a session_id but no role must NOT be read as
    # messages (requiring `role` is what prevents that).
    cols = [("id", "INTEGER"), ("session_id", "TEXT"), ("content", "TEXT")]
    assert _recall_detect_message_columns(cols) is None
    kind, _ = _recall_classify_table(cols)
    assert kind == "memories"


def test_detect_memory_falls_back_to_first_text_column() -> None:
    # An unrecognised text-column name still yields a memories table via the
    # TEXT-type fallback (id/ts are excluded from the fallback).
    cols = [("id", "INTEGER"), ("blurb", "VARCHAR(500)"), ("created", "TEXT")]
    colmap = _recall_detect_memory_columns(cols)
    assert colmap is not None
    assert colmap["content"] == "blurb"
    assert colmap["ts"] == "created"


# ---- pure field mappings -------------------------------------------------


def test_memory_type_remaps_non_searchable_and_keeps_label() -> None:
    # episodic is non-searchable in loom; a recall store wants it searchable,
    # so it becomes semantic with the original label preserved for a kind tag.
    assert _recall_memory_type("episodic") == ("semantic", "episodic")
    assert _recall_memory_type("profile") == ("semantic", "profile")
    # A normal type passes through with no relabel.
    assert _recall_memory_type("preference") == ("preference", "")
    # Empty / null → semantic default.
    assert _recall_memory_type("") == ("semantic", "")
    assert _recall_memory_type(None) == ("semantic", "")


def test_memory_tags_preserve_source_kind_and_metadata() -> None:
    tags = _recall_memory_tags(
        source_id=7,
        meta={"tags": ["ui", "preference"], "category": "prefs"},
        original_type_label="episodic",
    )
    assert "source:recall" in tags  # whole-import provenance filter
    assert "recall_id:7" in tags  # source-row traceability
    assert "kind:episodic" in tags  # remapped-away memory_type survives
    assert "kind:prefs" in tags  # metadata category
    assert "ui" in tags and "preference" in tags
    assert len(tags) == len(set(tags))  # deduped


def test_parse_dt_handles_iso_epoch_and_garbage() -> None:
    assert _recall_parse_dt("2026-01-02T03:04:05Z") == datetime(2026, 1, 2, 3, 4, 5)
    # unix seconds
    assert _recall_parse_dt(1735786800) == datetime(2025, 1, 2, 3, 0, 0)
    # unix milliseconds (magnitude-sniffed)
    assert _recall_parse_dt(1735786800000) == datetime(2025, 1, 2, 3, 0, 0)
    # unparseable → None, never raises
    assert _recall_parse_dt("not a date") is None
    assert _recall_parse_dt(None) is None
    assert _recall_parse_dt("") is None


def test_normalize_role_maps_to_loom_vocabulary() -> None:
    assert _recall_normalize_role("human") == "user"
    assert _recall_normalize_role("AI") == "assistant"
    assert _recall_normalize_role("assistant") == "assistant"
    assert _recall_normalize_role("system") == "system"
    assert _recall_normalize_role("function") == "tool"
    # Unknown → user (safe default in the fixed 4-role vocabulary).
    assert _recall_normalize_role("wizard") == "user"


def test_map_memory_row_produces_correct_payload() -> None:
    colmap = {
        "content": "content",
        "type": "memory_type",
        "metadata": "metadata",
        "ts": "created_at",
        "id": "id",
    }
    row = {
        "id": 1,
        "content": "The user prefers dark mode",
        "metadata": '{"tags": ["ui"], "confidence": 0.9, "entities": ["dark mode"]}',
        "memory_type": "preference",
        "created_at": "2026-01-02T03:04:05Z",
    }
    payload = _recall_map_memory_row(row, colmap, table="memories")
    assert payload is not None
    assert payload["value"] == "The user prefers dark mode"
    assert payload["memory_type"] == "preference"
    assert payload["valid_at"] == datetime(2026, 1, 2, 3, 4, 5)
    assert payload["confidence"] == 0.9
    assert payload["entities"] == ["dark mode"]
    assert payload["memory_key"] == "recall_memories_1"
    assert payload["source_trace"] == "recall://memories/1"
    assert payload["source_resource_id"] == "recall:memories:1"
    assert payload["source_class"] == "imported"
    assert "source:recall" in payload["tags"]
    assert "recall_id:1" in payload["tags"]
    assert "ui" in payload["tags"]


def test_map_memory_row_skips_empty_content() -> None:
    colmap = {"content": "content", "type": None, "metadata": None, "ts": None, "id": "id"}
    assert _recall_map_memory_row({"id": 9, "content": "   "}, colmap, table="memories") is None
    assert _recall_map_memory_row({"id": 9, "content": None}, colmap, table="memories") is None


def test_group_messages_orders_by_ts_within_conversation() -> None:
    msgs = [
        {
            "conversation_id": "B",
            "role": "user",
            "content": "b2",
            "ts": datetime(2026, 1, 1, 0, 0, 2),
            "msg_id": "10",
        },
        {
            "conversation_id": "A",
            "role": "user",
            "content": "a1",
            "ts": datetime(2026, 1, 1, 0, 0, 0),
            "msg_id": "11",
        },
        {
            "conversation_id": "B",
            "role": "assistant",
            "content": "b1",
            "ts": datetime(2026, 1, 1, 0, 0, 1),
            "msg_id": "12",
        },
    ]
    grouped = _recall_group_messages(msgs)
    # First-seen conversation order preserved: B before A.
    assert [cid for cid, _ in grouped] == ["B", "A"]
    b_contents = [m["content"] for m in grouped[0][1]]
    assert b_contents == ["b1", "b2"]  # reordered by ts


# ---- end-to-end planner over the fixture DB (write call mocked) ----------


def test_plan_writes_over_fixture(fixture_db) -> None:
    writes, report = _recall_plan_writes(fixture_db, org="o", namespace="n", agent="recall")

    # Per-table classification + counts.
    assert report["tables"]["memories"]["classification"] == "memories"
    assert report["tables"]["messages"]["classification"] == "messages"
    assert report["tables"]["embeddings_blob"]["classification"] == "ignore"
    # 2 usable memories (row 3 is blank → skipped).
    assert report["memories_imported"] == 2
    assert report["tables"]["memories"]["skipped"] == 1
    # 4 usable messages (one blank skipped) across 2 threads.
    assert report["messages_imported"] == 4
    assert report["threads"] == 2
    assert report["tables"]["messages"]["skipped"] == 1

    ops = [w["op"] for w in writes]
    assert ops.count("memory.set") == 2
    assert ops.count("conversation.create") == 2
    assert ops.count("conversation.append") == 4

    # Every write carries the identity tuple.
    for w in writes:
        assert w["payload"]["org"] == "o"
        assert w["payload"]["namespace"] == "n"
        assert w["payload"]["agent"] == "recall"

    # The episodic memory got remapped to semantic + kept its kind tag.
    mem = {w["payload"]["memory_key"]: w["payload"] for w in writes if w["op"] == "memory.set"}
    row2 = mem["recall_memories_2"]
    assert row2["memory_type"] == "semantic"
    assert "kind:episodic" in row2["tags"]
    assert row2["valid_at"] == datetime(2026, 2, 1, 10, 0, 0)

    # convA thread: create then its two messages, ordered by ts, roles mapped.
    appends_a = [
        w["payload"]
        for w in writes
        if w["op"] == "conversation.append" and w["payload"]["thread_id"] == "recall_messages_convA"
    ]
    assert [m["content"] for m in appends_a] == ["hi there", "hello!"]
    assert [m["role"] for m in appends_a] == ["user", "assistant"]
    # Original timestamp preserved in metadata (append has no ts input field).
    assert appends_a[0]["metadata"]["src_ts"] == "2026-01-01T00:00:00"


def test_cmd_import_recall_executes_plan_with_mocked_writer(tmp_path, monkeypatch) -> None:
    """The command body opens the DB, plans, and drives the writer. Mock the
    writer so no ClickHouse / server is required; assert it received exactly
    the planned ops."""
    p = tmp_path / "recall.sqlite"
    _build_fixture(str(p))

    calls: list[tuple[str, dict]] = []

    async def _fake_writer(op, payload):
        calls.append((op, payload))

    async def _fake_close():
        pass

    async def _fake_make_writer(args):
        return _fake_writer, _fake_close

    # Patch the writer factory on the module the command resolves it from.
    monkeypatch.setattr("server.cli.import_recall._recall_make_writer", _fake_make_writer)

    class _Args:
        sqlite = str(p)
        org = "o"
        namespace = "n"
        agent = "recall"
        base_url = None
        token = None
        dry_run = False

    rc = _cmd_import_recall(_Args())
    assert rc == 0
    ops = [op for op, _ in calls]
    assert ops.count("memory.set") == 2
    assert ops.count("conversation.create") == 2
    assert ops.count("conversation.append") == 4


def test_cmd_import_recall_dry_run_writes_nothing(tmp_path, monkeypatch) -> None:
    p = tmp_path / "recall.sqlite"
    _build_fixture(str(p))

    called = False

    async def _boom(args):
        nonlocal called
        called = True
        raise AssertionError("writer must not be built in dry-run")

    monkeypatch.setattr("server.cli.import_recall._recall_make_writer", _boom)

    class _Args:
        sqlite = str(p)
        org = "o"
        namespace = "n"
        agent = "recall"
        base_url = None
        token = None
        dry_run = True

    rc = _cmd_import_recall(_Args())
    assert rc == 0
    assert called is False


def test_cmd_import_recall_missing_tables_is_graceful(tmp_path, monkeypatch) -> None:
    """A DB whose only table matches no heuristic imports nothing and exits 0
    without ever building a writer."""
    p = tmp_path / "empty.sqlite"
    db = sqlite3.connect(str(p))
    db.execute("CREATE TABLE junk (a INTEGER, b REAL)")
    db.execute("INSERT INTO junk VALUES (1, 2.0)")
    db.commit()
    db.close()

    async def _boom(args):
        raise AssertionError("no writer should be built when nothing matches")

    monkeypatch.setattr("server.cli.import_recall._recall_make_writer", _boom)

    class _Args:
        sqlite = str(p)
        org = "o"
        namespace = "n"
        agent = "recall"
        base_url = None
        token = None
        dry_run = False

    rc = _cmd_import_recall(_Args())
    assert rc == 0


@pytest.mark.asyncio
async def test_make_writer_in_process_routes_ops_and_disconnects(monkeypatch) -> None:
    import argparse

    from server.cli import import_recall

    events: list[str] = []

    class _CH:
        async def connect(self) -> None:
            events.append("connect")

        async def disconnect(self) -> None:
            events.append("disconnect")

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())

    async def _set(p):
        events.append(f"set:{p['k']}")

    writer, closer = await import_recall._make_writer(
        argparse.Namespace(base_url=None, token=None), lambda: {"memory.set": _set}, timeout_s=1.0
    )
    await writer("memory.set", {"k": "a"})
    with pytest.raises(ValueError, match="unknown op"):
        await writer("memory.delete", {})
    await closer()
    assert events == ["connect", "set:a", "disconnect"]


@pytest.mark.asyncio
async def test_make_writer_over_http_posts_with_the_token(monkeypatch) -> None:
    import argparse

    import httpx

    from server.cli import import_recall

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization", "")))
        return httpx.Response(200, json={})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )

    def _no_inproc():
        raise AssertionError("--base-url never builds the in-process handlers")

    writer, closer = await import_recall._make_writer(
        argparse.Namespace(base_url="http://loom.test/", token="T"), _no_inproc, timeout_s=1.0
    )
    await writer("profile.set", {"label": "x"})
    await closer()
    assert seen == [("/v1/profile.set", "Bearer T")]


@pytest.mark.asyncio
async def test_a_write_awaiting_publication_is_done_over_http_and_in_process(monkeypatch) -> None:
    """PUBLICATION_PENDING says the write committed and is not to be repeated: neither
    writer counts it as failed. Any other 503 still fails."""
    import argparse

    import httpx

    from server.cli import import_recall
    from server.core.pg_lease import PublicationPending

    codes = {"/v1/knowledge.add": "PUBLICATION_PENDING", "/v1/memory.set": "BACKEND_UNAVAILABLE"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": {"code": codes[request.url.path]}})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    )
    writer, closer = await import_recall._make_writer(
        argparse.Namespace(base_url="http://loom.test/", token="T"), dict, timeout_s=1.0
    )
    await writer("knowledge.add", {})
    with pytest.raises(httpx.HTTPStatusError):
        await writer("memory.set", {})
    await closer()

    class _CH:
        async def connect(self) -> None: ...

        async def disconnect(self) -> None: ...

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())

    async def _pending(p):
        raise PublicationPending()

    writer, closer = await import_recall._make_writer(
        argparse.Namespace(base_url=None, token=None),
        lambda: {"knowledge.add": _pending},
        timeout_s=1.0,
    )
    await writer("knowledge.add", {})
    await closer()
