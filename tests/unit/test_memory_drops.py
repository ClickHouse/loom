"""The memory ingest drop ledger (server/memory/app/drops.py) and its wiring.

- record_drop buffers rows, mirrors the OTel counter and never raises;
  flush_drops writes the batch to loom.memory_drops and re-buffers on a
  ClickHouse failure (fail-open, logged at debug).
- Every discard point in the pipeline records a drop with a bounded
  (stage, reason) pair — pinned per site so a refactor can't silently
  un-wire one.

CH-free: the flush target and every downstream leg are stubbed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from server.memory.app import drops
from server.settings import settings

pytestmark = pytest.mark.usefixtures("stub_model_command")

_real_flush_later = drops._flush_later


@pytest.fixture(autouse=True)
def _quiet_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep drops buffered for assertions; each test's teardown clears them."""
    monkeypatch.setattr(drops, "_flush_later", lambda delay: True)


def _pairs() -> list[tuple[str, str]]:
    return [(r["stage"], r["reason"]) for r in drops._peek_for_tests()]


# ----------------------------------------------------------------------
# Ledger core
# ----------------------------------------------------------------------


def test_record_drop_buffers_a_bounded_row_and_ticks_the_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import metrics as m

    ticks: list[dict[str, Any]] = []
    monkeypatch.setattr(
        m,
        "record_memory_drop",
        lambda **kw: ticks.append(kw),
    )
    drops.record_drop("o", "n", "extract.judge", "wrong", item="k" * 1000, n=2)
    rows = drops._peek_for_tests()
    assert len(rows) == 1
    row = rows[0]
    assert (row["org"], row["namespace"], row["stage"], row["reason"], row["n"]) == (
        "o",
        "n",
        "extract.judge",
        "wrong",
        2,
    )
    assert len(row["item"]) == drops._ITEM_MAX_LEN
    assert row["ts"] is not None
    assert ticks == [{"namespace": "n", "stage": "extract.judge", "reason": "wrong", "n": 2}]


def test_record_drop_ignores_non_positive_n() -> None:
    drops.record_drop("o", "n", "s", "r", n=0)
    assert drops.pending_count() == 0


def test_record_drop_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import metrics as m

    def _boom(**kw):
        raise RuntimeError("metrics exploded")

    monkeypatch.setattr(m, "record_memory_drop", _boom)
    drops.record_drop("o", "n", "s", "r")  # metric failure is swallowed
    assert drops.pending_count() == 1


@pytest.mark.asyncio
async def test_flush_writes_batch_to_memory_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.core.ch as chmod

    inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    class _CH:
        async def insert(self, table, rows, settings=None):
            inserts.append((table, rows, settings or {}))

    monkeypatch.setattr(chmod, "get_client", lambda: _CH())
    drops.record_drop("o", "n", "write", "key_collision", item="k1")
    drops.record_drop("o", "n", "write", "key_collision", item="k2")
    written = await drops.flush_drops()
    assert written == 2
    assert drops.pending_count() == 0
    table, rows, settings = inserts[0]
    assert table == "loom.memory_drops"
    assert [r["item"] for r in rows] == ["k1", "k2"]
    assert set(rows[0]) == {"org", "namespace", "stage", "reason", "item", "n", "ts"}
    assert settings.get("async_insert") == 1


@pytest.mark.asyncio
async def test_flush_failure_is_fail_open_and_rebuffers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import server.core.ch as chmod

    class _Down:
        async def insert(self, table, rows, settings=None):
            raise ConnectionError("CH unreachable")

    monkeypatch.setattr(chmod, "get_client", lambda: _Down())
    drops.record_drop("o", "n", "s", "r")
    with caplog.at_level(logging.DEBUG, logger="server.memory.app.drops"):
        written = await drops.flush_drops()
    assert written == 0
    assert drops.pending_count() == 1  # kept for the next flush
    assert any("re-buffering" in rec.getMessage() for rec in caplog.records)
    assert not [rec for rec in caplog.records if rec.levelno > logging.DEBUG]


def test_buffer_is_capped() -> None:
    for i in range(drops._BUFFER_CAP + 5):
        drops.record_drop("o", "n", "s", "r", item=str(i))
    assert drops.pending_count() == drops._BUFFER_CAP
    # Oldest rows were the ones discarded.
    assert drops._peek_for_tests()[0]["item"] == "5"


