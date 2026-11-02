"""Verification state of a memory row — derived, never stored.

A row carries at most one attestation: `verified_by` (an actor string),
`verified_at`, and `verified_sha256` (the canonical content hash the actor
attested to). The attestation binds to CONTENT, so the state has to be
derived at read time against the row's current hash:

  human    — attested by a person and the content has not changed since
  process  — attested by a service account (CI, a validator) and unchanged
  stale    — attested, but the content changed after the attestation (or the
             current hash cannot be established, which is treated the same
             way: an attestation that cannot be confirmed confers nothing)
  none     — never attested

Actor strings follow `<kind>:<subject>`. The kind is decided from the caller's
token at verify time, never taken from the caller's input under auth, so an
agent cannot promote its own write to `human:`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from server.core.hashing import hash_text, sha256_value_json
from server.memory.domain.provenance import is_pipeline_actor

VerificationState = Literal["human", "process", "stale", "none"]
# The states in which an attestation binds the row's current content.
CONFIRMED_STATES: frozenset[VerificationState] = frozenset({"human", "process"})

HUMAN_PREFIX = "human:"
PROCESS_PREFIX = "process:"

_ACTOR_RE = re.compile(r"^(human|process):\S+$")

# A row with no content hash carries the column DEFAULT.
_UNSET_HASHES = frozenset({"", "0"})


def is_valid_actor(actor: str) -> bool:
    return bool(_ACTOR_RE.match(actor or ""))


def actor_for(subject: str, *, is_service_account: bool) -> str:
    """Actor string for a verifying caller: kind from the token, never the body."""
    return f"{PROCESS_PREFIX if is_service_account else HUMAN_PREFIX}{subject}"


def subject_of(actor: str) -> str:
    """The principal an actor string (`actor_for`) names: its subject; '' for one of
    neither kind, and for a pipeline's own actor (`provenance.is_pipeline_actor`),
    which no principal stands behind."""
    if is_pipeline_actor(actor):
        return ""
    for prefix in (HUMAN_PREFIX, PROCESS_PREFIX):
        if actor.startswith(prefix):
            return actor[len(prefix) :]
    return ""


def hash_is_set(sha: str | None) -> bool:
    return (sha or "") not in _UNSET_HASHES


def attests(
    verified_by: str | None, verified_sha256: str | None, current_sha256: str | None
) -> bool:
    """Whether the row counts as verified: its attestation binds its current
    content (`verification_state` is human or process). A row with an
    actor and no attested hash does not: memory.verify, the only writer of an
    attestation, always writes the hash with the actor, so an empty one binds
    nothing. The SQL form is `memory_repo.sql.attested_sql`."""
    return verification_state(verified_by, verified_sha256, current_sha256) in CONFIRMED_STATES


def confirmed(pinned: bool, state: str) -> bool:
    """Whether a person or a service account stood behind the row: it is pinned, or its
    attestation binds its current content (`state` from `verification_state`)."""
    return pinned or state in CONFIRMED_STATES


def verification_state(
    verified_by: str | None,
    verified_sha256: str | None,
    current_sha256: str | None,
) -> VerificationState:
    """Derive the state from the three attestation columns and the row's current hash."""
    if not verified_by:
        return "none"
    if (
        hash_is_set(verified_sha256)
        and hash_is_set(current_sha256)
        and verified_sha256 == current_sha256
    ):
        return "human" if verified_by.startswith(HUMAN_PREFIX) else "process"
    return "stale"


def row_verification(r: Mapping[str, Any]) -> VerificationState:
    """Verification state of a selected row; `value` is the stored JSON text, so it is
    hashed as selected."""
    whole = r.get("value") is not None and not r.get("value_truncated")
    current = live_sha256(r.get("content_sha256"), str(r["value"]) if whole else None)
    return verification_state(r.get("verified_by") or "", r.get("verified_sha256") or "", current)


def row_content_sha256(row: Any) -> str:
    """A MemoryRow's live hash (`live_sha256`); its `value` is decoded, so it is re-encoded
    to hash a row whose column is unset."""
    value = getattr(row, "value", None)
    whole = value is not None and not getattr(row, "value_truncated", False)
    value_json = json.dumps(value, default=str) if whole else None
    return live_sha256(getattr(row, "content_sha256", None), value_json)


def live_sha256(stored: str | bytes | None, value_json: str | None) -> str:
    """The row's live content hash: the stored column when set, else the hash of the value's
    JSON text when it was read whole (None when missing or cut), else '' (cannot be
    confirmed)."""
    column = hash_text(stored)
    if hash_is_set(column):
        return column
    return "" if value_json is None else sha256_value_json(value_json)
