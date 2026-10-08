"""Pure-function tests for the nested key-value bench — no server, no spend."""

from __future__ import annotations

from bench.nested_kv import (
    build_task,
    contains_final,
    follow_chain,
    instruction,
    summarize,
    value_of,
)


def test_build_task_chains_exactly_level_hops_and_is_deterministic() -> None:
    for level in range(5):
        t = build_task(pairs=20, level=level, seed=7)
        assert len(t["rows"]) == 20 and len(t["chain"]) == level + 1
        kv = dict(t["rows"])
        key = t["start_key"]
        hops = 0
        while key in kv:
            key = kv[key]
            hops += 1
        assert hops == level + 1 and key == t["final_value"]
        assert t["final_value"] not in kv  # the final value is not a key
        # Every uuid distinct except the chain links, which are both a value and a key.
        assert len(set(kv) | set(kv.values())) == 40 - level
    assert build_task(pairs=20, level=2, seed=7) == build_task(pairs=20, level=2, seed=7)
    assert (
        build_task(pairs=20, level=2, seed=7)["rows"]
        != build_task(pairs=20, level=2, seed=8)["rows"]
    )


def test_value_of_reads_the_pair_case_insensitively_and_only_for_that_key() -> None:
    k, v = "0f0e0d0c-0000-4000-8000-000000000001", "0f0e0d0c-0000-4000-8000-000000000002"
    assert value_of(f"Key: {k}, Value: {v}", k) == v
    assert value_of(f"KEY: {k.upper()},  VALUE: {v.upper()}", k) == v
    assert value_of(f"Key: {v}, Value: {k}", k) is None
    assert value_of("nothing here", k) is None


def test_follow_chain_stops_when_the_value_is_not_a_key() -> None:
    t = build_task(pairs=12, level=3, seed=3)
    kv = dict(t["rows"])
    texts = [f"Key: {k}, Value: {v}" for k, v in t["rows"]]

    def lookup(key: str) -> list[str]:
        return [s for s in texts if key in s.split(",")[0]]

    final, lookups = follow_chain(lookup, t["start_key"])
    assert final == t["final_value"]
    assert lookups == 2 * (t["level"] + 1)  # one lookup per hop plus the "is it a key?" probe
    assert follow_chain(lambda k: [], t["start_key"]) == (None, 1)
    assert contains_final([f"answer: {t['final_value'].upper()}"], t["final_value"])
    assert t["start_key"] in instruction(t["start_key"]) and kv[t["start_key"]] != t["start_key"]


def test_summarize_reports_per_level_accuracy_and_lookups() -> None:
    rows = [
        {
            "level": 0,
            "seed": 0,
            "write_ms": 10,
            "ask_correct": 1,
            "ask_ms": 5,
            "chain_correct": 1,
            "chain_lookups": 2,
            "chain_ms": 3,
        },
        {
            "level": 2,
            "seed": 1,
            "write_ms": 20,
            "ask_correct": 0,
            "ask_ms": 7,
            "chain_correct": 1,
            "chain_lookups": 6,
            "chain_ms": 9,
        },
    ]
    s = summarize(rows, ["ask", "chain"])
    assert s["by_level"]["0"]["ask"] == 1.0 and s["by_level"]["2"]["ask"] == 0.0
    assert s["by_level"]["2"]["chain_lookups_mean"] == 6.0
    assert s["ask"] == 0.5 and s["chain"] == 1.0 and s["write_ms_p50"] == 20


def test_backoff_outlasts_a_table_restart_and_honours_retry_after() -> None:
    from bench.nested_kv import BACKOFF_CAP_S, MAX_ATTEMPTS, backoff_seconds

    total_5xx = sum(backoff_seconds(a, 500) for a in range(MAX_ATTEMPTS))
    assert 150 <= total_5xx <= 240  # about three minutes, the length of a rolling restart
    assert backoff_seconds(7, 500) == BACKOFF_CAP_S
    assert backoff_seconds(0, 429) == 1.0 and backoff_seconds(0, 429, retry_after=7.5) == 7.5
    assert backoff_seconds(2, 500, retry_after=99.0) == 4.0  # Retry-After is a 429 header