# ----------------------------------------------------------------------
# Wiring — one assertion per discard point
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_records_parse_and_judge_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.extract as mx
    from server.llm.client import LLMResult
    from server.memory.app.pipeline import _step_llm_extract
    from server.memory.app.pipeline_state import WorkflowState

    async def _fake(*args, **kwargs):
        return mx.ExtractionResult(
            facts=[],
            llm_result=LLMResult(text="", tokens_in=1, tokens_out=1, provider="p", model="m"),
            dropped_parse=2,
            dropped_judge_wrong=1,
        )

    monkeypatch.setattr(mx, "extract_from_messages", _fake)
    await _step_llm_extract(
        WorkflowState(
            org="o", namespace="n", agent="a", messages=[{"role": "user", "content": "x"}]
        ),
        {},
    )
    rows = {(r["stage"], r["reason"]): r["n"] for r in drops._peek_for_tests()}
    assert rows == {("extract.parse", "discarded"): 2, ("extract.judge", "wrong"): 1}


@pytest.mark.asyncio
async def test_pipeline_records_a_budget_miss_once_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A response that did not fit the output budget is one ledger row,
    stage extract.truncate, reason retried|salvaged — the number the
    output-capacity router is sized on. A fitting call writes nothing."""
    import server.memory.app.extract as mx
    from server.llm.client import LLMResult
    from server.memory.app.pipeline import _step_llm_extract
    from server.memory.app.pipeline_state import WorkflowState

    for truncation, expected in (("salvaged", 1), ("retried", 1), ("", 0)):
        drops._reset_for_tests()

        async def _fake(*args, _t=truncation, **kwargs):
            return mx.ExtractionResult(
                facts=[],
                llm_result=LLMResult(text="", tokens_in=1, tokens_out=1, provider="p", model="m"),
                truncation=_t,
            )

        monkeypatch.setattr(mx, "extract_from_messages", _fake)
        await _step_llm_extract(
            WorkflowState(
                org="o", namespace="n", agent="a", messages=[{"role": "user", "content": "x"}]
            ),
            {},
        )
        rows = [(r["stage"], r["reason"], r["n"]) for r in drops._peek_for_tests()]
        assert rows == ([("extract.truncate", truncation, 1)] if expected else []), truncation


@pytest.mark.asyncio
async def test_pipeline_records_key_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import bulk_set as bulk_set_mod
    from server.memory.app.pipeline import _step_bulk_write
    from server.memory.app.pipeline_state import WorkflowState

    async def _bulk(bulk_input):
        class _Out:
            written: list = []
            skipped_blacklisted: list = []

        return _Out()

    monkeypatch.setattr(bulk_set_mod, "handle_memory_bulk_set", _bulk)
    await _step_bulk_write(
        WorkflowState(
            org="o",
            namespace="n",
            agent="a",
            extracted_facts=[
                {"key": "k", "value": "a"},
                {"key": "k", "value": "b"},
            ],
        ),
        {},
    )
    rows = drops._peek_for_tests()
    assert _pairs() == [("write", "key_collision")]
    assert rows[0]["item"] == "k"


@pytest.mark.asyncio
async def test_entity_extractor_records_parse_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.workers import entity_extractor as ee

    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "-",
        "memory_key": "k1",
        "memory_type": "semantic",
        "value": "Alice at ClickHouse",
        "version": 1,
        "content_sha256": "",
    }

    async def _rows(**kw):
        return [row]

    async def _apply(items, **kw):
        return {"memory": len(items), "memory_embeddings": len(items)}

    async def _garbled(v):
        return None

    async def _under_cap(pairs):
        return dict.fromkeys(pairs, False)

    monkeypatch.setattr(memory_repo, "find_unentitied_memories", _rows)
    monkeypatch.setattr(memory_repo, "apply_entities_batch", _apply)
    monkeypatch.setattr(ee, "_extract", _garbled)
    monkeypatch.setattr(ee, "namespaces_over_cap", _under_cap)
    monkeypatch.setattr(ee, "emit_worker_cost_event_for", lambda **kw: None)
    ee._parse_fail_streaks.clear()
    for _ in range(settings.memory_entity_extract_parse_fail_max):
        await ee.run_pass(ee.EntityExtractorConfig())
    pairs = _pairs()
    assert (
        pairs.count(("entity.extract", "parse_failed"))
        == settings.memory_entity_extract_parse_fail_max
    )
    assert pairs.count(("entity.extract", "parse_poisoned")) == 1
    assert all(r["item"] == "k1" for r in drops._peek_for_tests())


@pytest.mark.asyncio
async def test_consolidate_records_failed_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.test_memory_consolidate_handler import (
        _CH,
        _LLM,
        _input,
        _two_row_cluster,
        _wire,
    )

    ch = _CH(_two_row_cluster())
    _wire(monkeypatch, ch, _LLM("not json at all"))
    await handle_consolidate(_input())
    rows = drops._peek_for_tests()
    assert _pairs() == [("consolidate.merge", "failed")]
    assert rows[0]["n"] == 2
    assert set(rows[0]["item"].split(",")) == {"dog_name", "dog_food"}


async def handle_consolidate(inp):
    from server.memory.app.consolidate import handle_memory_consolidate

    return await handle_memory_consolidate(inp)


@pytest.mark.asyncio
async def test_schema_mode_records_stubbed_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.extract.handler as mx
    from server.llm.client import LLMResult
    from server.memory.api.models_bulk import MemorySetFromMessagesInput
    from server.memory.app.extract import ExtractedRecord
    from tests.unit.test_set_from_messages_fail_closed import _stub_projections

    _stub_projections(monkeypatch)

    async def _fake_bulk(bulk_input):
        class _Out:
            written: list = []
            skipped_blacklisted: list = []

        return _Out()

    async def _none(*a, **k):
        return None

    async def _fake_typed(
        msgs, schema, *, max_tokens=1024, observation_date=None, operator_policy=""
    ):
        return (
            ExtractedRecord(
                record={"category": None, "amount": None}, stubbed_fields=["category", "amount"]
            ),
            LLMResult(text="", tokens_in=0, tokens_out=0, provider="p", model="m"),
        )

    monkeypatch.setattr(mx, "handle_memory_bulk_set", _fake_bulk)
    monkeypatch.setattr(mx, "_write_inline_record_projection", _none)
    monkeypatch.setattr("server.memory.app.extract.extract_typed_record_iterative", _fake_typed)
    await mx.handle_memory_set_from_messages(
        MemorySetFromMessagesInput(
            org="o",
            namespace="n",
            agent="a",
            messages=[{"role": "user", "content": "x"}],
            memory_key="rec1",
            record_schema={"category": ["a", "b"], "amount": "number"},
        )
    )
    assert _pairs() == [("records.field", "stubbed"), ("records.field", "stubbed")]
    assert [r["item"] for r in drops._peek_for_tests()] == ["rec1.category", "rec1.amount"]


def test_chunk_split_reports_truncated_tail() -> None:
    from server.memory.app.chunk_projection import (
        _MAX_CHUNKS_PER_CALL,
        chunk_text,
        chunk_text_with_truncation,
    )
    from server.memory.domain.chunk_shape import CHUNK_SIZE

    small = "line of text\n" * 40
    chunks, lost = chunk_text_with_truncation(small)
    assert lost == 0 and chunks == chunk_text(small)

    huge = ("x" * (CHUNK_SIZE - 1) + "\n") * (_MAX_CHUNKS_PER_CALL + 10)
    # The default cap is 512; the tail accounting is exercised at a smaller
    # constant through the explicit cap.
    chunks, lost = chunk_text_with_truncation(huge, max_chunks=_MAX_CHUNKS_PER_CALL)
    assert len(chunks) == _MAX_CHUNKS_PER_CALL
    assert lost > 0
    # The reported tail is exactly what no chunk covers.
    covered = "".join(c for c, _, _ in chunks)
    assert lost >= len(huge.strip()) - len(covered) - _MAX_CHUNKS_PER_CALL * 2


@pytest.mark.asyncio
async def test_write_memory_chunks_records_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.core.ch as chmod
    import server.embedding as embmod
    from server.memory.app.chunk_projection import (
        _MAX_CHUNKS_PER_CALL,
        write_memory_chunks,
    )
    from server.memory.domain.chunk_shape import CHUNK_SIZE
    from server.settings import settings

    # The cap is the setting; pin it to the old constant so the
    # body below still overruns it.
    monkeypatch.setattr(settings, "memory_chunks_max_per_call", _MAX_CHUNKS_PER_CALL, raising=False)

    class _CH:
        async def insert(self, *a, **k):
            return None

        async def query_all(self, *a, **k):
            return []

    class _Emb:
        model_name = "fake"
        dim = 2

        async def embed_many(self, texts):
            return [[0.1, 0.2] for _ in texts]

        async def embed(self, text):
            return [0.1, 0.2]

    monkeypatch.setattr(chmod, "get_client", lambda: _CH())
    monkeypatch.setattr(embmod, "get_embedder", lambda: _Emb())

    class _Input:
        org = "o"
        namespace = "n"
        agent = "a"
        session_id = "s"

    body = ("x" * (CHUNK_SIZE - 1) + "\n") * (_MAX_CHUNKS_PER_CALL + 5)
    await write_memory_chunks(_Input(), [{"role": "user", "content": body}])
    assert _pairs() == [("chunk.split", "truncated")]
    assert drops._peek_for_tests()[0]["item"].startswith("chars=")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drops_summary_sql_and_params() -> None:
    from server.cli.hygiene import _drops_summary

    seen: dict[str, Any] = {}

    class _CH:
        async def query_all(self, sql, params=None):
            seen["sql"] = sql
            seen["params"] = params
            return [
                {
                    "stage": "write",
                    "reason": "key_collision",
                    "total": 3,
                    "rows": 3,
                    "last_seen": "2026-09-11 10:00:00",
                }
            ]

    rows = await _drops_summary(_CH(), namespace="ns", hours=6, org="acme")
    assert rows[0]["total"] == 3
    assert "FROM loom.memory_drops" in seen["sql"]
    assert "GROUP BY stage, reason" in seen["sql"]
    assert "AND org = {org:String}" in seen["sql"]
    assert seen["params"] == {"ns": "ns", "hours": 6, "org": "acme"}

    seen.clear()
    await _drops_summary(_CH(), namespace="ns", hours=24)
    assert "org" not in seen["params"] and "{org:String}" not in seen["sql"]


def test_format_drops_prints_counts_by_stage_reason() -> None:
    from server.cli.hygiene import _format_drops

    out = _format_drops(
        [
            {"stage": "write", "reason": "key_collision", "total": 3, "last_seen": "t1"},
            {"stage": "extract.judge", "reason": "wrong", "total": 1, "last_seen": "t2"},
        ],
        namespace="ns",
        hours=24,
    )
    assert "namespace 'ns'" in out and "4 item(s)" in out
    assert "write" in out and "key_collision" in out and "extract.judge" in out
    assert _format_drops([], namespace="ns", hours=24).startswith("no memory ingest drops")


def test_drops_cli_is_registered_and_runs(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import server.core.ch as chmod
    from server.cli import main

    class _CH:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def query_all(self, sql, params=None):
            assert params["ns"] == "ns" and params["hours"] == 48
            return [
                {
                    "stage": "chunk.split",
                    "reason": "truncated",
                    "total": 2,
                    "rows": 2,
                    "last_seen": "t",
                }
            ]

    monkeypatch.setattr(chmod, "get_client", lambda: _CH())
    rc = main(["drops", "--namespace", "ns", "--hours", "48"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "chunk.split" in out and "truncated" in out


@pytest.mark.asyncio
async def test_reset_stops_an_armed_flush_from_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.core.ch as chmod

    inserts: list[str] = []

    class _CH:
        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            inserts.append(table)

    monkeypatch.setattr(chmod, "get_client", lambda: _CH())
    monkeypatch.setattr(drops, "_flush_later", _real_flush_later)
    monkeypatch.setattr(drops, "_FLUSH_DELAY_S", 0.01)
    drops.record_drop("o", "n", "parse", "empty", item="item")
    drops._reset_for_tests()
    # A row only the first timer could flush: this one arms no timer of its own.
    monkeypatch.setattr(drops, "_flush_later", lambda delay: True)
    drops.record_drop("o", "n", "parse", "empty", item="after")
    await asyncio.sleep(0.05)
    assert inserts == []
    assert drops.pending_count() == 1


@pytest.mark.asyncio
async def test_the_timer_flush_is_tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The timer flush runs in a detached task no request tag reaches: its insert
    carries its own tag, inside a request's tag or not."""
    import server.core.ch as chmod

    tags: list[str] = []

    class _CH:
        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            tags.append(chmod.current_query_tag())

    monkeypatch.setattr(chmod, "get_client", lambda: _CH())
    monkeypatch.setattr(drops, "_flush_later", _real_flush_later)
    monkeypatch.setattr(drops, "_FLUSH_DELAY_S", 0.01)
    with chmod.query_tag("memory.set rid-1"):
        drops.record_drop("o", "n", "parse", "empty", item="item")
    for _ in range(100):
        if tags:
            break
        await asyncio.sleep(0.01)
    assert tags == ["drops.flush"]
