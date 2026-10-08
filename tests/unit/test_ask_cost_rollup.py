"""bench/ask_cost_rollup.py — dollars per ask outcome, joined to relied-on events."""

from __future__ import annotations

from bench.ask_cost_rollup import ask_sql, rollup, usage_sql


def test_rollup_joins_on_consumer_day_and_derives_unit_costs() -> None:
    asks = [
        {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "day": "2026-09-12",
            "asks": 100,
            "synthesized": 80,
            "abstained": 15,
            "retrieval_only": 5,
            "usd": 12.0,
            "llm_in": 1000,
            "llm_out": 100,
        },
        {
            "org": "o",
            "namespace": "n",
            "agent": "b",
            "day": "2026-09-12",
            "asks": 10,
            "synthesized": 0,
            "abstained": 10,
            "retrieval_only": 0,
            "usd": 0.5,
            "llm_in": 0,
            "llm_out": 0,
        },
    ]
    usage = [
        {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "day": "2026-09-12",
            "used": 40,
            "evidence": 3,
        },
        {"org": "o", "namespace": "m", "agent": "c", "day": "2026-09-12", "used": 7, "evidence": 0},
    ]
    rows = rollup(asks, usage)
    a = next(r for r in rows if r["agent"] == "a")
    assert a["usd_per_synthesized"] == 0.15 and a["usd_per_used"] == 0.3
    assert a["abstain_rate"] == 0.15 and a["used"] == 40 and a["evidence"] == 3
    b = next(r for r in rows if r["agent"] == "b")
    assert (
        b["usd_per_synthesized"] is None and b["usd_per_used"] is None and b["abstain_rate"] == 1.0
    )
    c = next(r for r in rows if r["agent"] == "c")  # usage without asks is kept
    assert c["asks"] == 0 and c["used"] == 7 and c["usd_per_used"] == 0.0
    assert rows[0]["agent"] == "a"  # sorted by spend, largest first


def test_sql_is_read_only_and_scoped() -> None:
    for sql in (ask_sql(7, "acme"), usage_sql(7, "acme")):
        assert sql.lstrip().upper().startswith("SELECT")
        assert "{org:String}" in sql and "INTERVAL 7 DAY" in sql
    assert "{org:String}" not in ask_sql(1, None)
