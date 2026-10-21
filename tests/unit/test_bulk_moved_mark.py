"""A bulk conflict mark that lands `moved` (a revision since owes its own judge)
leaves the entry unflagged, as memory.set does for any verdict but `conflict`."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
from server.memory.app.bulk_rows import _BulkWriteState
from server.memory.app.bulk_set import _bulk_settle_verdicts
from server.memory.app.bulk_verdicts import BulkVerdictPlan


@pytest.mark.asyncio
@pytest.mark.parametrize("landed", ["clear", "moved"])
async def test_a_mark_that_did_not_land_clears_the_verdict(
    monkeypatch: pytest.MonkeyPatch, landed: str
) -> None:
    async def _mark(**_: Any) -> str:
        return landed

    monkeypatch.setattr("server.memory.app.conflicts.mark_peers_conflicted", _mark)
    state = _BulkWriteState()
    state.key_of = {"new": "k"}
    state.results = [
        _BulkSetResult(memory_key="k", version=1, memory_id="new", conflict_verdict="conflict")
    ]
    plan = BulkVerdictPlan()
    plan.conflict_marks = [("new", ["peer"])]
    plan.conflicted = ["k"]
    bulk = MemoryBulkSetInput(
        org="o", namespace="n", agent="a", user_id="u", entries=[{"memory_key": "k", "value": "v"}]
    )
    await _bulk_settle_verdicts(None, bulk, state, plan)
    assert plan.conflicted == [] and plan.conflict_marks == []
    assert state.results[0].conflict_verdict == "clear"
