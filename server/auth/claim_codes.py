"""An agent's claim code: what it is stored as, and which accounts still hold one.

Agent self-signup stores the code's hash as `CLAIM_PREFIX` + an argon2id hash,
which `verify_password` reads as malformed. An agent account made before the
prefix holds a plain argon2id hash of its code; it is told apart by its shape
(`UNCLAIMED_SQL`). Only sign-up writes that shape. No settings write changes
the owner, the email or the password, and the one operator step that writes an
email on a password account (`account_admin.link_account`) refuses an
unclaimed one (`is_unclaimed`). Until the claim, no sign-in takes the code;
the claim page (`server.identity.app.agent_claim`) takes it once and writes the
claimer's own password with `CLAIMED_TAG`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from server.auth.passwords import hash_password_off_loop, verify_password_off_loop

CLAIM_PREFIX = "claim:"
# The claim's mark rides the workload-trust tag prefix (`agents.TAG_PREFIX`):
# every build's `namespace.register` carries such a tag forward from the row
# and drops it from a form. A trust's name never holds ':', so this names none.
CLAIMED_TAG = "machine:agent-signup:claimed"

# A `loom.namespaces` row whose password is still an agent's claim code: a
# prefixed hash, or a plain one on an agent sign-up's shape (an `agent-` name
# that is its own owner, with no email) that no claim has marked. No other
# account has a password and no email but a claimed agent's, which carries the
# tag. Constants only, inlined into the WHERE.
UNCLAIMED_SQL = (
    f"(password_hash != '' AND (startsWith(password_hash, '{CLAIM_PREFIX}')"
    " OR (startsWith(namespace, 'agent-') AND owner_email = namespace"
    f" AND contact_email = '' AND NOT has(tags, '{CLAIMED_TAG}'))))"
)


def is_unclaimed(row: Mapping[str, Any]) -> bool:
    """`UNCLAIMED_SQL` for a row already read."""
    password_hash = str(row.get("password_hash") or "")
    if not password_hash:
        return False
    if password_hash.startswith(CLAIM_PREFIX):
        return True
    namespace = str(row.get("namespace") or "")
    return (
        namespace.startswith("agent-")
        and row.get("owner_email") == namespace
        and not row.get("contact_email")
        and CLAIMED_TAG not in (row.get("tags") or [])
    )


async def hash_claim_code_off_loop(code: str) -> str:
    return CLAIM_PREFIX + await hash_password_off_loop(code)


async def verify_claim_code_off_loop(stored_hash: str, code: str) -> bool:
    """`code` against an unclaimed row's hash, prefixed or plain."""
    return await verify_password_off_loop(stored_hash.removeprefix(CLAIM_PREFIX), code)
