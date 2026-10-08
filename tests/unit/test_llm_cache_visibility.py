"""The response cache is visible per ask and per readout.

The cache pin rides the args side of the ledger row; what applied and what it
saved ride the result side beside the hit / miss counts, and `cost.summary`
sums the hits and the dollars not spent, so cache use is attributable per ask.
CH-free: fake clients record the statements."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any

from server.insights.api.models_cost import CostSummaryInput
from server.insights.app import cost as cost_h
from server.insights.store import cost as cost_store
from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput
from server.observability.event_args import (
    _MAX_ENTRIES,
    _MAX_RESULT_ENTRIES,
    IDENTIFIER_KEYS,
    IDENTITY_KEYS,
    project_args,
    project_result,
)

# ---- the ledger row ---------------------------------------------------------


def test_the_pin_rides_the_args_side_and_only_when_sent() -> None:
    body = MemoryAskInput(org="o", namespace="n", query="q", llm_cache=False).model_dump(
        exclude_none=True
    )
    out = project_args(body)
    assert out["llm_cache"] == "false"
    assert "llm_cache" not in project_args({"org": "o", "namespace": "n", "query": "q"}), (
        "unset pin = no key; the applied value is on the result side"
    )
    # memory.ask's whole argument surface still fits the args budget.
    keys = [
        k for k in MemoryAskInput.model_fields if k in IDENTIFIER_KEYS and k not in IDENTITY_KEYS
    ]
    assert len(keys) + 2 <= _MAX_ENTRIES  # + `query` digest + `_sha256`


def test_what_applied_and_what_it_served_ride_the_result_side() -> None:
    out = MemoryAskOutput(
        answer="a",
        source="synthesized",
        llm_cache=True,
        llm_cache_hits=3,
        llm_cache_misses=1,
        llm_cache_saved_usd=0.1234,
    )
    projected = project_result(out)
    assert projected["out_llm_cache"] == "true"
    assert projected["out_llm_cache_saved_usd"] == "0.1234"
    # The applied flag and the dollars sit beside the hit / miss counts.
    # Off: the row says so, rather than saying nothing.
    off = project_result(MemoryAskOutput(answer="a", source="synthesized"))
    assert off["out_llm_cache"] == "false" and off["out_llm_cache_saved_usd"] == "0.0"
    keys = [
        k for k in MemoryAskOutput.model_fields if k in IDENTIFIER_KEYS and k not in IDENTITY_KEYS
    ]
    assert len(keys) <= _MAX_RESULT_ENTRIES - 8, "the 3440 headroom rule still holds"
    assert {"out_source", "out_summary_hits", "out_timeline_rows"} <= set(projected)


def test_handler_sums_the_saving_over_the_tiers_and_reads_off_as_zero() -> None:
    from server.memory.app.ask.handler import _cache_count, _cache_sum

    strong = SimpleNamespace(hits=1, misses=1, saved_usd=0.05)
    cheap = SimpleNamespace(hits=2, misses=0, saved_usd=0.001, _peer=strong)
    other = SimpleNamespace(hits=9, misses=9, saved_usd=9.0, _peer=None)  # not this ask's
    assert abs(_cache_sum(strong, cheap, "saved_usd", other) - 0.051) < 1e-9
    assert _cache_count(strong, cheap, "hits", other) == 3
    assert _cache_sum(None, cheap, "saved_usd") == 0.0, "no strong wrapper = the cache was off"
    from server.memory.app.ask.handler import _ask_cache_fields

    fields = _ask_cache_fields(strong, cheap, other)
    assert (
        fields["llm_cache"] is True
        and fields["llm_cache_hits"] == 3
        and fields["llm_cache_misses"] == 1
    )
    assert fields["llm_cache_saved_usd"] == 0.051
    off = _ask_cache_fields(None, cheap)
    assert off == {
        "llm_cache": False,
        "llm_cache_hits": 0,
        "llm_cache_misses": 0,
        "llm_cache_saved_usd": 0.0,
    }
    assert set(fields) <= set(MemoryAskOutput.model_fields)
    assert MemoryAskOutput().llm_cache is False and MemoryAskOutput().llm_cache_saved_usd == 0.0


# ---- cost.summary ---------------------------------------------------------------


class _CH:
    def __init__(
        self, rows: list[dict[str, Any]], cache_rows: list[dict[str, Any]] | None = None
    ) -> None:
        self.rows = rows
        self.cache_rows = cache_rows if cache_rows is not None else []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, dict(params or {})))
        if "cache_hits" in flat and "UNION ALL" not in flat and "count()" not in flat:
            return self.cache_rows
        return self.rows


def _run(monkeypatch, ch: _CH, **kw) -> Any:
    monkeypatch.setattr(cost_h, "get_client", lambda: ch)
    inp = CostSummaryInput(
        org="o", namespace="n", from_date=date(2026, 9, 1), to_date=date(2026, 9, 14), **kw
    )
    return asyncio.run(cost_h.handle_cost_summary(inp))


def test_rollup_path_reads_the_cache_totals_in_one_bounded_statement(monkeypatch) -> None:
    ch = _CH(
        [
            {
                "operation": "memory.ask",
                "cost_usd": 2.0,
                "tokens": 1,
                "embed_tokens": 0,
                "ops": 4,
                "errors": 0,
                "lat_q": [1.0, 2.0, 3.0],
            }
        ],
        cache_rows=[{"cache_hits": 7, "cache_saved_usd": 0.35}],
    )
    out = _run(monkeypatch, ch)
    assert len(ch.calls) == 2
    union_sql, cache_sql = ch.calls[0][0], ch.calls[1][0]
    assert "UNION ALL" in union_sql and "cache_hits" not in union_sql, (
        "the roll-up read is unchanged"
    )
    assert cache_sql.startswith("SELECT " + " ".join(cost_store.CACHE_SELECT.split()))
    assert "FROM loom.agent_events WHERE org = {org:String}" in cache_sql
    assert "toDate(ts) BETWEEN {from_d:Date} AND {to_d:Date}" in cache_sql
    assert "operation = 'memory.ask'" in cache_sql and "namespace = {ns:String}" in cache_sql
    assert "PREWHERE" not in cache_sql
    assert out.cache_hits == 7 and out.cache_saved_usd == 0.35
    assert out.total_cost_usd == 2.0, "spent and not-spent never overlap"


def test_events_scan_path_folds_the_cache_columns_into_its_statement(monkeypatch) -> None:
    ch = _CH(
        [
            {
                "model": "m1",
                "cost_usd": 1.0,
                "tokens": 1,
                "embed_tokens": 0,
                "ops": 2,
                "errors": 0,
                "lat_q": [1.0, 2.0, 3.0],
                "cache_hits": 2,
                "cache_saved_usd": 0.1,
            },
            {
                "model": "m2",
                "cost_usd": 1.0,
                "tokens": 1,
                "embed_tokens": 0,
                "ops": 2,
                "errors": 0,
                "lat_q": [1.0, 2.0, 3.0],
                "cache_hits": 3,
                "cache_saved_usd": 0.2,
            },
        ]
    )
    out = _run(monkeypatch, ch, group_by=["model"])
    assert len(ch.calls) == 1, "the scan path pays nothing extra"
    sql = ch.calls[0][0]
    assert "toUInt64OrZero(args['out_llm_cache_hits'])" in sql and "UNION ALL" not in sql
    assert [r.cache_hits for r in out.rows] == [2, 3]
    assert out.cache_hits == 5 and abs(out.cache_saved_usd - 0.3) < 1e-9


def test_include_cache_off_or_another_operation_reads_nothing_extra(monkeypatch) -> None:
    ch = _CH([])
    out = _run(monkeypatch, ch, include_cache=False)
    assert len(ch.calls) == 1 and "cache_hits" not in ch.calls[0][0]
    assert out.cache_hits == 0 and out.cache_saved_usd == 0.0
    ch = _CH([])
    _run(monkeypatch, ch, operation="memory.set")
    assert len(ch.calls) == 1, "a readout filtered to another op cannot hold ask cache rows"
    ch = _CH([])
    _run(monkeypatch, ch, group_by=["model"], include_cache=False)
    assert "cache_hits" not in ch.calls[0][0]
