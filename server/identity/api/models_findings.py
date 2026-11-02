"""The evidence contract every diagnostic finding carries.

`namespace.audit` and `namespace.validate` keep their own codes, severities and samples;
these fields say what kind of result each finding is, which rule produced it, what it was
measured against and which stored revisions back it, so the CLI, the dashboard, an MCP
client and a chat client read one shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# contract_violation: a stored value breaks a declared invariant, schema or attestation.
# usage_heuristic: a pattern that usually costs recall or storage; a warning, not proof of
#   misuse, since a legitimate design can match it.
# system_state: the deployment's own state (an indexing backlog, missing telemetry), not
# anything a writer did.
FindingKind = Literal["contract_violation", "usage_heuristic", "system_state"]


class FindingEvidence(BaseModel):
    """One live revision a finding's sample names, read once the report's checks have run.

    It carries identifiers only, never the value."""

    memory_key: str
    memory_id: str
    version: int
    # The hash memory.get serves; '' for a high-sensitivity row, a value longer than
    # namespace_report_hash_max_bytes, or a row that changed while the report read it.
    content_sha256: str
    agent: str
    user_id: str
    session_id: str
    memory_type: str
    source_class: str
    updated_at: datetime


class FindingContract(BaseModel):
    """The fields a finding adds to its code, severity, count and samples."""

    kind: FindingKind
    # Raised when the rule's predicate or a built-in threshold changes, so reports at two
    # versions are not one series; settings and request inputs are not versioned.
    rule_version: int = Field(ge=1)
    # The operation that computed the finding.
    source_op: str
    # How far back the rule looked, in seconds; None when it reads current state only.
    window_seconds: int | None = None
    # The denominator of `share`; None for a count with none (every validate check) and
    # for an audit finding whose summary read was skipped.
    population: int | None = None
    # The sample cap the report ran with.
    sample_limit: int
    # False when the cap cut the samples or they cannot name every match (hot keys
    # against all gets). Required, so no finding claims completeness by default.
    samples_complete: bool
    # The live revision of each row the samples matched, read after the checks under the
    # request's read scope; a sample that names no single row has none.
    evidence: list[FindingEvidence] = Field(default_factory=list)
    # What the rule leaves out by design.
    exceptions: str = ""
    # Why a hit may still be correct, or what the rule cannot see.
    uncertainty: str = ""


class ReportCoverage(BaseModel):
    """Which rules a report ran and which it skipped.

    A finding's incomplete samples are marked on the finding."""

    rules_run: list[str] = Field(default_factory=list)
    rules_skipped: list[str] = Field(default_factory=list)
    # No rule skipped. Required, so no report claims to be complete by default.
    complete: bool
