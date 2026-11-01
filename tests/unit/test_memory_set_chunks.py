"""A long searchable memory.set value is also written as
verbatim chunks under its key.

The chunk rows carry `parent_memory_key = memory_key` and the deterministic
`session_id = "memory.set:<key>"`, so a re-set replaces the key's own chunks
(tombstoning the ones the new value does not re-assert) and `memory.delete`
tombstones the whole set. Short values, non-searchable types and the setting
off write nothing; the cap truncates and records a drop; a chunk failure
never fails the write.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import server.memory.app.handlers as mem_handlers
from server.memory.api.models_crud import MemoryDeleteInput, MemorySetInput
from server.memory.app import conflicts as cf
from server.memory.app import restore as _restore_mod
from server.memory.app import set_chunks as sc
from server.memory.app import set_commit as set_commit
from server.memory.app import stack as _stack_mod
from server.memory.app import write as write_mod
from server.memory.domain.conflicts import ConflictResult
from server.memory.domain.excerpt import EXCERPT_LIMIT
from server.memory.store import MemoryRow
from server.settings import settings


@pytest.fixture(autouse=True)
def _head_is_embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests' fake client answers no head read: the key's head stays plain."""

    async def _yes(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(sc, "_head_still_embedded", _yes)


_LIMIT = EXCERPT_LIMIT
_KEY = "investigation_notes"
_SID = f"memory.set:{_KEY}"

# ~5 KB of prose with paragraph breaks, the shape of an investigation note.
_NOTE_5KB = "\n\n".join(
    f"Paragraph {i}: the replica lagged for {i * 3} minutes after the merge storm; "
    f"the evidence is in query_log row {1000 + i} and the alert fired at 08:{i:02d}. "
    "The follow-up is to lower max_bytes_to_merge_at_max_space_in_pool on the tier."
    for i in range(1, 27)
)
assert len(_NOTE_5KB) > 4800


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _CH:
    """Records inserts; `query_all` answers routed by a SQL needle."""

    def __init__(self, routes: list[tuple[str, list[dict[str, Any]]]] | None = None) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.query_all_sqls: list[str] = []
        self.routes = routes or []
        self.fail_inserts_into: tuple[str, ...] = ()

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if table in self.fail_inserts_into:
            raise RuntimeError(f"insert into {table} unavailable")
        self.inserts.append((table, rows, settings or {}))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        self.query_all_sqls.append(sql)
        for needle, result in self.routes:
            if needle in sql:
                return result
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows, _ in self.inserts if t == table for r in rows]


class _BatchEmbedder:
    model_name = "fake-batch"
    dim = 3

    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.serial: list[str] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[0.1, 0.2, float(i)] for i, _ in enumerate(texts)]

    async def embed(self, text: str) -> list[float]:
        self.serial.append(text)
        return [0.1, 0.2, 0.3]


class _SerialEmbedder:
    model_name = "fake-serial"
    dim = 3

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [0.5, 0.5, float(len(self.calls))]


def _input(value: Any, **kw: Any) -> MemorySetInput:
    base: dict[str, Any] = {
        "org": "test",
        "namespace": "ops-alerts",
        "agent": "sre",
        "memory_key": _KEY,
        "value": value,
        "memory_type": "semantic",
        "classify": False,
    }
    base.update(kw)
    return MemorySetInput(**base)


def _live_row(index: int, text: str, *, sha: Any = None) -> dict[str, Any]:
    return {
        "agent": "sre",
        "session_id": _SID,
        "run_id": "-",
        "chunk_index": index,
        "content_sha256": sha if sha is not None else _sha(text),
        "content": text,
        "embedding": [0.1, 0.2, 0.3],
        "embedding_model": "fake-batch",
        "embedding_dim": 3,
        "parent_memory_key": _KEY,
        "line_start": 1,
        "line_end": 1,
        "modality": "",
        "caption": "",
        "source_url": _SID,
        "source_local_path": "",
    }


@pytest.fixture(autouse=True)
def _chunking_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_set_chunk_max_chunks", 48, raising=False)


