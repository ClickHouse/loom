"""code.search confidence: the vector leg's best cosine on the embedder's calibrated
scale, and the risk bands on it."""

from __future__ import annotations

import pytest

from server.code.domain.index import search_confidence as sc
from server.settings import settings


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


@pytest.mark.parametrize("confident", [0.5, 0.46])
def test_a_scale_with_no_range_reads_the_middle_at_every_cosine(confident: float) -> None:
    # the confident cosine equal to the abstain threshold, then under it: no cosine a
    # served result can have says anything, so none reads 1.0
    scale = dict(abstain_threshold=0.5, confident_similarity=confident)
    assert not sc.has_range(**scale)
    for cosine in (0.2, 0.46, 0.5, 0.7, 1.0):
        assert sc.similarity_term(cosine, **scale) == sc.NO_COSINE_CONFIDENCE


def test_the_narrowest_scale_with_a_range_is_still_read() -> None:
    scale = dict(abstain_threshold=0.5, confident_similarity=0.51)
    assert sc.has_range(**scale)
    assert sc.similarity_term(0.5, **scale) == 0.0
    assert sc.similarity_term(0.51, **scale) == 1.0


def test_risk_bands() -> None:
    bands = dict(medium_from=0.35, low_from=0.70)
    assert [sc.risk_band(c, **bands) for c in (0.0, 0.349, 0.35, 0.699, 0.7, 1.0)] == [
        "high",
        "high",
        "medium",
        "medium",
        "low",
        "low",
    ]


def test_the_default_bands_on_the_scale() -> None:
    def band(confidence: float) -> str:
        return sc.risk_band(
            confidence,
            medium_from=settings.code_search_medium_risk_from,
            low_from=settings.code_search_low_risk_from,
        )

    # a cosine at the confident hit's is low risk; one at the middle of the range or
    # under it is high risk, and the band never abstains
    assert band(1.0) == "low" and band(0.0) == "high"
    # a search with no cosine is neither
    assert band(sc.NO_COSINE_CONFIDENCE) == "medium"
