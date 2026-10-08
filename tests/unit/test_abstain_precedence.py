"""The shared abstain envelope's precedence, as a full decision table.

memory.search and code.search both answer through `finalize_abstain`. The
order of its gates decides which `abstain_code` a caller and the
`loom.search.abstain{reason}` metric see: empty result, then a weak top hit,
then a missing identifier, then the calibrator. Every combination of the four
inputs is listed here, so a reordering fails with the case that moved.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import pytest

from server.memory.domain.retrieval_calibration import RetrievalCalibration, finalize_abstain

THRESHOLD = 0.40


def _calibration(risk: str) -> RetrievalCalibration:
    return RetrievalCalibration(
        confidence=0.1, risk=risk, route_agreement=0.0, explanation="routes disagree"
    )


@dataclass(frozen=True)
class Case:
    top: float | None
    grounding: str | None
    risk: str

    @property
    def expected(self) -> str:
        if self.top is None:
            return "no_match"
        if self.top < THRESHOLD:
            return "below_threshold"
        if self.grounding is not None:
            return "entity_absent"
        if self.risk == "abstain":
            return "low_calibrated_confidence"
        return ""


CASES = [
    Case(top, grounding, risk)
    for top, grounding, risk in product(
        [None, 0.10, THRESHOLD, 0.90],
        [None, "entity_absent:#116016"],
        ["abstain", "low", "high"],
    )
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c.top}-{bool(c.grounding)}-{c.risk}")
def test_each_combination_answers_the_first_gate_that_applies(case: Case) -> None:
    abstain, code, reason = finalize_abstain(
        top_similarity=case.top,
        calibration=_calibration(case.risk),
        abstain_threshold=THRESHOLD,
        grounding_reason=case.grounding,
    )
    assert code == case.expected
    assert abstain is bool(case.expected)
    assert bool(reason) is abstain


def test_the_threshold_itself_answers() -> None:
    abstain, code, _ = finalize_abstain(
        top_similarity=THRESHOLD, calibration=_calibration("low"), abstain_threshold=THRESHOLD
    )
    assert (abstain, code) == (False, "")
    abstain, code, reason = finalize_abstain(
        top_similarity=THRESHOLD - 1e-9,
        calibration=_calibration("low"),
        abstain_threshold=THRESHOLD,
    )
    assert (abstain, code) == (True, "below_threshold")
    assert "0.40" in reason, "the reason names the threshold an operator tunes"


def test_each_reason_carries_its_own_evidence() -> None:
    _, _, grounding = finalize_abstain(
        top_similarity=0.9,
        calibration=_calibration("low"),
        abstain_threshold=THRESHOLD,
        grounding_reason="entity_absent:#116016",
    )
    assert grounding == "entity_absent:#116016"
    _, _, calibrated = finalize_abstain(
        top_similarity=0.9, calibration=_calibration("abstain"), abstain_threshold=THRESHOLD
    )
    assert calibrated.endswith("routes disagree")
    _, _, empty = finalize_abstain(
        top_similarity=None,
        calibration=_calibration("low"),
        abstain_threshold=THRESHOLD,
        no_match_reason="no code matched",
    )
    assert empty == "no code matched"


def test_a_calibrator_without_a_risk_never_abstains() -> None:
    # code.search passes calibrators of its own shape; a missing `risk` reads as "not abstain".
    abstain, code, _ = finalize_abstain(
        top_similarity=0.9, calibration=object(), abstain_threshold=THRESHOLD
    )
    assert (abstain, code) == (False, "")
