"""A refinement never retires a peer whose stated amounts it drops."""

from __future__ import annotations

from datetime import datetime

from server.memory.app.conflict_judge import ELABORATION, Judgement
from server.memory.domain import conflicts as conflict_rules


def test_quantities_and_preservation() -> None:
    old = "placed an online order with Thrive Market last month and spent around $150 on organic products"
    new = "placed an online order with Thrive Market last month and got great deals on organic products"
    assert conflict_rules.quantities(old) == {"150"}
    assert conflict_rules.preserves_quantities(new, old) is False
    assert conflict_rules.preserves_quantities(old + " and 2 bags", old) is True
    assert conflict_rules.quantities(
        "walked 5 miles in 45 minutes, paid 1,200 dollars, 10% off"
    ) == {
        "5",
        "45",
        "1200",
        "10",
    }
    assert conflict_rules.preserves_quantities("no numbers here", "none here either") is True


def _peer(pid: str, value: str, valid_at: datetime | None = None) -> conflict_rules.ConflictPeer:
    return conflict_rules.ConflictPeer(
        memory_id=pid,
        memory_key=f"k_{pid}",
        similarity=0.9,
        pinned=False,
        canonical=value.lower(),
        value=value,
        valid_at=valid_at,
    )


def test_refines_verdict_keeps_the_peer_that_carries_the_amount() -> None:
    old = _peer(
        "a",
        "User ordered from Thrive Market last month and spent around $150.",
        datetime(2023, 4, 26),
    )
    other = _peer("b", "User likes Thrive Market for organic products.", datetime(2023, 4, 26))
    judged = Judgement("refines", {"a": ELABORATION, "b": ELABORATION}, ["a", "b"], ran=True)
    out = conflict_rules._fold_judged(
        judged,
        [old, other],
        new_valid_at=datetime(2023, 4, 29),
        new_value="User ordered from Thrive Market last month and got great deals.",
    )
    assert out.verdict == "refines" and [p.memory_id for p in out.refined_peers] == ["b"]
    # the new row carries the amount: both peers retire as before
    out2 = conflict_rules._fold_judged(
        judged,
        [old, other],
        new_valid_at=datetime(2023, 4, 29),
        new_value="User ordered from Thrive Market last month, spent $150, and got great deals.",
    )
    assert sorted(p.memory_id for p in out2.refined_peers) == ["a", "b"]
    # nothing survives the guard: clear, nothing retired
    out3 = conflict_rules._fold_judged(
        judged, [old], new_valid_at=datetime(2023, 4, 29), new_value="great deals last month"
    )
    assert out3.verdict == "clear" and out3.refined_peers == []
