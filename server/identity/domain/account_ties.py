"""The spellings an account ties together, kept once the account stops tying them.

A person's account ties its login handle (`owner_email`) to its email
(`contact_email`); the claim cap reads that tie to cap a token minted for one
spelling at a grant revoked under the other. A write that clears or changes
either field keeps the old pair in `released_aliases`, which only the cap reads.
"""

from __future__ import annotations

from typing import Any


def kept_aliases(existing: dict[str, Any], owner: str, contact: str) -> list[str]:
    """`released_aliases` for a row that becomes (owner, contact): the stored
    ones, plus the account's old pair when the new one no longer ties it."""
    kept = set(existing.get("released_aliases") or [])
    old_owner = (existing.get("owner_email") or "").lower()
    old_contact = (existing.get("contact_email") or "").lower()
    if old_contact and (old_owner, old_contact) != (owner.lower(), contact.lower()):
        kept |= {old_owner, old_contact} - {""}
    return sorted(kept)
