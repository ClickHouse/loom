"""bench/answer_scoring.py — the token F1 / BLEU-1 answer columns and the
simplification step in front of them."""

from __future__ import annotations

import pytest

from bench.answer_scoring import (
    SIMPLIFY_PROMPT,
    bleu1,
    build_simplify_prompt,
    date_match,
    duration_days,
    duration_match,
    gold_date_window,
    paper_tokens,
    parse_date,
    parse_simplified,
    rouge_l,
    simplify_heuristic,
    squad_tokens,
    temporal_kind,
    token_f1,
)

GOLD = "7 May 2023"


@pytest.mark.parametrize(
    ("prediction", "f1", "b1"),
    [
        # Reference values, reproduced to their published precision.
        ("Caroline went to the LGBTQ support group on May 7, 2023.", 0.3750, 0.2308),
        ("May 7, 2023", 0.8571, 0.7500),
        ("8 May 2023", 0.6667, 0.6667),
    ],
)
def test_table_12_is_reproduced(prediction: str, f1: float, b1: float) -> None:
    assert token_f1(prediction, GOLD) == pytest.approx(f1, abs=1e-4)
    assert bleu1(prediction, GOLD) == pytest.approx(b1, abs=1e-4)


def test_the_wrong_answer_outscores_the_verbose_right_one() -> None:
    """The reason these columns never replace the judge (README hygiene note)."""
    verbose_right = "Caroline went to the LGBTQ support group on May 7, 2023."
    wrong = "8 May 2023"
    assert token_f1(wrong, GOLD) > token_f1(verbose_right, GOLD)
    # The heuristic only drops the final period here (no lead-in to remove):
    # 3 shared tokens of 12 -> F1 0.4, still below the wrong answer.
    assert token_f1(simplify_heuristic(verbose_right), GOLD) == pytest.approx(0.4, abs=1e-4)


def test_tokenisers() -> None:
    assert paper_tokens("May 7, 2023.") == ["may", "7", ",", "2023", "."]
    assert squad_tokens("The May 7, 2023.") == ["may", "7", "2023"]
    # SQuAD normalisation drops articles and punctuation, so the same
    # prediction scores higher under it — the variant is opt-in and named.
    assert token_f1("the 7 may 2023.", GOLD, normalize="squad") == 1.0
    assert token_f1("the 7 may 2023.", GOLD) < 1.0


def test_edge_cases() -> None:
    assert token_f1("", "") == 1.0 and bleu1("", "") == 1.0
    assert token_f1("", GOLD) == 0.0 and bleu1(GOLD, "") == 0.0
    assert token_f1("nothing in common", GOLD) == 0.0
    assert bleu1("nothing in common", GOLD) == 0.0
    # Brevity penalty: a one-token correct candidate against a three-token gold.
    assert bleu1("2023", GOLD) == pytest.approx(1.0 * pow(2.718281828, 1 - 3 / 1), rel=1e-6)


def test_simplify_prompt_is_the_paper_figure_12() -> None:
    for rule in (
        "Extract the Core Information",
        "Remove Extraneous Phrases",
        "Omit Explanations",
        "Be Concise",
        "Business Administration",
    ):
        assert rule in SIMPLIFY_PROMPT
    p = build_simplify_prompt("What degree did I graduate with?", "You graduated in Law.")
    assert p.endswith("- Simplified Answer:")
    assert "- Question: What degree did I graduate with?" in p
    assert "- Original Answer: You graduated in Law." in p


def test_parse_simplified_strips_label_and_quotes_and_keeps_fallback() -> None:
    assert parse_simplified('- Simplified Answer: "May 7, 2023"', fallback="x") == "May 7, 2023"
    assert (
        parse_simplified("\n  Business Administration \n", fallback="x")
        == "Business Administration"
    )
    assert parse_simplified("", fallback="the original") == "the original"


def test_heuristic_drops_lead_in_and_keeps_first_sentence() -> None:
    assert (
        simplify_heuristic(
            "Based on the information provided, you moved to Lisbon in 2021. "
            "You also mentioned Porto."
        )
        == "you moved to Lisbon in 2021"
    )
    assert simplify_heuristic("The answer is 7.5 hours. Roughly.") == "7.5 hours"
    assert simplify_heuristic("") == ""


def test_harness_carries_the_columns() -> None:
    import csv
    import io
    from pathlib import Path

    from bench.longmemeval import ItemResult, _write_csv

    r = ItemResult(
        question_id="q",
        question_type="single-session-user",
        question="?",
        gold_answer=GOLD,
        top_k_excerpts=[],
        recalled=True,
        hypothesis="Caroline went on May 7, 2023.",
        f1=0.375,
        bleu1=0.2308,
        simplified_hypothesis="May 7, 2023",
        simplified_by="heuristic",
    )
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "out.csv"
        _write_csv(path, [r])
        rows = list(csv.DictReader(io.StringIO(path.read_text())))
    assert rows[0]["f1"] == "0.375" and rows[0]["bleu1"] == "0.2308"
    assert rows[0]["simplified_hypothesis"] == "May 7, 2023"
    assert rows[0]["simplified_by"] == "heuristic"