# --- the helper --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_5kb_semantic_string_writes_chunks_under_its_key() -> None:
    ch, emb = _CH(), _BatchEmbedder()
    n = await sc.write_set_chunks(
        _input(_NOTE_5KB, run_id="run-9"), memory_type="semantic", limit=_LIMIT, ch=ch, embedder=emb
    )
    rows = ch.rows_for("loom.memory_chunks")
    assert n == len(rows) >= 5, "a 5 KB note is several 800-char chunks"
    assert len(emb.batches) == 1 and len(emb.batches[0]) == n, "one batched embed request"
    assert emb.serial == []
    assert [r["chunk_index"] for r in rows] == list(range(n))
    for r in rows:
        assert r["parent_memory_key"] == _KEY
        assert r["session_id"] == _SID and r["source_url"] == _SID
        assert r["run_id"] == "run-9" and r["agent"] == "sre"
        assert r["tombstoned_at"] is None
        assert r["content_sha256"] == _sha(r["content"])
        assert r["embedding_model"] == "fake-batch" and r["embedding_dim"] == 3
        assert r["content"] in _NOTE_5KB
    # The chunks between them cover the whole note, not only its head.
    assert "Paragraph 26" in "".join(r["content"] for r in rows)
    # The insert is synchronous: the chunk plane is read on the search path.
    assert ch.inserts[-1][2] == {"async_insert": 0, "wait_for_async_insert": 1}
    # The prior-chunk read is a primary-key read under FINAL on the key's session.
    assert any(
        "FROM loom.memory_chunks FINAL" in s and "session_id" in s for s in ch.query_all_sqls
    )


@pytest.mark.asyncio
async def test_run_id_defaults_to_the_identity_sentinel() -> None:
    ch = _CH()
    await sc.write_set_chunks(
        _input(_NOTE_5KB), memory_type="semantic", limit=_LIMIT, ch=ch, embedder=_BatchEmbedder()
    )
    assert {r["run_id"] for r in ch.rows_for("loom.memory_chunks")} == {"-"}


@pytest.mark.asyncio
async def test_chunks_carry_the_writers_user() -> None:
    """A user-bounded read keeps to chunks stamped with its user: the writer's
    user_id rides every chunk, the sentinel when the write names none."""
    ch = _CH()
    await sc.write_set_chunks(
        _input(_NOTE_5KB, user_id="u-42"),
        memory_type="semantic",
        limit=_LIMIT,
        ch=ch,
        embedder=_BatchEmbedder(),
    )
    assert {r["user_id"] for r in ch.rows_for("loom.memory_chunks")} == {"u-42"}
    ch = _CH()
    await sc.write_set_chunks(
        _input(_NOTE_5KB), memory_type="semantic", limit=_LIMIT, ch=ch, embedder=_BatchEmbedder()
    )
    assert {r["user_id"] for r in ch.rows_for("loom.memory_chunks")} == {"-"}


@pytest.mark.asyncio
async def test_a_tombstone_carries_the_chunks_user_forward() -> None:
    live = {**_live_row(0, "old text"), "user_id": "u-42"}
    ch = _CH(routes=[("FROM loom.memory_chunks FINAL", [live])])
    n = await sc.tombstone_set_chunks(
        org="test", namespace="ops-alerts", memory_key=_KEY, user_id="u-42", ch=ch
    )
    [row] = ch.rows_for("loom.memory_chunks")
    assert n == 1 and row["user_id"] == "u-42" and row["tombstoned_at"] is not None


@pytest.mark.asyncio
async def test_a_5kb_episodic_value_writes_no_chunks() -> None:
    ch, emb = _CH(), _BatchEmbedder()
    n = await sc.write_set_chunks(
        _input(_NOTE_5KB, memory_type="episodic"),
        memory_type="episodic",
        limit=_LIMIT,
        ch=ch,
        embedder=emb,
    )
    assert n == 0 and ch.inserts == [] and emb.batches == []
    assert ch.query_all_sqls == [], "a non-searchable type does not even read prior chunks"


