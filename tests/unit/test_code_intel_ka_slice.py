"""Keyword-agnostic slice of the mined lanes: the rule is
`code_tokens(query)` shares no lexeme with any gold chunk's lexemes; the
fixture is what the rule produces on an index."""

from __future__ import annotations

import json
from pathlib import Path

from bench.code_intel_ka_slice import (
    derive_slice,
    gold_prefixes,
    load_rows,
    shared_lexemes,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "bench" / "fixtures"


def _row(qid: str, query: str, gold: list[str], lane: str = "issue") -> dict:
    return {
        "id": qid,
        "type": "conceptual",
        "query": query,
        "gold": {"paths": gold},
        "lane": lane,
        "source": {"pr_number": 1, "issue": 2, "merged_at": "2026-01-01 00:00:00"},
    }


def test_shared_lexemes_uses_the_code_tokenizer_over_the_gold_union() -> None:
    lex = {
        "src/Functions/transform": {"transform", "nullable", "enum", "column"},
        "src/Core/Settings": {"settings", "max_threads"},
    }
    # subtokens of a camel-case identifier count: caseWithExpression -> case, with, expression
    assert shared_lexemes(
        "caseWithExpression returns NULL for Nullable(Enum)", ["src/Functions/transform"], lex
    ) == {"nullable", "enum"}
    # the union spans every gold prefix; a trailing slash is tolerated
    assert shared_lexemes(
        "max_threads wrong", ["src/Functions/transform", "src/Core/Settings/"], lex
    ) == {"max_threads"}
    # an unknown prefix contributes nothing
    assert shared_lexemes("anything", ["src/Nowhere"], lex) == set()
    # the df variant: an ignored lexeme is not overlap
    assert (
        shared_lexemes(
            "nullable enum", ["src/Functions/transform"], lex, ignore={"nullable", "enum"}
        )
        == set()
    )


def test_derive_slice_keeps_only_zero_overlap_rows_and_counts_the_histogram() -> None:
    lex = {"src/A": {"alpha", "beta", "gamma", "delta", "eps"}, "src/B": {"zeta"}}
    rows = [
        _row("q0", "Segmentation fault (STID: 3440-3393)", ["src/A"]),  # 0 shared
        _row("q1", "alpha is wrong", ["src/A"]),  # 1
        _row("q2", "alpha beta gamma", ["src/A"]),  # 3
        _row("q3", "alpha beta gamma delta eps", ["src/A"]),  # 5
        _row("q4", "zeta crash", ["src/A", "src/B"]),  # 1 via the second gold
        _row("q5", "nothing indexed here", ["src/Missing"]),  # no indexed gold: not KA
    ]
    kept, stats = derive_slice(rows, lex)
    assert [r["id"] for r in kept] == ["q0"] and kept[0] == rows[0]
    assert stats == {
        "rows": 6,
        "no_indexed_gold": 1,
        "ka": 1,
        "shared_1": 2,
        "shared_2_3": 1,
        "shared_4_plus": 1,
    }
    assert gold_prefixes(rows) == ["src/A", "src/B", "src/Missing"]


def test_shipped_slice_is_a_subset_of_the_parent_lanes_and_under_the_stop_rule() -> None:
    slice_path = _FIXTURES / "code_intel_mined_issue_ka.jsonl"
    rows = load_rows(slice_path)
    header = [line for line in slice_path.read_text().splitlines() if line.startswith("// {")]
    stats = json.loads(header[-1][3:])
    parents = {r["id"]: r for r in load_rows(_FIXTURES / "code_intel_mined_issue.jsonl")}
    parents.update({r["id"]: r for r in load_rows(_FIXTURES / "code_intel_mined_pr_title.jsonl")})
    assert rows and len(rows) == stats["total_ka"] == stats["issue"]["ka"] + stats["pr_title"]["ka"]
    for r in rows:
        assert r == parents[r["id"]], r["id"]
        assert r["type"] == "conceptual" and r["lane"] in {"issue", "pr_title"}
    # Under 60 questions the plane has no measurable
    # keyword-agnostic demand; the graph leg stays parked on this number
    assert len(rows) < 60
    assert stats["rule"] == {"ignore_df_above": 0.0}
