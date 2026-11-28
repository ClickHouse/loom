"""How an agent may use one authorized revision, given only the evidence it can read."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, cast, get_args

from pydantic import BaseModel

from server.core.hashing import hash_text
from server.memory.domain import proposal_review, trust
from server.memory.domain.provenance import SOURCE_CLASSES
from server.memory.domain.redaction_lineage import is_content_hash

NextStep = Literal[
    "inspect_only",
    "clarify_dispute",
    "recheck_source",
    "obtain_review",
    "validate_lead",
    "follow_if_applicable",
]
Reason = Literal[
    "diagnostic_only",
    "dispute_unresolved",
    "source_recheck_needed",
    "code_recheck_needed",
    "review_pending",
    "review_hash_mismatch",
    "required_evidence_unknown",
    "procedure_candidate",
    "human_review_current",
    "process_review_current",
    "no_attestation",
    "review_unknown",
    "source_asserted",
    "source_unchanged",
    "code_unchanged",
    "freshness_unknown",
    "dispute_unknown",
]
Review = Literal["human", "process", "stale", "none", "unknown"]
Source = Literal["asserted", "extracted", "observed", "derived", "imported", "external", "unknown"]
SourceState = Literal["unchanged", "changed", "missing", "not_applicable", "unknown"]
CodeState = Literal[
    "fresh", "moved", "changed", "unresolved", "missing", "not_applicable", "unknown"
]
Dispute = Literal["unresolved", "none", "unknown"]
ProcedureState = Literal[
    "candidate", "exercised", "authored", "quarantined", "template", "not_applicable", "unknown"
]


class GuidanceLifecycle(BaseModel):
    status: str
    procedure_status: ProcedureState
    review_requirement: Literal["pending", "none", "unknown"]


class GuidanceFreshness(BaseModel):
    source: SourceState
    code: CodeState


class UseGuidance(BaseModel):
    next_step: NextStep
    reasons: list[Reason]
    source_class: Source
    lifecycle: GuidanceLifecycle
    review: Review
    freshness: GuidanceFreshness
    dispute: Dispute


def _review(row: Mapping[str, Any]) -> Review:
    if "verified_by" not in row or row["verified_by"] is None:
        return "unknown"
    if not row["verified_by"]:
        return "none"
    actor = str(row["verified_by"])
    if not trust.is_valid_actor(actor):
        return "unknown"
    stored = row.get("value")
    whole = isinstance(stored, str) and not row.get("value_truncated")
    current = trust.live_sha256(row.get("content_sha256"), stored if whole else None)
    attested = hash_text(row.get("verified_sha256"))
    if not is_content_hash(current) or not is_content_hash(attested):
        return "unknown"
    return trust.verification_state(actor, attested, current)


def guidance_for_row(
    row: Mapping[str, Any],
    *,
    code_state: str = "unknown",
    source_state: str = "unknown",
    procedure_status: str = "not_applicable",
    dispute: str = "unknown",
) -> UseGuidance:
    """Project facts selected for the served revision. A missing column has unknown coverage."""
    status = str(row.get("status") or row.get("status_text") or "unknown")
    origin = row.get("source_class")
    source = cast(Source, origin if origin in SOURCE_CLASSES else "unknown")
    proc = cast(
        ProcedureState,
        procedure_status if procedure_status in get_args(ProcedureState) else "unknown",
    )
    lifecycle = GuidanceLifecycle(
        status=status,
        procedure_status=proc,
        review_requirement=(
            "pending"
            if proposal_review.pending_review(dict(row, status=status))
            else "none"
            if row.get("requires_review") is not None
            else "unknown"
        ),
    )
    if (
        source_state == "unknown"
        and source == "asserted"
        and row.get("source_trace") == ""
        and row.get("source_resource_id") == ""
    ):
        source_state = "not_applicable"
    freshness = GuidanceFreshness(
        source=cast(
            SourceState, source_state if source_state in get_args(SourceState) else "unknown"
        ),
        code=cast(CodeState, code_state if code_state in get_args(CodeState) else "unknown"),
    )
    dispute_state = cast(Dispute, dispute if dispute in get_args(Dispute) else "unknown")
    if status == "conflicted":
        dispute_state = "unresolved"
    review = _review(row)
    rules: list[tuple[bool, NextStep, Reason]] = [
        (
            status in {"rejected", "withdrawn", "superseded", "duplicate", "retired"}
            or proc == "quarantined",
            "inspect_only",
            "diagnostic_only",
        ),
        (dispute_state == "unresolved", "clarify_dispute", "dispute_unresolved"),
        (freshness.source in {"changed", "missing"}, "recheck_source", "source_recheck_needed"),
        (
            freshness.code in {"changed", "unresolved", "missing"},
            "recheck_source",
            "code_recheck_needed",
        ),
        (lifecycle.review_requirement == "pending", "obtain_review", "review_pending"),
        (review == "stale", "obtain_review", "review_hash_mismatch"),
        (
            status != "active"
            or lifecycle.review_requirement == "unknown"
            or proc == "unknown"
            or "unknown" in (freshness.code, freshness.source),
            "validate_lead",
            "required_evidence_unknown",
        ),
        (proc == "candidate", "validate_lead", "procedure_candidate"),
        (review == "human", "follow_if_applicable", "human_review_current"),
        (review == "process", "validate_lead", "process_review_current"),
        (review == "none", "validate_lead", "no_attestation"),
        (review == "unknown", "validate_lead", "review_unknown"),
    ]
    matched = [(step, reason) for applies, step, reason in rules if applies]
    reasons = [reason for _, reason in matched]
    if source == "asserted":
        reasons.append("source_asserted")
    if freshness.source == "unchanged":
        reasons.append("source_unchanged")
    if freshness.code in {"fresh", "moved"}:
        reasons.append("code_unchanged")
    if "unknown" in (freshness.code, freshness.source):
        reasons.append("freshness_unknown")
    if dispute_state == "unknown":
        reasons.append("dispute_unknown")
    return UseGuidance(
        next_step=matched[0][0],
        reasons=reasons,
        source_class=source,
        review=review,
        lifecycle=lifecycle,
        freshness=freshness,
        dispute=dispute_state,
    )