@pytest.mark.asyncio
async def test_a_900_char_value_writes_no_chunks() -> None:
    ch, emb = _CH(), _BatchEmbedder()
    short = ("The replica lagged after the merge storm. " * 30)[:900]
    assert len(short) == 900 < _LIMIT
    n = await sc.write_set_chunks(
        _input(short), memory_type="semantic", limit=_LIMIT, ch=ch, embedder=emb
    )
    assert n == 0 and ch.inserts == [] and emb.batches == [] and ch.query_all_sqls == []


@pytest.mark.asyncio
async def test_a_dict_value_chunks_its_indented_json() -> None:
    value = {
        "attempt_id": "sre-follow-up-86be1a4d",
        "evidence": [f"query_log row {1000 + i} shows the lag at 08:{i:02d}" for i in range(60)],
        "conclusion": "lower the merge pool bytes on the tier",
    }
    ch = _CH()
    n = await sc.write_set_chunks(
        _input(value), memory_type="semantic", limit=_LIMIT, ch=ch, embedder=_BatchEmbedder()
    )
    rows = ch.rows_for("loom.memory_chunks")
    assert n == len(rows) >= 3
    text = sc.human_text(value, "semantic")
    assert text == json.dumps(value, ensure_ascii=False, indent=1)
    for r in rows:
        assert r["content"] in text
    joined = "\n".join(r["content"] for r in rows)
    # indent=1 puts one list item per line, so a chunk boundary falls between
    # items and every item is whole in some chunk.
    assert ' "query_log row 1059 shows the lag at 08:59"' in joined
    assert '"conclusion": "lower the merge pool bytes on the tier"' in joined
    assert '\\"' not in joined, "the human text, not the JSON-escaped stored value"


def test_human_text_shapes() -> None:
    assert sc.human_text("plain", "semantic") == "plain"
    assert sc.human_text(None, "semantic") == ""
    assert sc.human_text(["a", "b"], "semantic") == '[\n "a",\n "b"\n]'
    assert sc.human_text({"k": "é"}, "semantic") == '{\n "k": "é"\n}'
    assert sc.human_text(7, "semantic") == "7"


@pytest.mark.asyncio
async def test_a_reset_with_shorter_text_tombstones_the_chunks_it_no_longer_asserts() -> None:
    # First write: 5 KB → N chunks (recorded through a first stub).
    first = _CH()
    await sc.write_set_chunks(
        _input(_NOTE_5KB), memory_type="semantic", limit=_LIMIT, ch=first, embedder=_BatchEmbedder()
    )
    prior = first.rows_for("loom.memory_chunks")
    n_prior = len(prior)
    # The re-set keeps the first two paragraphs verbatim and rewrites the rest,
    # so chunk 0 is re-asserted unchanged and the tail shrinks.
    shorter = _NOTE_5KB[:1700] + "\n\nResolved: the tier was resized; nothing further. " * 8
    live = [_live_row(r["chunk_index"], r["content"]) for r in prior]
    # A FixedString(64) reads back as bytes from the driver.
    live[0]["content_sha256"] = live[0]["content_sha256"].encode()
    ch = _CH(routes=[("FROM loom.memory_chunks FINAL", live)])
    n = await sc.write_set_chunks(
        _input(shorter), memory_type="semantic", limit=_LIMIT, ch=ch, embedder=_BatchEmbedder()
    )
    assert 0 < n < n_prior
    chunk_inserts = [(rows, st) for t, rows, st in ch.inserts if t == "loom.memory_chunks"]
    assert len(chunk_inserts) == 2, "tombstones first, then the live rows"
    tombstones, live_rows = chunk_inserts[0][0], chunk_inserts[1][0]
    assert all(r["tombstoned_at"] is not None for r in tombstones)
    assert all(r["tombstoned_at"] is None for r in live_rows)
    assert [r["chunk_index"] for r in live_rows] == list(range(n))
    new_keys = {(r["chunk_index"], r["content_sha256"]) for r in live_rows}
    tomb_keys = {(r["chunk_index"], r["content_sha256"]) for r in tombstones}
    # Chunk 0 is unchanged: re-inserted live, NOT tombstoned (its bytes sha matched).
    assert prior[0]["content"] == live_rows[0]["content"]
    assert (0, _sha(prior[0]["content"])) in new_keys
    assert (0, _sha(prior[0]["content"])) not in tomb_keys
    assert not (new_keys & tomb_keys), "a re-asserted chunk is never tombstoned in the same write"
    # Every prior chunk the new value does not re-assert is tombstoned, so no
    # live chunk survives beyond the new count.
    expected_tomb = {(r["chunk_index"], r["content_sha256"]) for r in prior} - new_keys
    assert tomb_keys == expected_tomb
    assert all(
        r["chunk_index"] >= n or (r["chunk_index"], r["content_sha256"]) not in new_keys
        for r in tombstones
    )
    assert {r["chunk_index"] for r in tombstones} >= set(range(n, n_prior))
    # A tombstone carries the live row forward: same key columns, its content
    # and vector, and a version (created_at) no older than the tombstone stamp.
    t0 = next(r for r in tombstones if r["chunk_index"] == n_prior - 1)
    assert t0["content"] == prior[-1]["content"] and t0["embedding"] == [0.1, 0.2, 0.3]
    assert t0["session_id"] == _SID and t0["parent_memory_key"] == _KEY
    assert t0["created_at"] == t0["tombstoned_at"]
    assert isinstance(t0["content_sha256"], str) and len(t0["content_sha256"]) == 64


