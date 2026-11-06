"""Identifier-grounding abstain leg: the pure detector.

`identifier_grounding_missing` catches the "on-topic but factually wrong"
recall mode — a query naming a specific identifier the corpus lacks still
draws a same-domain neighbour, so the cosine gate never fires. The detector
is deliberately conservative: only high-precision identifier forms, and a
single grounded identifier is enough to keep the result.
"""

from __future__ import annotations

import pytest

from server.memory.domain.retrieval_calibration import (
    finalize_abstain,
    identifier_grounding_missing,
)


@pytest.mark.parametrize(
    ("query", "hits", "reason"),
    [
        pytest.param(
            "fix status of PR 113594",
            ["merged the fix for PR 117593 last week", "reviewed #120000"],
            "entity_absent:113594",
            id="PR number absent",
        ),
        pytest.param(
            "fix status of PR 113594",
            ["the fix for PR 113594 was reverted"],
            None,
            id="PR number present",
        ),
        # Query says "#113594"; the hit writes the bare number: still grounded.
        pytest.param(
            "what about #113594", ["closed 113594 as dup"], None, id="hash and bare number"
        ),
        pytest.param(
            "did @octocat approve it",
            ["approved by someone on the team"],
            "entity_absent:octocat",
            id="handle absent",
        ),
        pytest.param(
            "did @octocat approve it",
            ["approved by @octocat yesterday"],
            None,
            id="handle present",
        ),
        pytest.param(
            "how do I tune the abstain threshold",
            ["set LOOM_MEMORY_SEARCH_..."],
            None,
            id="no identifier",
        ),
        # @Handle in the query, @handle in the hit: grounded.
        pytest.param("ping @Handle", ["notified @handle earlier"], None, id="case insensitive"),
        # Two ids; only 117593 is in a hit. ANY grounded identifier keeps the result.
        pytest.param(
            "compare PR 113594 and issue 117593",
            ["issue 117593 tracks the rollout"],
            None,
            id="one of several present",
        ),
        pytest.param(
            "compare PR 113594 and issue 117593",
            ["unrelated note about the deploy"],
            "entity_absent:113594",
            id="all absent names the first",
        ),
        pytest.param("PR 113594", [], "entity_absent:113594", id="no hits"),
    ],
)
def test_identifier_grounding_missing_names_the_first_identifier_when_none_is_grounded(
    query: str, hits: list[str], reason: str | None
) -> None:
    assert identifier_grounding_missing(query, hits) == reason


def test_finalize_abstain_forces_entity_absent_above_threshold() -> None:
    # A strong top hit (0.60) that would NOT abstain normally still abstains
    # when the grounding leg supplies a reason — that is the whole point.
    class _Cal:
        risk = "medium"
        explanation = ""

    abstain, code, reason = finalize_abstain(
        top_similarity=0.60,
        calibration=_Cal(),
        abstain_threshold=0.268,
        grounding_reason="entity_absent:113594",
    )
    assert abstain is True
    assert code == "entity_absent"
    assert reason == "entity_absent:113594"


def test_finalize_abstain_grounding_none_is_noop() -> None:
    class _Cal:
        risk = "medium"
        explanation = ""

    abstain, code, reason = finalize_abstain(
        top_similarity=0.60,
        calibration=_Cal(),
        abstain_threshold=0.268,
        grounding_reason=None,
    )
    assert abstain is False
    assert code == ""
