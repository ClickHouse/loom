from server.memory.domain.retrieval_calibration import calibrate_retrieval


def test_calibration_abstains_without_results() -> None:
    result = calibrate_retrieval(
        top_similarity=0.0,
        result_count=0,
    )

    assert result.confidence == 0.0
    assert result.risk == "abstain"


def test_calibration_rewards_route_agreement_and_margin() -> None:
    result = calibrate_retrieval(
        top_similarity=0.82,
        second_similarity=0.45,
        result_count=5,
        graph_seeds=2,
        graph_neighbors=4,
        triples_extracted=1,
        triples_matched=1,
        evidence_support_count=3,
    )

    assert result.confidence > 0.75
    assert result.risk == "low"
    assert result.route_agreement == 1.0


def test_the_caller_sets_the_risk_bands() -> None:
    args = dict(top_similarity=0.55, second_similarity=0.5, result_count=5, extra_routes_possible=1)
    memory = calibrate_retrieval(**args)
    code = calibrate_retrieval(**args, medium_risk_from=0.40, low_risk_from=0.55)
    assert memory.confidence == code.confidence
    assert memory.risk == "high"
    assert code.risk == "medium"


def test_the_code_plane_labels_a_typical_right_hit_medium_not_high() -> None:
    from server.settings import settings

    assert settings.code_search_medium_risk_from <= 0.40 < settings.code_search_low_risk_from