@pytest.mark.asyncio
async def test_the_cap_truncates_and_records_a_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_set_chunk_max_chunks", 2, raising=False)
    drops: list[tuple[Any, ...]] = []

    def _record(org: str, ns: str, stage: str, reason: str, *, item: str = "", n: int = 1) -> None:
        drops.append((org, ns, stage, reason, item, n))

    monkeypatch.setattr("server.memory.app.drops.record_drop", _record)
    ch = _CH()
    n = await sc.write_set_chunks(
        _input(_NOTE_5KB), memory_type="semantic", limit=_LIMIT, ch=ch, embedder=_BatchEmbedder()
    )
    assert n == 2 and len(ch.rows_for("loom.memory_chunks")) == 2
    assert len(drops) == 1
    org, ns, stage, reason, item, count = drops[0]
    assert (org, ns, stage, reason, count) == (
        "test",
        "ops-alerts",
        "chunk.split",
        "truncated",
        1,
    )
    assert item.startswith(f"memory.set:{_KEY} chars=")
    assert int(item.rsplit("=", 1)[1]) > 2000, "the tail past two chunks is most of the note"


@pytest.mark.asyncio
async def test_an_embedder_without_embed_many_falls_back_serial() -> None:
    ch, emb = _CH(), _SerialEmbedder()
    n = await sc.write_set_chunks(
        _input(_NOTE_5KB), memory_type="semantic", limit=_LIMIT, ch=ch, embedder=emb
    )
    assert n >= 5 and len(emb.calls) == n
    assert {r["embedding_model"] for r in ch.rows_for("loom.memory_chunks")} == {"fake-serial"}


@pytest.mark.asyncio
async def test_a_failing_chunk_insert_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ch = _CH()
    ch.fail_inserts_into = ("loom.memory_chunks",)
    with caplog.at_level("WARNING"):
        n = await sc.write_set_chunks_fail_open(
            _input(_NOTE_5KB),
            memory_type="semantic",
            limit=_LIMIT,
            ch=ch,
            embedder=_BatchEmbedder(),
        )
    assert n == 0
    assert any("verbatim chunks not written" in r.message for r in caplog.records)


