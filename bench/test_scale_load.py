"""Pure-function tests for the scale harness — no server, no store, no spend."""

from __future__ import annotations

from typing import Any

import httpx

from bench.scale_load import _error_code, _outcomes, _pct, _summary, op_reads, tenant_sql


def test_the_single_shape_puts_every_key_in_one_namespace() -> None:
    assert tenant_sql("single", 10, 2) == ("'scale-one'", "'big'")


def test_the_zipf_shape_names_orgs_and_namespaces_by_tenant_rank() -> None:
    org, ns = tenant_sql("zipf", 10000, 1000)
    # 10 namespaces per org; the rank is floor((T + 1)^u) - 1 for u in [0, 1): 0 to T - 1
    assert "intDiv(" in org and ", 10))" in org and "% 10" in ns
    assert "pow(10001, (n * 0.6180339887498949) % 1)" in org


def test_the_zipf_ranks_reach_every_tenant() -> None:
    # The same arithmetic as the SQL, in Python: every rank from 0 to T - 1 is drawn.
    import math

    t = 10
    ranks = {math.floor(pow(t + 1, (n * 0.6180339887498949) % 1)) - 1 for n in range(5000)}
    assert ranks == set(range(t))


def test_a_percentile_is_nearest_rank() -> None:
    assert _pct([], 0.5) == 0.0
    assert _pct([5.0, 1.0, 3.0], 0.5) == 3.0
    assert _pct([float(i) for i in range(1, 101)], 0.99) == 99.0
    assert _summary([1.0, 2.0], {"503:X": 1}) == {
        "n": 2,
        "p50_ms": 1.0,
        "p99_ms": 2.0,
        "mean_ms": 1.5,
        "errors": {"503:X": 1},
    }


def test_a_writes_outcome_is_read_from_its_answer() -> None:
    assert _outcomes("/v1/memory.set", {"noop": True}) == ["noop"]
    assert _outcomes("/v1/memory.set", {"duplicate_of": "m1"}) == ["duplicate"]
    assert _outcomes("/v1/memory.set", {"status": "active"}) == ["written"]
    # a skipped entry (a pinned or locked key) is in `written` with version 0
    written = [{"version": 1}, {"version": 2}, {"version": 0}]
    bulk = {"written": written, "duplicates": ["k1"], "merged": [], "unchecked": 2}
    assert sorted(_outcomes("/v1/memory.bulk_set", bulk)) == [
        "duplicate",
        "unchecked",
        "unchecked",
        "written",
        "written",
    ]
    assert _outcomes("/v1/memory.search", {"results": [{}, {}]}) == ["hits", "hits"]


def test_a_refusals_code_is_its_loom_error_code() -> None:
    body = {"detail": {"code": "ADMISSION_CONTENDED", "message": "busy"}}
    assert _error_code(httpx.Response(503, json=body)) == "ADMISSION_CONTENDED"
    assert _error_code(httpx.Response(502, text="bad gateway")) == ""
    assert _error_code(httpx.Response(500, json={"error": "x"})) == ""


class _Rows:
    def __init__(self, row: list[Any]) -> None:
        self.result_rows = [row]


class _Ch:
    def __init__(self, row: list[Any], flush_row: list[Any] | None = None) -> None:
        self.row = row
        self.flush_row = flush_row

    def query(self, sql: str, parameters: dict[str, Any]) -> _Rows:
        assert parameters["op"] == "memory.set "
        if (
            parameters["flushes"]
            and self.flush_row
            and "({flushes:Bool} AND startsWith(log_comment, 'pg_batch.flush'))" in sql
        ):
            # the publication batch's statements in the window, counted with the op's
            return _Rows([a + b for a, b in zip(self.row, self.flush_row, strict=True)])
        return _Rows(self.row)


def test_op_reads_divides_by_the_calls_sent() -> None:
    # 40 statements, 80 MiB, 3 s of CPU over 20 calls sent, refused ones included
    got = op_reads(_Ch([40, 80 * 2**20, 2**20, 2, 3_000_000]), "memory.set", 0.0, 1.0, 20)
    assert got == {
        "statements_per_call": 2.0,
        "read_mib_per_call": 4.0,
        "max_statement_memory_mib": 1.0,
        "failed_statements": 2,
        "clickhouse_cpu_ms_per_call": 150.0,
    }
    # under the Postgres authority a write also counts the publication batch's statements
    fake = _Ch([2, 0, 0, 0, 1000], flush_row=[3, 0, 0, 0, 4000])
    assert op_reads(fake, "memory.set", 0.0, 1.0, 1)["statements_per_call"] == 2.0
    flushed = op_reads(fake, "memory.set", 0.0, 1.0, 1, flushes=True)
    assert flushed["statements_per_call"] == 5.0 and flushed["clickhouse_cpu_ms_per_call"] == 5.0
    # no call sent: the window's totals, not a division by zero
    assert (
        op_reads(_Ch([0, None, None, 0, None]), "memory.set", 0.0, 1.0, 0)[
            "clickhouse_cpu_ms_per_call"
        ]
        == 0.0
    )


def test_the_open_loop_starts_each_send_on_time_and_counts_those_past_the_cap() -> None:
    import asyncio

    from bench.scale_load import open_loop

    started: list[int] = []

    async def slow(i: int) -> None:
        started.append(i)
        await asyncio.sleep(0.2)  # longer than the run: every send is still in flight

    # 20 sends due over 0.1 s, at most 5 in flight: 5 start, 15 are counted, none waits
    skipped, took = asyncio.run(open_loop(20, 200.0, 5, slow))
    assert started == [0, 1, 2, 3, 4] and skipped == 15 and took < 0.2

    async def fast(i: int) -> None:
        started.append(i)

    started.clear()
    assert asyncio.run(open_loop(10, 1000.0, 5, fast))[0] == 0 and len(started) == 10
