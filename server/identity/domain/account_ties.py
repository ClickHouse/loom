"""The spellings an account ties together, and who its login signs in as.

A person's account ties its login handle (`owner_email`) to its email
(`contact_email`); the claim cap reads that tie to cap a token minted for one
spelling at a grant revoked under the other. A release clears both fields and
keeps the old pair in `released_aliases`, which only the cap reads. An account
with a login does not change owner otherwise.
"""

from __future__ import annotations

from typing import Any


def kept_aliases(existing: dict[str, Any]) -> list[str]:
    """`released_aliases` for a released account: the stored ones, plus the
    pair it tied when it was a person's account."""
    kept = set(existing.get("released_aliases") or [])
    if existing.get("contact_email"):
        kept |= {
            (existing.get("owner_email") or "").lower(),
            str(existing["contact_email"]).lower(),
        } - {""}
    return sorted(kept)


def owner_moved(existing: dict[str, Any], owner: str) -> bool:
    """Whether writing `owner` over an account with a login (an email, or a
    password such as an agent's claim code) changes who the login signs in as."""
    if not (existing.get("contact_email") or existing.get("password_hash")):
        return False
    return owner.lower() != (existing.get("owner_email") or "").lower()