# --- memory.delete -----------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_delete_tombstones_the_keys_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    live = [_live_row(0, "first chunk of the note"), _live_row(1, "second chunk of the note")]
    ch = _CH(routes=[("FROM loom.memory_chunks FINAL", live)])
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    now = datetime(2026, 9, 18, 12, 0, 0)
    existing = MemoryRow(
        memory_id="mid-1",
        org="test",
        namespace="ops-alerts",
        agent="sre",
        user_id="-",
        session_id="-",
        memory_key=_KEY,
        memory_type="semantic",
        value=_NOTE_5KB,
        confidence=0.8,
        version=100,
        pinned=False,
        created_at=now - timedelta(days=1),
        expires_at=None,
    )

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    out = await mem_handlers.handle_memory_delete(
        MemoryDeleteInput(org="test", namespace="ops-alerts", agent="sre", memory_key=_KEY)
    )
    assert out.tombstoned is True
    tombs = ch.rows_for("loom.memory_chunks")
    assert [r["chunk_index"] for r in tombs] == [0, 1]
    assert all(r["tombstoned_at"] is not None for r in tombs)
    assert all(r["session_id"] == _SID and r["parent_memory_key"] == _KEY for r in tombs)
    assert all(r["content_sha256"] == _sha(r["content"]) for r in tombs)
    chunk_sql = next(s for s in ch.query_all_sqls if "FROM loom.memory_chunks FINAL" in s)
    assert "session_id = {sid:String}" in chunk_sql and "tombstoned_at IS NULL" in chunk_sql


@pytest.mark.asyncio
async def test_memory_delete_of_an_episodic_row_reads_no_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    existing = MemoryRow(
        memory_id="mid-2",
        org="test",
        namespace="ops-alerts",
        agent="sre",
        user_id="-",
        session_id="-",
        memory_key=_KEY,
        memory_type="episodic",
        value="x",
        confidence=0.8,
        version=100,
        pinned=False,
        created_at=datetime(2026, 9, 18),
        expires_at=None,
    )

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    await mem_handlers.handle_memory_delete(
        MemoryDeleteInput(org="test", namespace="ops-alerts", agent="sre", memory_key=_KEY)
    )
    assert not any("loom.memory_chunks" in s for s in ch.query_all_sqls)


# --- the write path ----------------------------------------------------------


