"""Run.complete(recalled_keys) — what the session's Loom tools
served — joins run.get's live `recalled`; it is never a usage event."""

from __future__ import annotations

import inspect

from server.tasks.api.models_run import RunCompleteInput
from server.tasks.app import run as hr


def test_recalled_keys_is_a_distinct_field_from_memories_used() -> None:
    inp = RunCompleteInput(
        org="o",
        namespace="n",
        run_id="cc-s1",
        status="completed",
        recalled_keys=["coffee_type", "where_i_live"],
    )
    assert inp.recalled_keys == ["coffee_type", "where_i_live"] and inp.memories_used == []
    # It rides the metadata, not the usage ledger: the complete handler folds
    # it into `meta` and no usage-event branch reads it.
    src = inspect.getsource(hr.handle_run_complete)
    assert 'meta["recalled_keys"] = _merge_recalled(' in src
    used_block = inspect.getsource(hr._credit_used) + src[: src.index("meta = _parse_metadata")]
    assert "input.recalled_keys" not in used_block, "never credited as used/evidence/harm"
    assert "run_feedback.record_usage(" in used_block, (
        "the reliance path is still the usage-event path"
    )


def test_merge_recalled_is_a_distinct_ordered_union_with_a_cap() -> None:
    assert hr._merge_recalled(None, ["b", "a", "b", " ", None]) == ["b", "a"]
    assert hr._merge_recalled(["a"], ["b", "a"]) == ["a", "b"]
    assert hr._merge_recalled([], []) == []
    assert len(hr._merge_recalled([str(i) for i in range(600)], ["x"], cap=500)) == 500


def test_run_get_unions_the_stashed_keys_with_the_trace_derived_set() -> None:
    src = (
        inspect.getsource(hr.handle_run_get)
        if hasattr(hr, "handle_run_get")
        else inspect.getsource(hr)
    )
    assert 'run.metadata.get("recalled_keys")' in src
    assert "recalled = _merge_recalled(recalled, stashed)" in src
