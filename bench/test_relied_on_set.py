"""CH-free unit tests for bench/relied_on_set.py's pure core.

`pytest bench/test_relied_on_set.py` (bench/ is outside the default testpaths).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.relied_on_set import (
    NamespaceSet,
    dedupe_pairs,
    pair_traces,
    propensity_by_rank,
    to_dataset,
)

T0 = datetime(2026, 9, 12, 9, 0, 0)


def _trace(q: str, keys: list[str], dt: int = 0, agent: str = "ask", shape: str = "s1") -> dict:
    return {
        "query": q,
        "returned_keys": keys,
        "ts": T0 + timedelta(seconds=dt),
        "agent": agent,
        "shape": shape,
    }


def _used(key: str, dt: int) -> dict:
    return {"memory_key": key, "ts": T0 + timedelta(seconds=dt)}


def test_a_trace_becomes_a_pair_when_a_returned_key_is_relied_on_inside_the_window() -> None:
    traces = [_trace("who owns the nightly build?", ["k1", "k2", "k3"])]
    used = [_used("k2", 5), _used("k9", 6)]  # k9 was not returned: not this retrieval's label
    pairs = pair_traces(traces, used, window_seconds=120)
    assert len(pairs) == 1
    assert pairs[0].relied_keys == ["k2"] and pairs[0].returned_keys == ["k1", "k2", "k3"]


def test_events_outside_the_window_or_before_the_trace_do_not_label_it() -> None:
    traces = [_trace("q", ["k1"])]
    assert pair_traces(traces, [_used("k1", -1)], window_seconds=120) == []
    assert pair_traces(traces, [_used("k1", 121)], window_seconds=120) == []
    assert len(pair_traces(traces, [_used("k1", 120)], window_seconds=120)) == 1


def test_blank_queries_and_empty_results_never_become_questions() -> None:
    traces = [_trace("", ["k1"]), _trace("q", [])]
    assert pair_traces(traces, [_used("k1", 1)]) == []


def test_min_relied_and_returned_order_are_kept() -> None:
    traces = [_trace("q", ["a", "b", "c"])]
    used = [_used("c", 1), _used("a", 2)]
    (p,) = pair_traces(traces, used, min_relied=2)
    assert p.relied_keys == ["a", "c"]  # returned order, not event order
    assert pair_traces(traces, used, min_relied=3) == []


def test_dedupe_unions_gold_across_repeats_of_the_same_query() -> None:
    pairs = pair_traces(
        [_trace("q", ["a", "b"], 0), _trace("q", ["a", "b"], 300)],
        [_used("a", 1), _used("b", 301)],
    )
    assert len(pairs) == 2
    (d,) = dedupe_pairs(pairs)
    assert d.relied_keys == ["a", "b"] and d.ts == T0


def test_dataset_has_the_retrieval_recall_shape_and_provenance() -> None:
    pairs = pair_traces(
        [_trace("q1", ["a", "b"]), _trace("q2", ["c"])],
        [_used("a", 1), _used("c", 2)],
    )
    ds = to_dataset(
        NamespaceSet("o", "ns", pairs),
        instrument={"embedding_models": ["e"]},
        window_seconds=120,
        days=7,
    )
    assert ds["org"] == "o" and ds["namespace"] == "ns"
    assert [q["gold_keys"] for q in ds["questions"]] == [["a"], ["c"]]
    assert all(set(q) >= {"query", "gold_keys", "note"} for q in ds["questions"])
    assert ds["_built"]["window_seconds"] == 120 and ds["_built"]["embedding_models"] == ["e"]
    assert "lower bound" in ds["_caveat"]
    assert ds["_label_shapes"] == ["s1"] and all(q["shape"] == "s1" for q in ds["questions"])


def test_a_pair_remembers_the_rank_each_relied_key_was_shown_at() -> None:
    (p,) = pair_traces([_trace("q", ["a", "b", "c"])], [_used("c", 1), _used("a", 2)])
    assert p.shown_ranks == {"a": 1, "c": 3}
    # dedupe keeps the BEST rank a key was ever shown at and unions the rest
    pairs = pair_traces(
        [_trace("q", ["x", "a"], 0), _trace("q", ["a", "b"], 300)],
        [_used("a", 1), _used("b", 301), _used("a", 302)],
    )
    (d,) = dedupe_pairs(pairs)
    assert d.shown_ranks == {"a": 1, "b": 2}  # "a": rank 2 in the first trace, 1 in the second


def test_propensity_curve_counts_every_labelled_trace_once() -> None:
    pairs = pair_traces(
        [_trace("q1", ["a", "b"], 0), _trace("q2", ["c", "d"], 10), _trace("q3", ["e", "f"], 20)],
        [_used("a", 1), _used("c", 11), _used("e", 21), _used("f", 22)],
    )
    # rank 1 relied on in 3/3 traces, rank 2 in 1/3
    assert propensity_by_rank(pairs) == {"1": 1.0, "2": 0.3333}
    ds = to_dataset(
        NamespaceSet("o", "n", dedupe_pairs(pairs)),
        instrument={},
        window_seconds=120,
        days=1,
        propensity=propensity_by_rank(pairs),
    )
    assert ds["_propensity_by_rank"] == {"1": 1.0, "2": 0.3333}
    q3 = next(q for q in ds["questions"] if q["query"] == "q3")
    assert q3["gold_shown_ranks"] == {"e": 1, "f": 2}


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
