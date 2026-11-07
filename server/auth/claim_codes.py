"""An agent's claim code: what it is stored as, and which accounts still hold one.

Agent self-signup stores the code's hash as `CLAIM_PREFIX` + an argon2id hash,
which `verify_password` reads as malformed. An agent account made before the
prefix holds a plain argon2id hash of its code; it is told apart from a person's
account by its shape (`UNCLAIMED_SQL`). Until the claim, no sign-in takes the
code; the claim page (`server.identity.app.agent_claim`) takes it once and
writes the claimer's own password with `CLAIMED_TAG`.
"""

from __future__ import annotations

from server.auth.passwords import hash_password_off_loop, verify_password_off_loop

CLAIM_PREFIX = "claim:"
# Agent sign-up's tag, and the server-owned mark a claim adds next to it
# (`namespace.register` carries it forward and drops it from the form).
AGENT_TAG = "agent"
CLAIMED_TAG = "agent-claimed"

# A `loom.namespaces` row whose password is still an agent's claim code: a
# prefixed hash, or an agent sign-up's shape (tagged, no email, the namespace its
# own owner) that no claim has marked. Constants only, inlined into the WHERE.
UNCLAIMED_SQL = (
    f"(password_hash != '' AND (startsWith(password_hash, '{CLAIM_PREFIX}')"
    f" OR (has(tags, '{AGENT_TAG}') AND NOT has(tags, '{CLAIMED_TAG}')"
    " AND contact_email = '' AND owner_email = namespace)))"
)


async def hash_claim_code_off_loop(code: str) -> str:
    return CLAIM_PREFIX + await hash_password_off_loop(code)


async def verify_claim_code_off_loop(stored_hash: str, code: str) -> bool:
    """`code` against an unclaimed row's hash, prefixed or plain."""
    return await verify_password_off_loop(stored_hash.removeprefix(CLAIM_PREFIX), code)
