"""A duplicate-check draft at its own field limits is a valid request: the
text and frames handed to `similar` are cut to that model's limits instead of
failing its validation (a 500 on a long issue body)."""

from __future__ import annotations

import pytest

from server.tracker.api.models import TrackerDuplicateCheckInput, TrackerSimilarInput
from server.tracker.app import dupcheck_candidates as dc


class _Stop(Exception):
    pass


@pytest.mark.asyncio
async def test_a_maximal_draft_reaches_similar_within_its_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[TrackerSimilarInput] = []

    async def fake_query(src, sim_input, sim):
        seen.append(sim_input)
        raise _Stop

    monkeypatch.setattr(dc.tsim, "_similar_query", fake_query)
    draft = TrackerDuplicateCheckInput(
        org="o",
        namespace="qa-x",
        title="t" * 500,
        body="b" * 20000,
        frames=[f"f{i}" for i in range(60)],
        symbols=[f"s{i}" for i in range(40)],
    )

    with pytest.raises(_Stop):
        await dc.candidates(None, draft, wall=None, status={})

    sim = seen[0]
    assert len(sim.text) == TrackerSimilarInput.model_fields["text"].metadata[0].max_length
    assert sim.text.startswith("t" * 500 + "\n")
    assert len(sim.frames) == 60
    assert sim.frames[0] == "f0"
