"""Test-name-shaped tokens in a query: test_failure records key on a STRING
test name, so the tracker route finds them by these tokens, not by the
numeric identifier path."""

from __future__ import annotations

from server.memory.domain.retrieval_calibration import salient_test_identifiers


def test_salient_test_identifiers_shapes() -> None:
    q = (
        "Why did test_s3_plain_rewritable and "
        "test_merge_tree_s3/test.py::test_s3_no_delete_objects[node] fail, and "
        "what about 05047_distributed_plan_finished_stages_drain or Executor.RemoveTasks?"
    )
    got = salient_test_identifiers(q)
    assert "test_s3_plain_rewritable" in got
    assert any("test.py::test_s3_no_delete_objects" in g for g in got)
    assert "05047_distributed_plan_finished_stages_drain" in got
    assert "Executor.RemoveTasks" in got


def test_salient_test_identifiers_empty_on_plain_prose() -> None:
    assert salient_test_identifiers("how do we make CI deterministic?") == []
