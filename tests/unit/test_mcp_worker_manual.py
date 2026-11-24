from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from server.knowledge.domain.mcp_worker_manual import (
    ManualPosition,
    ManualSelection,
    manual_demand,
    next_offer,
)
from server.knowledge.domain.mcp_worker_schedule import ScheduleBinding, ScheduleRejected

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_POSITION = ManualPosition(UUID(int=9), 0)
_BINDING = ScheduleBinding("org", "docs", "source", "connection", "worker", 3, 5, 7, 11)
_SELECTION = ManualSelection(_BINDING, "alice", "test-actor", "a" * 64, "copy", ("café", "other"))


def test_current_offer_does_not_rotate_or_change_the_job_identity():
    offer = next_offer(_POSITION, None, _SELECTION, _NOW)
    clock = _NOW + timedelta(seconds=100)
    same = replace(_SELECTION, selected=("other", "café"))
    assert next_offer(offer.position, offer, same, clock) is offer
    demand = manual_demand(offer, offer.position, _SELECTION, now=_NOW)
    assert manual_demand(offer, offer.position, same, now=clock) == demand
    assert demand.due_at == _NOW


@pytest.mark.parametrize(
    "changed",
    [
        replace(_SELECTION, selected=("cafe\u0301", "other")),
        replace(_SELECTION, selected=("café", "other", "unexpected")),
        replace(_SELECTION, destination_id="new-copy"),
        replace(_SELECTION, owner="bob"),
        replace(_SELECTION, binding=replace(_BINDING, worker_generation=12)),
        replace(_SELECTION, binding=replace(_BINDING, source_generation=6)),
    ],
)
def test_changed_bound_selection_does_not_reuse_existing_offer(changed):
    offer = next_offer(_POSITION, None, _SELECTION, _NOW)
    with pytest.raises(ScheduleRejected, match="unavailable"):
        manual_demand(offer, offer.position, changed, now=_NOW)
    with pytest.raises(ScheduleRejected, match="selection changed"):
        next_offer(offer.position, offer, changed, _NOW)


def test_expired_position_never_revives_with_new_offer_or_recreated_source():
    original = next_offer(_POSITION, None, _SELECTION, _NOW)
    with pytest.raises(ScheduleRejected, match="unavailable"):
        manual_demand(original, original.position, _SELECTION, now=original.expires_at)
    replacement = next_offer(original.position, original, _SELECTION, original.expires_at)
    assert replacement.position == ManualPosition(_POSITION.incarnation, 2)
    recreated = next_offer(ManualPosition(UUID(int=10), 0), None, _SELECTION, original.expires_at)
    for offer in (replacement, recreated):
        with pytest.raises(ScheduleRejected, match="unavailable"):
            manual_demand(offer, original.position, _SELECTION, now=offer.issued_at)
