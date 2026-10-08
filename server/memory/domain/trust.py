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

import re
from typing import Literal

VerificationState = Literal["human", "process", "stale", "none"]

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
    return verification_state(verified_by, verified_sha256, current_sha256) in ("human", "process")


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
