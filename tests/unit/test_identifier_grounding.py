"""Identifier-grounding abstain leg: the pure detector.

`identifier_grounding_missing` catches the "on-topic but factually wrong"
recall mode — a query naming a specific identifier the corpus lacks still
draws a same-domain neighbour, so the cosine gate never fires. The detector
is deliberately conservative: only high-precision identifier forms, and a
single grounded identifier is enough to keep the result.
"""

from __future__ import annotations

from server.memory.domain.retrieval_calibration import (
    finalize_abstain,
    identifier_grounding_missing,
)


def test_pr_number_absent_from_hits_returns_reason() -> None:
    reason = identifier_grounding_missing(
        "fix status of PR 113594",
        ["merged the fix for PR 117593 last week", "reviewed #120000"],
    )
    assert reason == "entity_absent:113594"


def test_pr_number_present_in_a_hit_returns_none() -> None:
    reason = identifier_grounding_missing(
        "fix status of PR 113594",
        ["the fix for PR 113594 was reverted"],
    )
    assert reason is None


def test_hash_and_bare_number_forms_ground_the_same_id() -> None:
    # Query says "#113594"; hit writes the bare number — still grounded.
    assert identifier_grounding_missing("what about #113594", ["closed 113594 as dup"]) is None


def test_handle_absent_returns_reason() -> None:
    reason = identifier_grounding_missing(
        "did @octocat approve it",
        ["approved by someone on the team"],
    )
    assert reason == "entity_absent:octocat"


def test_handle_present_returns_none() -> None:
    assert (
        identifier_grounding_missing("did @octocat approve it", ["approved by @octocat yesterday"])
        is None
    )


def test_no_identifier_query_returns_none() -> None:
    assert (
        identifier_grounding_missing(
            "how do I tune the abstain threshold", ["set LOOM_MEMORY_SEARCH_..."]
        )
        is None
    )


def test_grounding_is_case_insensitive() -> None:
    # @Handle in the query, @handle in the hit -> grounded.
    assert identifier_grounding_missing("ping @Handle", ["notified @handle earlier"]) is None


def test_multiple_identifiers_one_present_returns_none() -> None:
    # Two ids; only #117593 is in a hit. ANY grounded identifier keeps the result.
    assert (
        identifier_grounding_missing(
            "compare PR 113594 and issue 117593",
            ["issue 117593 tracks the rollout"],
        )
        is None
    )


def test_multiple_identifiers_all_absent_returns_first() -> None:
    reason = identifier_grounding_missing(
        "compare PR 113594 and issue 117593",
        ["unrelated note about the deploy"],
    )
    assert reason == "entity_absent:113594"


def test_empty_hits_with_identifier_returns_reason() -> None:
    assert identifier_grounding_missing("PR 113594", []) == "entity_absent:113594"


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
