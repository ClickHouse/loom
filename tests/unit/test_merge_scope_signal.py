"""A merge names the scope it folded into, so the write path can count
cross-run merges as drops."""

from server.memory.domain import conflicts as conflict_rules


def _peer(run_id: str) -> conflict_rules.ConflictPeer:
    return conflict_rules.ConflictPeer(
        memory_id="peer-1",
        memory_key="k",
        similarity=0.95,
        pinned=False,
        canonical="x",
        value="x",
        run_id=run_id,
    )


def test_merge_scope_names_the_three_cases() -> None:
    assert conflict_rules.merge_scope("-", "rA") == "unscoped"
    assert conflict_rules.merge_scope("", "-") == "unscoped"
    assert conflict_rules.merge_scope("rA", "rA") == "same_run"
    assert conflict_rules.merge_scope("rA", "rB") == "cross_run"
    assert conflict_rules.merge_scope("rA", "-") == "cross_run"


def test_merge_results_carry_their_scope_and_duplicates_carry_none() -> None:
    same = conflict_rules.merge_or_run_duplicate(_peer("rA"), [_peer("rA")], new_run_id="rA")
    assert same.verdict == "merge" and same.merge_scope == "same_run"
    unscoped = conflict_rules.merge_or_run_duplicate(_peer("rA"), [_peer("rA")], new_run_id="-")
    assert unscoped.verdict == "merge" and unscoped.merge_scope == "unscoped"
    dup = conflict_rules.merge_or_run_duplicate(_peer("rA"), [_peer("rA")], new_run_id="rB")
    assert dup.verdict == "duplicate" and dup.merge_scope == ""
