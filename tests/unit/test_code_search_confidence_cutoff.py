"""The low-confidence abstain cut-off is a parameter of the
shared calibration and a code-plane setting; the default keeps the
historical 0.35."""

from __future__ import annotations

from server.memory.domain.retrieval_calibration import calibrate_retrieval


def test_cutoff_is_a_parameter_with_the_historical_default() -> None:
    # a prose question's typical shape: top cosine 0.40, no margin, legs disagreeing
    kw = dict(
        top_similarity=0.40,
        second_similarity=0.40,
        result_count=10,
        abstain_threshold=0.245,
        extra_route_votes=0,
        extra_routes_possible=2,
    )
    c = calibrate_retrieval(**kw)
    assert 0.33 < c.confidence < 0.35 and c.risk == "abstain"
    assert calibrate_retrieval(**kw, confidence_abstain_below=0.30).risk == "high"
    # the similarity threshold still abstains whatever the cut-off
    assert (
        calibrate_retrieval(**{**kw, "top_similarity": 0.20}, confidence_abstain_below=0.30).risk
        == "abstain"
    )


def test_code_search_passes_its_setting_at_every_call_site() -> None:
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[2] / "server/code/app/search"
    src = "\n".join(p.read_text() for p in sorted(pkg.glob("*.py")))
    assert (
        src.count("calibrate_retrieval(")
        == src.count(
            "confidence_abstain_below=float(settings.code_search_abstain_confidence_below)"
        )
        >= 1
    )
