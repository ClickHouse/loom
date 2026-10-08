"""Outcome-boost + answer-cache gating contracts (levers 9→ranking, ask)."""

from __future__ import annotations

from server.code.app.answers import _split_coverage
from server.code.app.answers_entry import question_id
from server.code.app.search.priors import _BOOST_CAP, outcome_boost_factor


def test_boost_is_capped_both_directions() -> None:
    assert outcome_boost_factor(1e6, 0, 0) == 1 + _BOOST_CAP
    assert outcome_boost_factor(0, 1e6, 0) == 1 - _BOOST_CAP


def test_boost_decays_with_recency() -> None:
    fresh = outcome_boost_factor(5, 0, 0)
    two_weeks = outcome_boost_factor(5, 0, 14)
    stale = outcome_boost_factor(5, 0, 140)
    assert fresh > two_weeks > stale
    assert abs((two_weeks - 1) - (fresh - 1) / 2) < 1e-9  # half-life = 14d
    assert stale < 1.01  # ancient popularity ~vanishes


def test_no_signal_is_neutral() -> None:
    assert outcome_boost_factor(0, 0, 0) == 1.0


def test_negative_signal_never_decays() -> None:
    # grep-fallbacks stay sticky; only positive signal ages out
    assert outcome_boost_factor(0, 3, 999) == outcome_boost_factor(0, 3, 0)


def test_coverage_split() -> None:
    body, grade = _split_coverage("Answer text.\nCOVERAGE: full")
    assert (body, grade) == ("Answer text.", "full")
    body, grade = _split_coverage("Answer.\ncoverage: NONE")
    assert grade == "none"
    # model forgot the marker -> default partial, answer intact
    body, grade = _split_coverage("Just an answer.")
    assert (body, grade) == ("Just an answer.", "partial")
    # unknown grade -> partial
    _, grade = _split_coverage("x\nCOVERAGE: maybe")
    assert grade == "partial"


def test_question_id_normalizes() -> None:
    assert question_id("How  Does X? ") == question_id("how does x?")