@pytest.mark.parametrize(
    ("prediction", "rl_f1", "rl_recall"),
    [
        # The same three reference answers, ROUGE-L by hand: gold tokens [7, may, 2023].
        # The long sentence shares the ordered pair (may, 2023) → LCS 2 of 13 / 3.
        ("Caroline went to the LGBTQ support group on May 7, 2023.", 0.2500, 0.6667),
        ("May 7, 2023", 0.5714, 0.6667),  # [may, 7, ",", 2023]: LCS 2 (order differs from gold)
        ("8 May 2023", 0.6667, 0.6667),
        ("7 May 2023", 1.0, 1.0),
        ("", 0.0, 0.0),
    ],
)
def test_rouge_l_is_lcs_based(prediction: str, rl_f1: float, rl_recall: float) -> None:
    f1, recall = rouge_l(prediction, GOLD)
    assert f1 == pytest.approx(rl_f1, abs=1e-4)
    assert recall == pytest.approx(rl_recall, abs=1e-4)


def test_rouge_l_rewards_order_where_f1_does_not() -> None:
    # Bag-of-tokens F1 cannot tell these apart; ROUGE-L can.
    assert token_f1("2023 may 7", GOLD) == pytest.approx(1.0)
    assert rouge_l("2023 may 7", GOLD)[0] < 1.0


def test_date_match_is_unit_aware_and_gold_precision_bound() -> None:
    assert date_match("September 24, 2025", "2025-09-24") is True
    assert date_match("24 Sep 2025", "September 24, 2025") is True
    assert date_match("May 7, 2023", "7 May 2023") is True
    assert date_match("8 May 2023", "7 May 2023") is False  # an off-by-one day
    assert date_match("2023-06-15", "June 2023") is True  # gold month admits any day
    assert date_match("sometime in 2022", "2022") is True
    assert date_match("I don't know", "7 May 2023") is False
    assert date_match("anything", "her mother") is None  # gold names no date


def test_duration_match_uses_the_paper_epsilon() -> None:
    assert duration_days("two weeks") == 14 and duration_days("a couple of weeks") == 14
    assert duration_match("12 days", "13 days") is True  # ε = 1 day
    assert duration_match("2 weeks", "13 days") is True
    assert duration_match("11 days", "13 days") is False
    assert duration_match("3 months", "four months") is False
    assert duration_match("13 weeks", "3 months") is True  # 91 vs 90 days
    assert duration_match("x", "her mother") is None


def test_temporal_kind() -> None:
    assert [
        temporal_kind(g) for g in ("7 May 2023", "June 2023", "2022", "10 years ago", "her mother")
    ] == [
        "date",
        "date",
        "year",
        "duration",
        "other",
    ]
    assert parse_date("The week before 9 June 2023") == (2023, 6, 9)


def test_relative_golds_resolve_against_their_anchor_not_to_it() -> None:
    from datetime import date

    # LoCoMo writes relative golds; the anchor is the session date the reader echoes.
    assert gold_date_window("The sunday before 25 May 2023") == (
        date(2023, 5, 21),
        date(2023, 5, 21),
    )
    assert gold_date_window("The Friday before 20 May 2023") == (
        date(2023, 5, 19),
        date(2023, 5, 19),
    )
    assert gold_date_window("The week before 9 June 2023") == (date(2023, 6, 2), date(2023, 6, 8))
    assert date_match("2023-05-21", "The sunday before 25 May 2023") is True
    assert date_match("2023-05-25", "The sunday before 25 May 2023") is False  # echoed the anchor
    assert date_match("2023-05-20", "The Friday before 20 May 2023") is False
    assert date_match("June 5, 2023", "The week before 9 June 2023") is True
    assert date_match("7 May 2023", "7 May 2023") is True and gold_date_window("2022") is None


def test_cohen_kappa_and_agreement() -> None:
    from bench.answer_scoring import cohen_kappa, label_agreement

    a = ["CORRECT", "CORRECT", "WRONG", "WRONG", "CORRECT", "WRONG"]
    b = ["CORRECT", "WRONG", "WRONG", "WRONG", "CORRECT", "CORRECT"]
    assert label_agreement(a, b) == 4 / 6
    # po = 4/6, pe = (3/6*3/6) + (3/6*3/6) = 0.5 -> kappa = (0.667-0.5)/0.5 = 0.3333
    assert cohen_kappa(a, b) == 0.3333
    assert cohen_kappa(a, a) == 1.0
    assert cohen_kappa([], []) is None
    assert cohen_kappa(["CORRECT"] * 3, ["CORRECT"] * 3) is None  # chance agreement 1
    assert cohen_kappa(a, b[:-1]) is None
