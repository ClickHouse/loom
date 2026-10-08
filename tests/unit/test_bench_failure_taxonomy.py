from bench.failure_taxonomy import (
    classify_answer_failure,
    classify_extraction_failure,
    classify_graph_failure,
)


def test_extraction_failure_taxonomy() -> None:
    assert classify_extraction_failure(matched=True, memory_count=0) == "passed"
    assert classify_extraction_failure(matched=False, memory_count=0) == "extraction"
    assert classify_extraction_failure(matched=False, memory_count=3) == "retrieval_ranking"


def test_graph_failure_taxonomy() -> None:
    assert classify_graph_failure(matched=True, observed_count=0) == "passed"
    assert classify_graph_failure(matched=False, observed_count=0) == "extraction"
    assert classify_graph_failure(matched=False, observed_count=3) == "graph_resolution"


def test_answer_failure_taxonomy_orders_the_stages() -> None:
    f = classify_answer_failure
    assert (
        f(judged_correct=True, recalled=False, gold_in_excerpt=False, gold_in_evidence=False)
        == "passed"
    )
    assert (
        f(judged_correct=False, recalled=False, gold_in_excerpt=True, gold_in_evidence=True)
        == "retrieval_ranking"
    )
    assert (
        f(judged_correct=False, recalled=True, gold_in_excerpt=True, gold_in_evidence=True)
        == "reader"
    )
    # Over-compression: the turn had it, the excerpt did not.
    assert (
        f(judged_correct=False, recalled=True, gold_in_excerpt=False, gold_in_evidence=True)
        == "compression_loss"
    )
    assert (
        f(judged_correct=False, recalled=True, gold_in_excerpt=False, gold_in_evidence=False)
        == "not_verbatim"
    )
