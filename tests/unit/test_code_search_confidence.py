"""code.search confidence: the vector leg's best cosine on the embedder's calibrated
scale, times how far the lexical leg confirms that unit."""

from __future__ import annotations

import pytest

from server.code.domain.index import search_confidence as sc
from server.settings import settings


def _row(unit: str, path: str) -> dict[str, str]:
    return {"chunk_id": unit, "path": path}


def test_similarity_runs_from_the_middle_of_the_calibrated_range_to_its_top() -> None:
    # abstain threshold 0.64, confident cosine 0.80: the ramp starts at 0.72
    scale = dict(abstain_threshold=0.64, confident_similarity=0.80)
    assert sc.similarity_term(0.70, **scale) == 0.0
    assert sc.similarity_term(0.72, **scale) == 0.0
    assert sc.similarity_term(0.76, **scale) == pytest.approx(0.5)
    assert sc.similarity_term(0.80, **scale) == 1.0
    assert sc.similarity_term(0.95, **scale) == 1.0


def test_one_cosine_reads_differently_on_two_embedders_scales() -> None:
    # 0.55 is a confident hit for a model whose unrelated text scores 0.2 and noise for
    # one whose unrelated text scores 0.6
    assert sc.similarity_term(0.55, abstain_threshold=0.25, confident_similarity=0.50) == 1.0
    assert sc.similarity_term(0.55, abstain_threshold=0.64, confident_similarity=0.80) == 0.0


def test_a_degenerate_scale_is_a_step_at_the_confident_cosine() -> None:
    scale = dict(abstain_threshold=0.5, confident_similarity=0.5)
    assert sc.similarity_term(0.49, **scale) == 0.0
    assert sc.similarity_term(0.5, **scale) == 1.0


def test_agreement_is_the_vector_legs_first_unit_among_the_lexical_rows() -> None:
    vector = [_row("u1", "a.py"), _row("u2", "b.py")]
    assert sc.leg_agreement(vector, [_row("u9", "z.py"), _row("u1", "a.py")]) == 1.0
    # another unit of its file
    assert sc.leg_agreement(vector, [_row("u7", "a.py"), _row("u2", "b.py")]) == 0.5
    # the vector leg's second unit confirms nothing about its first
    assert sc.leg_agreement(vector, [_row("u2", "b.py")]) == 0.0
    assert sc.leg_agreement(vector, []) == 0.0
    assert sc.leg_agreement([], [_row("u1", "a.py")]) == 0.0


def test_confidence_is_the_similarity_scaled_by_the_agreement() -> None:
    assert sc.confidence(1.0, 0.0) == pytest.approx(sc.UNCONFIRMED)
    assert sc.confidence(1.0, 1.0) == pytest.approx(sc.CONFIRMED)
    assert sc.confidence(1.0, sc.AGREEMENT_UNKNOWN) == pytest.approx(0.65)
    assert sc.confidence(0.5, 1.0) == pytest.approx(0.45)
    # no similarity, no confidence, whatever the lexical leg returns
    assert sc.confidence(0.0, 1.0) == 0.0


def test_risk_bands() -> None:
    bands = dict(abstain_below=0.15, medium_from=0.35, low_from=0.60)
    assert [sc.risk_band(c, **bands) for c in (0.0, 0.149, 0.15, 0.349, 0.35, 0.599, 0.6, 1.0)] == [
        "abstain",
        "abstain",
        "high",
        "high",
        "medium",
        "medium",
        "low",
        "low",
    ]


def test_the_default_bands_place_the_formulas_levels() -> None:
    def band(similarity: float, agreement: float) -> str:
        return sc.risk_band(
            sc.confidence(similarity, agreement),
            abstain_below=settings.code_search_abstain_confidence_below,
            medium_from=settings.code_search_medium_risk_from,
            low_from=settings.code_search_low_risk_from,
        )

    # a confident cosine: unconfirmed is medium, confirmed by file or unit is low
    assert [band(1.0, a) for a in (0.0, 0.5, 1.0)] == ["medium", "low", "low"]
    # a cosine where another repository's text sits abstains even when confirmed
    assert band(0.1, 1.0) == "abstain"
    # a search with no cosine reads medium
    assert band(sc.NO_COSINE_CONFIDENCE / sc.CONFIRMED, 1.0) == "medium"
