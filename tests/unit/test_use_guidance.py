"""The permitted next step for one revision with the evidence the reader can inspect."""

from typing import Any

import pytest

from server.core.hashing import sha256_value
from server.memory.domain.use_guidance import guidance_for_row

SHA = sha256_value("read the index")
ROW = {"status": "active", "source_class": "asserted", "requires_review": False, "verified_by": ""}
REVIEW = {"verified_by": "human:reviewer", "verified_sha256": SHA, "content_sha256": SHA}


@pytest.mark.parametrize(
    ("facts", "code", "dispute", "step", "reason"),
    [
        ({}, "fresh", "none", "validate_lead", "no_attestation"),
        (
            REVIEW | {"verified_by": "process:validator"},
            "fresh",
            "none",
            "validate_lead",
            "process_review_current",
        ),
        (REVIEW, "fresh", "unknown", "follow_if_applicable", "human_review_current"),
        (
            REVIEW | {"content_sha256": "f" * 64},
            "fresh",
            "none",
            "obtain_review",
            "review_hash_mismatch",
        ),
        (REVIEW, "changed", "unresolved", "clarify_dispute", "dispute_unresolved"),
        (REVIEW, "changed", "none", "recheck_source", "code_recheck_needed"),
        (REVIEW, "unknown", "none", "validate_lead", "required_evidence_unknown"),
        (
            REVIEW | {"status": "rejected"},
            "changed",
            "unresolved",
            "inspect_only",
            "diagnostic_only",
        ),
        (REVIEW | {"requires_review": True}, "fresh", "none", "obtain_review", "review_pending"),
    ],
)
def test_guidance(facts: dict[str, Any], code: str, dispute: str, step: str, reason: str) -> None:
    row = {**ROW, **facts}
    opts = dict(source_state="unchanged", code_state=code, dispute=dispute)
    result = guidance_for_row(row, **opts)
    assert result.next_step == step
    assert result.reasons[0] == reason
    assert result == guidance_for_row(
        row | {"pinned": True, "verified": True, "recall_count": 999, "similarity": 1}, **opts
    )


def test_unknown_is_distinct_from_checked_absence_and_changed_bytes() -> None:
    assert guidance_for_row({}).review == "unknown"
    assert guidance_for_row(ROW).review == "none"
    row = ROW | REVIEW | {"content_sha256": "", "value": '"read the index"'}
    assert guidance_for_row(row).review == "human"
    assert guidance_for_row(row | {"value_truncated": True}).review == "unknown"
    assert (
        guidance_for_row(row | {"content_sha256": SHA.encode(), "value_truncated": True}).review
        == "human"
    )
    assert guidance_for_row(row | {"verified_sha256": "broken"}).review == "unknown"


def test_accumulates_independent_reasons_in_precedence_order() -> None:
    row = ROW | REVIEW | {"tags": ["source-changed"], "requires_review": True}
    result = guidance_for_row(
        row, source_state="changed", code_state="changed", dispute="unresolved"
    )
    assert result.reasons == [
        "dispute_unresolved",
        "source_recheck_needed",
        "code_recheck_needed",
        "review_pending",
        "human_review_current",
        "source_asserted",
    ]


def test_procedure_and_allegation_do_not_change_publication() -> None:
    opts = dict(source_state="not_applicable", code_state="not_applicable")
    for status, step in [("quarantined", "inspect_only"), ("candidate", "validate_lead")]:
        assert guidance_for_row(ROW | REVIEW, procedure_status=status, **opts).next_step == step
    result = guidance_for_row(ROW | {"status": "conflicted"}, **opts)
    assert result.next_step == "clarify_dispute"
    assert result.lifecycle.status == "conflicted"
