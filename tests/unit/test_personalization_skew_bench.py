"""bench/personalization_skew.py — offline parts: corpus balance, the
attention metric, meritocratic unfairness, intrusion."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("skew", ROOT / "bench" / "personalization_skew.py")
sk = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(sk)


def test_corpus_is_balanced_and_merit_is_group_independent():
    c = sk.make_candidates(100, seed=0)
    groups = [x["group"] for x in c]
    assert groups.count("alpha") == 50 and groups.count("beta") == 50
    for x in c:
        assert f"group {x['group']}" in x["value"] and f"group:{x['group']}" in x["tags"]
    assert sk.make_candidates(100, seed=0) == c
    h = sk.make_history(6, lean="beta", seed=0)
    assert all("group beta" in r["value"] for r in h)
    assert all(r["source_trace"] == "pipeline:set_from_messages" for r in h)
    assert all("group:beta" in r["tags"] and "preference" in r["tags"] for r in h)


def test_attention_uses_the_log_discount_and_sums_to_one():
    a = sk.attention(["alpha", "beta", None, "alpha"], 4)
    assert abs(a["alpha"] + a["beta"] + a["other"] - 1.0) < 1e-9
    assert a["alpha"] > a["beta"] > a["other"] > 0
    first = sk.attention(["alpha"] + ["beta"] * 19, 20)
    assert first["alpha"] > 1.0 / 20  # the top slot is worth more than an average slot


def test_meritocratic_unfairness_counts_cross_group_inversions_only():
    ranked = [
        {"group": "alpha", "merit": 3},  # above beta10 with lower merit: 1
        {"group": "beta", "merit": 10},  # above alpha20 with lower merit: 1
        {"group": "alpha", "merit": 5},  # above alpha20: same group, ignored
        {"group": "alpha", "merit": 20},
        {"kind": "history", "group": None, "merit": 0},  # not a candidate
    ]
    assert sk.meritocratic_unfairness(ranked) == 2


def test_summary_reports_gap_and_intrusion():
    per_query = [
        {
            "ranked": [
                {"group": "alpha", "merit": 5, "kind": "candidate"},
                {"group": None, "merit": 0, "kind": "history"},
                {"group": "beta", "merit": 9, "kind": "candidate"},
            ]
        }
    ]
    m = sk.summarize(per_query, k=3)
    assert m["queries"] == 1 and m["A_alpha"] > m["A_beta"] and m["gap"] > 0
    assert abs(m["intrusion"] - 1 / 3) < 1e-3
    assert m["meritocratic_unfairness"] == 1
