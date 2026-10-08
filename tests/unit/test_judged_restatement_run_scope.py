"""A judged restatement of another run's row is this run's
duplicate (inserted), never a merge (no insert) — the canonical match's run
rule, applied to the judge's verdict too."""

from server.memory.app.conflict_judge import Judgement
from server.memory.domain import conflicts as conflict_rules


def _peer(run_id: str) -> conflict_rules.ConflictPeer:
    return conflict_rules.ConflictPeer(
        memory_id="peer-1",
        memory_key="rA/session_0003",
        similarity=0.93,
        pinned=False,
        canonical="emma plan",
        value="Emma's plan ...",
        run_id=run_id,
    )


def _restated() -> Judgement:
    return Judgement("merge", {"peer-1": "restatement"}, ["peer-1"], ran=True)


def test_restatement_of_another_runs_row_is_this_runs_duplicate() -> None:
    out = conflict_rules._fold_judged(_restated(), [_peer("rA")], new_run_id="rB")
    assert out.verdict == "duplicate"
    assert out.duplicate_of_id == "peer-1"
    assert out.existing_memory_id is None


def test_restatement_of_an_unscoped_row_under_a_run_is_a_duplicate_too() -> None:
    out = conflict_rules._fold_judged(_restated(), [_peer("-")], new_run_id="rB")
    assert out.verdict == "duplicate" and out.duplicate_of_id == "peer-1"


def test_same_run_and_unscoped_writes_still_merge() -> None:
    same = conflict_rules._fold_judged(_restated(), [_peer("rB")], new_run_id="rB")
    assert same.verdict == "merge" and same.existing_memory_id == "peer-1"
    unscoped = conflict_rules._fold_judged(_restated(), [_peer("rA")], new_run_id="-")
    assert unscoped.verdict == "merge" and unscoped.existing_memory_id == "peer-1"
