"""What an assistant answer read, as steps (server/assistant/domain/steps.py), and the live read
of a streaming answer's rows (ledger.live_rows, on clickhouse-local against the production
table)."""

from __future__ import annotations

import asyncio
import time

import pytest

from server.assistant.domain.steps import STEP_TEXT, Step, steps_of
from server.assistant.domain.support import RUN_ROWS, CallRow
from server.assistant.store import ledger
from server.observability.consumer import ASSISTANT_CONSUMER
from server.operations.toolsets import MCP_ASSISTANT_OPS
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl


def test_every_assistant_op_has_its_step_text() -> None:
    # An op added to the toolset fails here until it gets words.
    assert set(STEP_TEXT) == MCP_ASSISTANT_OPS


def test_a_step_says_what_was_read_and_a_failed_one_what_could_not_be() -> None:
    steps, cut = steps_of(
        [
            CallRow("namespace.audit", True),
            CallRow("memory.search", False),
        ]
    )
    assert steps == [
        Step("namespace.audit", "Read the audit", True),
        Step("memory.search", "Couldn't search memories", False),
    ]
    assert cut is False


def test_an_unknown_op_and_a_derived_row_are_no_step() -> None:
    steps, _ = steps_of(
        [
            CallRow("memory.ask", True),
            # The consistency check memory.ask records under its own row.
            CallRow("memory.ask.consistency", True),
            CallRow("memory.set", True),
        ]
    )
    assert [s.op for s in steps] == ["memory.ask"]


def test_a_cut_run_shows_its_first_rows_and_says_so() -> None:
    rows = [CallRow("memory.get", True) for i in range(RUN_ROWS + 1)]
    steps, cut = steps_of(rows)
    assert (len(steps), cut) == (RUN_ROWS, True)


def _row(operation: str, *, ago_s: float, at_ms: int | None = None, **kw: object) -> str:
    """One `loom.agent_events` row `ago_s` seconds before now (or at `at_ms`, epoch
    milliseconds), of the caller's answers."""
    cols: dict[str, object] = {
        "org": "acme",
        "namespace": "ns",
        "run_id": "r1",
        "subject": "alice",
        "consumer": ASSISTANT_CONSUMER,
        "operation": operation,
        "success": 1,
        "request_id": operation,
        **kw,
    }
    values = [f"'{v}'" if isinstance(v, str) else str(v) for v in cols.values()]
    at = (
        f"fromUnixTimestamp64Milli({at_ms})"
        if at_ms is not None
        else f"now64(3) - toIntervalMillisecond({int(ago_s * 1000)})"
    )
    return (
        f"INSERT INTO loom.agent_events ({', '.join([*cols, 'ts', 'ingested_at'])}) "
        f"VALUES ({', '.join([*values, at, at])})"
    )


def _live(run_id: str, started: float, *rows: str) -> list[str]:
    ch = LocalCH(ddl("agent_events"), *rows)
    got = asyncio.run(
        ledger.live_rows("acme", run_id, "alice", namespace="ns", started=started, ch=ch)  # type: ignore[arg-type]
    )
    return [r.operation for r in got]


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_before_its_run_is_known_an_answer_reads_its_namespace_from_its_start() -> None:
    started = time.time() - 2
    got = _live(
        "",
        started,
        _row("namespace.audit", ago_s=1),
        # The previous answer's rows, 5 s before this one started.
        _row("memory.search", ago_s=7, run_id="r0"),
        # Under the code namespace: found once the run is known.
        _row("code.search", ago_s=1, namespace="code-loom"),
        # Another caller, another consumer.
        _row("memory.get", ago_s=1, subject="bob"),
        _row("memory.list", ago_s=1, consumer="my-script"),
    )
    assert got == ["namespace.audit"]


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_once_its_run_is_known_an_answer_reads_its_run_in_any_namespace() -> None:
    started = time.time() - 2
    got = _live(
        "r1",
        started,
        _row("namespace.audit", ago_s=1),
        _row("code.search", ago_s=1, namespace="code-loom"),
        _row("memory.search", ago_s=1, namespace="-", success=0),
        _row("memory.get", ago_s=1, run_id="r2"),
    )
    assert sorted(got) == ["code.search", "memory.search", "namespace.audit"]


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_calls_of_one_millisecond_are_read_in_one_order() -> None:
    # Two calls that finished in the same millisecond, inserted in the other order: each
    # read lists them by request, so the steps do not swap from one poll to the next.
    at = int((time.time() - 1) * 1000)
    started = time.time() - 2
    got = _live(
        "r1",
        started,
        _row("memory.search", ago_s=0, at_ms=at, request_id="q2"),
        _row("namespace.audit", ago_s=0, at_ms=at, request_id="q1"),
    )
    assert got == ["namespace.audit", "memory.search"]
