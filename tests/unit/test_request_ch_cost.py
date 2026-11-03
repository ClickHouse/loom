"""One statement answers "why was this ask slow or expensive".

The ClickHouse client reads the driver's response summary after every
statement into the request's work bag and onto the enclosing `ch.*` span,
sends `query_id = "<request_id>:<n>:<nonce>"` per statement, and the ask service
marks its stages and its injection counts into the same bag; `event_args`
flattens the bag onto the ledger row beside `out_stage_ms`.
"""

from __future__ import annotations

import contextvars
from typing import Any

import pytest

import server.core.ch as ch_mod
from server.core import ch_request_cost
from server.core import request_work as rw
from server.observability.event_args import request_work_columns, request_work_stage_columns


class _Result:
    column_names = ("n",)
    result_rows = [(1,)]

    def __init__(self, summary: dict[str, Any] | None) -> None:
        self.summary = summary


class _FakeAsync:
    """Driver stand-in that returns the summary it was given and records the
    settings of every call."""

    def __init__(self, summary: dict[str, Any] | None) -> None:
        self.summary = summary
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        self.calls.append(("query", dict(settings or {})))
        return _Result(self.summary)

    async def command(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        self.calls.append(("command", dict(settings or {})))
        return _Result(self.summary) if self.summary is not None else "ok"

    async def insert(
        self,
        table: str,
        data: Any,
        column_names: Any = None,
        column_type_names: Any = None,
        settings: Any = None,
    ) -> Any:
        self.calls.append(("insert", dict(settings or {})))
        return _Result(self.summary)

    async def close(self) -> None:
        pass


def _client(fake: _FakeAsync) -> ch_mod.CHClient:
    c = ch_mod.CHClient()
    c._client = fake
    return c


@pytest.fixture(autouse=True)
def _guard_off(monkeypatch: pytest.MonkeyPatch) -> Any:
    # This file is about the summary and the ids; the skew guard has its own.
    from server.core import ch_schema_guard

    async def _nothing_pending(**_kw: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(ch_schema_guard, "pending_tables", _nothing_pending)
    ch_mod.describe_cache_clear()
    yield
    ch_mod.set_query_tag("")
    rw._current.set(None)  # a bag bound here must not leak into the next test


_CLOUD_SUMMARY = {
    "read_rows": "40628",
    "read_bytes": "1180905",
    "written_rows": "0",
    "written_bytes": "0",
    "total_rows_to_read": "40628",
    "result_rows": "1",
    "result_bytes": "272",
    "elapsed_ns": "9804846",
    "memory_usage": "19193349",
    "query_id": "abc:1",
}


# ------------------------------------------------------------ summary parsing


def test_summary_parses_the_cloud_header_and_sums_into_the_bag() -> None:
    bag = rw.begin()
    parsed = rw.note_ch_summary(_CLOUD_SUMMARY)
    assert parsed == {
        "read_rows": 40628,
        "read_bytes": 1180905,
        "elapsed_ms": 9.805,
        "query_id": "abc:1",
    }
    rw.note_ch_summary(
        {**_CLOUD_SUMMARY, "read_rows": "2", "read_bytes": "10", "elapsed_ns": "1000000"}
    )
    assert (bag.ch_statements, bag.ch_read_rows, bag.ch_read_bytes) == (2, 40630, 1180915)
    assert bag.ch_elapsed_ms == pytest.approx(10.805, abs=1e-6)


def test_an_empty_or_odd_summary_is_a_null_reading_not_an_error() -> None:
    bag = rw.begin()
    assert rw.note_ch_summary({}) == {}
    assert rw.note_ch_summary(None) == {}
    assert rw.note_ch_summary({"query_id": "only-an-id"}) == {}
    assert rw.note_ch_summary({"read_rows": "not a number", "read_bytes": None}) == {}
    assert bag.ch_statements == 0


def test_no_bound_bag_records_nothing() -> None:
    ctx = contextvars.copy_context()

    def _run() -> Any:
        rw._current.set(None)
        assert rw.note_ch_summary(_CLOUD_SUMMARY)["read_rows"] == 40628  # parsed for the span
        assert rw.next_query_suffix() is None
        rw.note_injected(facts=3)
        rw.note_schema_skipped("x")
        rw.mark_stage("retrieval")
        return rw.current()

    assert ctx.run(_run) is None


async def test_every_statement_kind_reads_its_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAsync(_CLOUD_SUMMARY)
    c = _client(fake)
    bag = rw.begin()
    await c.query_all("SELECT 1")
    await c.execute("ALTER TABLE loom.t UPDATE a = 1 WHERE 1")
    await c.insert("loom.t", [{"a": 1}])
    # query, command, DESCRIBE (cached types), insert — four responses, four summaries.
    assert bag.ch_statements == 4
    assert bag.ch_read_rows == 4 * 40628


async def test_a_command_that_returns_a_scalar_is_not_a_statement_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(None)
    c = _client(fake)
    bag = rw.begin()
    await c.execute("SYSTEM FLUSH LOGS")
    assert bag.ch_statements == 0


def test_summary_lands_on_the_enclosing_span(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _Span:
        def is_recording(self) -> bool:
            return True

        def set_attribute(self, k: str, v: Any) -> None:
            seen[k] = v

    from opentelemetry import trace

    monkeypatch.setattr(trace, "get_current_span", lambda: _Span())
    rw.begin()
    ch_request_cost.note_result_summary(_Result(_CLOUD_SUMMARY))
    assert seen == {
        "ch.read_rows": 40628,
        "ch.read_bytes": 1180905,
        "ch.elapsed_ms": 9.805,
        "ch.query_id": "abc:1",
    }


# ------------------------------------------------------------------ query_id


def test_query_id_is_request_id_n_nonce_and_counts_attempts() -> None:
    bag = rw.begin()
    ch_mod.set_query_tag("memory.ask 123e4567-e89b-12d3-a456-426614174000")
    a = ch_mod.statement_settings("SELECT 1")
    b = ch_mod.statement_settings("SELECT 2")
    assert a["query_id"] == f"123e4567-e89b-12d3-a456-426614174000:1:{bag.ch_nonce}"
    assert b["query_id"] == f"123e4567-e89b-12d3-a456-426614174000:2:{bag.ch_nonce}"
    assert a["log_comment"] == "memory.ask 123e4567-e89b-12d3-a456-426614174000"
    # A caller's own id is kept.
    assert ch_mod.statement_settings("SELECT 3", {"query_id": "mine"})["query_id"] == "mine"


def test_no_query_id_for_a_worker_pass_or_an_unbound_context() -> None:
    rw.begin()
    ch_mod.set_query_tag("worker.embedding")  # one token: no request id
    assert "query_id" not in ch_mod.statement_settings("SELECT 1")
    ch_mod.set_query_tag("memory.get 42")
    ctx = contextvars.copy_context()

    def _unbound() -> dict[str, Any]:
        rw._current.set(None)
        return ch_mod.statement_settings("SELECT 1")

    assert "query_id" not in ctx.run(_unbound)


async def test_each_driver_call_of_a_request_carries_its_own_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAsync(_CLOUD_SUMMARY)
    c = _client(fake)
    bag = rw.begin()
    ch_mod.set_query_tag("memory.search rid-1")
    await c.query_all("SELECT 1")
    await c.insert("loom.t", [{"a": 1}])
    await c.execute("ALTER TABLE loom.t UPDATE a = 1 WHERE 1")
    ids = [s.get("query_id") for _, s in fake.calls]
    # query, describe, insert, command
    assert ids == [f"rid-1:{n}:{bag.ch_nonce}" for n in (1, 2, 3, 4)]
    assert len(set(ids)) == 4


# ---------------------------------------------------------------- the ledger


def test_request_work_columns_flatten_the_bag() -> None:
    bag = rw.begin()
    rw.note_ch_summary(_CLOUD_SUMMARY)
    rw.note_ch_summary({"read_rows": "2", "read_bytes": "8", "elapsed_ns": "500000"})
    rw.note_injected(facts=3, chunks=1)
    rw.note_schema_skipped("memory_reinforcement")
    rw.note_schema_skipped("memory_reinforcement")  # once
    cols = request_work_columns(bag)
    assert cols == {
        "out_ch_statements": "2",
        "out_ch_read_rows": "40630",
        "out_ch_read_bytes": "1180913",
        "out_ch_elapsed_ms": "10.3",
        "out_injected_facts": "3",
        "out_injected_chunks": "1",
        "schema_skipped": "memory_reinforcement",
    }


def test_an_empty_bag_adds_nothing_and_none_is_tolerated() -> None:
    assert request_work_columns(rw.begin()) == {}
    assert request_work_columns(None) == {}
    assert request_work_stage_columns(None) == {}
    assert request_work_stage_columns(rw.begin()) == {}


def test_ask_stage_marks_become_the_stage_columns() -> None:
    bag = rw.begin()
    bag.stage_ms = {
        "retrieval": 812.4,
        "pending": 3.0,
        "injection": 141.2,
        "window": 20.0,
        "synthesis": 2310.5,
        "judge": 690.1,
    }
    cols = request_work_stage_columns(bag)
    assert cols["out_stage_top"] == "synthesis"
    assert cols["out_stage_top_ms"] == "2310.5"
    assert cols["out_stage_ms"].startswith("synthesis=2310,retrieval=812,judge=690,injection=141")
    assert float(cols["out_stage_total_ms"]) >= 0.0


def test_the_transport_writes_the_bag_beside_the_result_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import ledger
    from server.observability.ledger import emit_agent_event

    captured: list[Any] = []

    class _Em:
        def emit(self, e: Any) -> None:
            captured.append(e)

    monkeypatch.setattr(ledger, "get_emitter", lambda: _Em())
    bag = rw.begin()
    rw.mark_stage("retrieval")
    rw.note_ch_summary(_CLOUD_SUMMARY)
    rw.note_injected(facts=2)
    emit_agent_event(
        op_name="memory.ask",
        identity={"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"},
        request_id="rid",
        latency_ms=5,
        success=True,
        error_code="",
        raw_args={"query": "q"},
        result={"answer": "x", "source": "synthesized"},
    )
    args = captured[0].args
    assert args["out_ch_statements"] == "1" and args["out_ch_read_bytes"] == "1180905"
    assert args["out_injected_facts"] == "2" and args["out_injected_chunks"] == "0"
    assert args["out_stage_ms"].startswith("retrieval=") and "out_stage_total_ms" in args
    assert bag is rw.current()


def test_a_search_result_keeps_its_own_partition(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory.search carries `retrieval_work.stage_ms`; the bag's marks (none
    for a search) must not overwrite it."""
    from server.observability import ledger
    from server.observability.ledger import emit_agent_event

    captured: list[Any] = []

    class _Em:
        def emit(self, e: Any) -> None:
            captured.append(e)

    monkeypatch.setattr(ledger, "get_emitter", lambda: _Em())
    rw.begin()
    rw.mark_stage("ask_only")
    emit_agent_event(
        op_name="memory.search",
        identity={"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"},
        request_id="rid",
        latency_ms=5,
        success=True,
        error_code="",
        result={"retrieval_work": {"stage_ms": {"vector_legs": 10.0, "total": 12.0}}},
    )
    assert captured[0].args["out_stage_ms"] == "vector_legs=10"


# -------------------------------------------------------- the ask's own marks


def test_injection_counts_facts_and_chunks_separately() -> None:
    bag = rw.begin()
    rw.note_injected(facts=5, chunks=2)
    rw.note_injected(facts=1)
    assert (bag.injected_facts, bag.injected_chunks) == (6, 2)


def test_the_ask_service_marks_the_named_stages() -> None:
    """The six persisted stage names exist as marks in the ask
    service, in order, and the injection span wraps the identifier reads."""
    from pathlib import Path

    package = Path("server/memory/app/ask")
    # The pool phases mark the first three stages, the cascade the last three.
    src = "".join(
        (package / name).read_text(encoding="utf-8") for name in ("pool.py", "cascade.py")
    )
    order = [
        src.index(f'_rw.mark_stage("{n}")')
        for n in ("retrieval", "pending", "injection", "window", "synthesis", "judge")
    ]
    assert order == sorted(order)
    injection = (package / "identifiers.py").read_text(encoding="utf-8")
    assert '"ch.query.identifier_injection"' in injection
    assert "_rw.note_injected(" in injection