def _wire_set(monkeypatch: pytest.MonkeyPatch, ch: _CH, embedder: Any) -> None:
    """The memory.set write path against the stub store, conflict check `clear`
    (the fixture shape of tests/unit/test_memory_set_verdict.py)."""
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr(mem_handlers, "get_embedder", lambda: embedder)
    monkeypatch.setattr("server.memory.app.set_commit.get_embedder", mem_handlers.get_embedder)
    monkeypatch.setattr("server.memory.app.update.get_embedder", mem_handlers.get_embedder)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch, raising=False)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    import server.memory.app.blacklist_guard as _bl

    monkeypatch.setattr(_bl, "get_client", lambda: ch, raising=False)
    monkeypatch.setattr(cf, "get_client", lambda: ch, raising=False)

    async def _none(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("server.memory.store.get_by_key", _none)
    monkeypatch.setattr("server.memory.store.get_by_id", _none)

    async def _check(**_: Any) -> ConflictResult:
        return ConflictResult(verdict="clear", peers=[])

    monkeypatch.setattr(cf, "check_on_commit", _check)
    monkeypatch.setattr(cf, "mark_peers_conflicted", _none)

    async def _audit(event_type: str, **_: Any) -> str:
        return "hash"

    monkeypatch.setattr("server.observability.audit.emit", _audit)
    monkeypatch.setattr("server.memory.store.record_usage_event", _none)

    async def _supersede(**_: Any) -> int:
        return 0

    monkeypatch.setattr("server.memory.app.key_supersession.supersede_same_key_rows", _supersede)
    monkeypatch.setattr(set_commit, "_project_set_entities_fail_open", _none)


@pytest.mark.asyncio
async def test_memory_set_writes_the_chunks_after_the_row_and_answers_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, emb = _CH(), _BatchEmbedder()
    _wire_set(monkeypatch, ch, emb)
    out = await write_mod.handle_memory_set(_input(_NOTE_5KB, run_id="run-1"))
    tables = [t for t, _, _ in ch.inserts]
    assert "loom.memory" in tables and "loom.memory_embeddings" in tables
    chunks = ch.rows_for("loom.memory_chunks")
    assert out.chunks_written == len(chunks) >= 5
    assert tables.index("loom.memory_chunks") > tables.index("loom.memory"), "row first"
    assert {r["parent_memory_key"] for r in chunks} == {_KEY}
    assert {r["run_id"] for r in chunks} == {"run-1"}
    # The row's own vector still comes from the excerpt: two embeds for the
    # row (value + key axis) in one batch, then one batch for the chunks.
    assert len(emb.batches) == 2 and len(emb.batches[0]) == 2
    assert out.model_dump()["chunks_written"] == out.chunks_written


@pytest.mark.asyncio
async def test_memory_set_of_a_short_value_answers_zero_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH()
    _wire_set(monkeypatch, ch, _BatchEmbedder())
    out = await write_mod.handle_memory_set(_input("Deploys happen on Thursday"))
    assert out.chunks_written == 0 and ch.rows_for("loom.memory_chunks") == []


@pytest.mark.asyncio
async def test_memory_set_succeeds_when_the_chunk_insert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH()
    ch.fail_inserts_into = ("loom.memory_chunks",)
    _wire_set(monkeypatch, ch, _BatchEmbedder())
    out = await write_mod.handle_memory_set(_input(_NOTE_5KB))
    assert out.memory_id and out.version > 0 and out.noop is False
    assert out.chunks_written == 0
    assert any(t == "loom.memory" for t, _, _ in ch.inserts)


@pytest.mark.asyncio
async def test_memory_set_announces_its_row_when_supersession_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is in before the supersession runs: a failure there answers an error,
    and the event is already in the outbox (a retry is a restatement, never announced)."""
    ch = _CH()
    _wire_set(monkeypatch, ch, _BatchEmbedder())
    announced: list[Any] = []

    async def _announce(changes: Any) -> None:
        announced.extend(changes)

    async def _supersede(**_: Any) -> int:
        raise RuntimeError("supersession insert failed")

    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    monkeypatch.setattr("server.memory.app.key_supersession.supersede_same_key_rows", _supersede)
    with pytest.raises(RuntimeError, match="supersession"):
        await write_mod.handle_memory_set(_input("Deploys happen on Thursday"))
    [(event, row)] = announced
    assert event == "memory.written" and row["memory_key"] == _KEY


@pytest.mark.asyncio
async def test_memory_delete_announces_its_tombstone_when_retiring_derived_rows_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tombstone is in before the derived rows are retired: a failure there answers
    an error, and the event is already in the outbox (a retry finds no live row)."""
    ch = _CH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    existing = MemoryRow(
        memory_id="mid-1",
        org="test",
        namespace="ops-alerts",
        agent="sre",
        user_id="-",
        session_id="-",
        memory_key=_KEY,
        memory_type="semantic",
        value="Deploys happen on Thursday",
        confidence=0.8,
        version=100,
        pinned=False,
        created_at=datetime(2026, 9, 17, 12, 0, 0),
        expires_at=None,
    )
    announced: list[Any] = []

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    async def _announce(changes: Any) -> None:
        announced.extend(changes)

    async def _retire(*_: Any, **__: Any) -> tuple[int, int]:
        raise RuntimeError("summary downgrade failed")

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    monkeypatch.setattr(mem_handlers, "_retire_derived", _retire)
    with pytest.raises(RuntimeError, match="summary downgrade"):
        await mem_handlers.handle_memory_delete(
            MemoryDeleteInput(org="test", namespace="ops-alerts", agent="sre", memory_key=_KEY)
        )
    assert [r["tombstoned_at"] is not None for r in ch.rows_for("loom.memory")] == [True]
    [(event, row)] = announced
    assert event == "memory.tombstoned" and row["memory_key"] == _KEY


@pytest.mark.asyncio
async def test_memory_set_announces_its_row_when_the_embedding_insert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dual write's memory row lands while its embedding row fails: the write answers
    an error, and the event is already in the outbox (a retry is a restatement)."""
    ch = _CH()
    ch.fail_inserts_into = ("loom.memory_embeddings",)
    _wire_set(monkeypatch, ch, _BatchEmbedder())
    announced: list[Any] = []

    async def _announce(changes: Any) -> None:
        announced.extend(changes)

    monkeypatch.setattr("server.memory.app.webhooks.announce", _announce)
    with pytest.raises(RuntimeError, match="memory_embeddings"):
        await write_mod.handle_memory_set(_input("Deploys happen on Thursday"))
    assert len(ch.rows_for("loom.memory")) == 1
    [(event, row)] = announced
    assert event == "memory.written" and row["memory_key"] == _KEY


def test_write_outcome_label_precedence() -> None:
    assert set_commit._set_write_outcome("dup-id", "conflict", True) == "duplicate"
    assert set_commit._set_write_outcome("", "conflict", True) == "conflicted"
    assert set_commit._set_write_outcome("", "clear", True) == "updated"
    assert set_commit._set_write_outcome("", "clear", False) == "created"


class _ParamCH(_CH):
    """`_CH` that also records each read's parameters."""

    def __init__(self, routes: list[tuple[str, list[dict[str, Any]]]] | None = None) -> None:
        super().__init__(routes)
        self.params: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
        self.params.append(dict(params or {}))
        return await super().query_all(sql, params, **kw)


@pytest.mark.asyncio
async def test_a_tombstone_lands_in_the_partition_of_the_row_it_retires() -> None:
    """created_at is the version and the monthly partition key: a January chunk
    retired in March gets a January tombstone (a March one would never merge
    with it), with tombstoned_at saying when."""
    january, march = datetime(2026, 1, 20, 9, 0), datetime(2026, 3, 2, 10, 0)
    live = {**_live_row(0, "old text"), "user_id": "u-42", "created_at": january}
    ch = _CH(routes=[("FROM loom.memory_chunks FINAL", [live])])
    await sc.tombstone_set_chunks(
        org="test", namespace="ops-alerts", memory_key=_KEY, user_id="u-42", ch=ch, now=march
    )
    [row] = ch.rows_for("loom.memory_chunks")
    assert row["created_at"] == january and row["tombstoned_at"] == march
    # the live read selects the version column it carries forward
    assert any("source_local_path, created_at" in s for s in ch.query_all_sqls)


@pytest.mark.asyncio
async def test_a_writer_retires_only_its_own_and_legacy_chunks() -> None:
    """Every user holding the key shares its chunk session: a re-set or delete
    retires the writer's chunks and the legacy '-' ones, never another user's."""
    ch = _ParamCH()
    await sc.tombstone_set_chunks(
        org="test", namespace="ops-alerts", memory_key=_KEY, user_id="u-42", ch=ch
    )
    sql = next(s for s in ch.query_all_sqls if "FROM loom.memory_chunks FINAL" in s)
    assert "AND user_id IN ('-', {uid:String})" in sql
    assert ch.params[-1]["uid"] == "u-42"

    ch = _ParamCH()
    await sc.write_set_chunks(
        _input(_NOTE_5KB, user_id="u-7"),
        memory_type="semantic",
        limit=_LIMIT,
        ch=ch,
        embedder=_BatchEmbedder(),
    )
    assert [p["uid"] for p in ch.params if "uid" in p] == ["u-7"]


@pytest.mark.asyncio
async def test_memory_delete_retires_the_deleted_rows_users_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _ParamCH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    existing = MemoryRow(
        memory_id="mid-1",
        org="test",
        namespace="ops-alerts",
        agent="sre",
        user_id="u-42",
        session_id="-",
        memory_key=_KEY,
        memory_type="semantic",
        value=_NOTE_5KB,
        confidence=0.8,
        version=100,
        pinned=False,
        created_at=datetime(2026, 9, 17),
        expires_at=None,
    )

    async def _get_by_key(*_: Any, **__: Any) -> MemoryRow:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    await mem_handlers.handle_memory_delete(
        MemoryDeleteInput(
            org="test", namespace="ops-alerts", agent="sre", user_id="u-42", memory_key=_KEY
        )
    )
    # every chunk read of the delete keeps to the deleted row's user
    assert {p["uid"] for p in ch.params if "uid" in p and "sid" in p} == {"u-42"}


@pytest.mark.asyncio
async def test_chunks_that_land_after_the_key_turned_high_are_tombstoned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer 'high' write retired the key's chunks while these were embedding: the
    writer re-reads the head after its insert and retires what it just wrote."""
    retired: list[str] = []

    async def _no(*_a: Any, **_k: Any) -> bool:
        return False

    async def _tombstone(**kw: Any) -> int:
        retired.append(kw["memory_key"])
        return 1

    class _CH:
        async def insert(self, *_a: Any, **_k: Any) -> None:
            return None

    class _Embedder:
        model_name = "fake"

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(sc, "_head_still_embedded", _no)
    monkeypatch.setattr(sc, "tombstone_set_chunks", _tombstone)
    inp = MemorySetInput(
        org="o", namespace="n", agent="a", memory_key="k", value="word " * (EXCERPT_LIMIT // 2)
    )
    assert await sc.write_set_chunks(inp, ch=_CH(), embedder=_Embedder()) == 0
    # once for the previous value's chunks, once for what this write just inserted
    assert retired == ["k", "k"]


@pytest.mark.asyncio
async def test_a_failed_head_read_retires_the_chunks_it_just_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-check is a security check: when it cannot read the head, the chunks
    this call inserted are retired (and only those), then the error propagates."""
    calls: list[dict[str, Any]] = []

    async def _boom(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("replica down")

    async def _tombstone(**kw: Any) -> int:
        calls.append(kw)
        return 1

    class _CH:
        async def insert(self, *_a: Any, **_k: Any) -> None:
            return None

    class _Embedder:
        model_name = "fake"

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(sc, "_head_still_embedded", _boom)
    monkeypatch.setattr(sc, "tombstone_set_chunks", _tombstone)
    inp = MemorySetInput(
        org="o", namespace="n", agent="a", memory_key="k", value="word " * (EXCERPT_LIMIT // 2)
    )
    with pytest.raises(RuntimeError):
        await sc.write_set_chunks(inp, ch=_CH(), embedder=_Embedder())
    retire = calls[-1]
    assert retire["only"] and retire["strong"] is True


@pytest.fixture
def stubbed_after_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """`set_commit`'s after-write projections stubbed, for the chunk step alone."""

    async def nothing(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(set_commit, "_set_post_insert", nothing)
    monkeypatch.setattr(set_commit, "project_stated_relations", nothing)
    monkeypatch.setattr(set_commit, "follow_beside_response", lambda **_: None)
    monkeypatch.setattr(set_commit, "peer_id_in_read_scope", lambda *_: "")


async def _after_write(duplicate_of: str) -> list[dict[str, Any]]:
    # one memory.set's after-write step over a key that holds a long value's chunks
    served = [_live_row(0, "the served long value's first passage")]
    ch = _CH(routes=[("FROM loom.memory_chunks FINAL", served)])
    w = set_commit._SetWrite(
        input=_input(_NOTE_5KB), version=1, retyped_from="", existing=None, expires_at=None
    )
    w.memory_row = {"status": "duplicate" if duplicate_of else "active"}
    w.embedder = _BatchEmbedder()
    await set_commit._set_after_write(
        w, ch, None, SimpleNamespace(verdict="clear"), duplicate_of, []
    )
    return ch.rows_for("loom.memory_chunks")


@pytest.mark.asyncio
async def test_a_duplicate_leaves_the_keys_chunks_to_the_served_value(
    stubbed_after_write: None,
) -> None:
    assert await _after_write("canonical-id") == []


@pytest.mark.asyncio
async def test_an_active_write_replaces_the_keys_chunks_with_its_own(
    stubbed_after_write: None,
) -> None:
    written = await _after_write("")
    assert any(r["tombstoned_at"] is not None for r in written)
    assert any(r["tombstoned_at"] is None for r in written)
