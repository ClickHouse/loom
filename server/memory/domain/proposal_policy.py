"""The namespace owner's proposal policy: its shape and its stored form."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProposalPolicy(BaseModel):
    """The namespace owner's rule for proposals (stored as JSON on
    `loom.namespaces.proposal_policy`; '' = the defaults, manual)."""

    mode: Literal["manual", "auto"] = "manual"
    # Accept once this many graded runs that used the proposal passed ...
    min_passes: int = Field(default=2, ge=1, le=1000)
    # ... and no more than this many failed.
    max_fails: int = Field(default=0, ge=0, le=1000)
    # Reject once this many failed (0 = never reject on counts).
    reject_after_fails: int = Field(default=0, ge=0, le=1000)


def parse_policy(raw: Any) -> ProposalPolicy:
    """The stored policy; the defaults for '' or anything unparseable (a
    malformed row never turns auto on)."""
    if isinstance(raw, ProposalPolicy):
        return raw
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return ProposalPolicy()
        try:
            data = json.loads(text)
        except ValueError:
            return ProposalPolicy()
    try:
        return ProposalPolicy.model_validate(data or {})
    except Exception:  # noqa: BLE001 — a bad row reads as manual
        return ProposalPolicy()


def serialize_policy(policy: ProposalPolicy | None) -> str:
    """'' for the defaults, so an unset policy stores nothing."""
    if policy is None or policy == ProposalPolicy():
        return ""
    return policy.model_dump_json()
