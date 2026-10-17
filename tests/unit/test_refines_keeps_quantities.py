"""A refinement never retires a peer whose stated amounts it drops."""

from __future__ import annotations

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
