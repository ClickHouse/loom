"""A newer participant row cannot hide disagreeing replicas at an older version."""

from __future__ import annotations

import copy

import pytest

from server.cli import cell_control
from server.cli import cell_update_participants as participants
from tests.unit.test_cell_update_participants import participant

ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))
PROOF = "host|boot||namespace|1|3|||cell-retirement-v1"


@pytest.mark.parametrize("order", ORDERS, ids=lambda order: "".join(map(str, order)))
@pytest.mark.parametrize("difference", ["state", "proof"])
def test_higher_version_does_not_hide_equal_version_conflict(order, difference):
    rows = [participant(1, proof=PROOF, version=2), participant(1, proof=PROOF, version=1)]
    conflict = participant(1, proof=PROOF, version=1)
    if difference == "state":
        conflict["state"] = "retired"
    else:
        conflict["host_proof"] = PROOF.replace("|1|3|", "|2|3|")
    rows.append(conflict)
    with pytest.raises(cell_control.Refused, match="conflicting equal versions"):
        participants.latest([rows[index] for index in order])


@pytest.mark.parametrize("order", ORDERS, ids=lambda order: "".join(map(str, order)))
def test_matching_old_duplicates_preserve_actual_newest_row_and_input(order):
    older = participant(1, proof=PROOF, version=1)
    newer = participant(1, "drained", proof=PROOF, version=2)
    rows = [newer, older, copy.deepcopy(older)]
    ordered = [rows[index] for index in order]
    before = copy.deepcopy(ordered)
    assert participants.latest(ordered) == {newer["participant_id"]: newer}
    assert ordered == before
