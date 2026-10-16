"""The auth plane's own reads and writes (server/auth/): the grants a token's
claim is capped at, the subject revocations a mint reads, the sign-in state's
compare-and-set in Keeper, and the JIT registry reads. The OAuth exchange and grant change claims are leases
(`server.core.leases`), whose statements are recorded with the leases."""

from __future__ import annotations

from typing import Any

from tests.analyzer.cases._common import NS, ORG
from tests.analyzer.corpus import Case, RecordingCH


async def _auth(ch: RecordingCH, c: dict[str, Any]) -> None:
    import contextlib

    from server.auth import claims, jit, revocation
    from server.core import grant_cache

    await grant_cache._read(ORG, "alice@x.io")
    await revocation._read_revocations(["alice"])
    await revocation.list_active()
    await jit._registry_row(ORG, NS)
    await jit._accounts_naming(ORG, "alice@x.io")
    await jit._ever_granted(ORG, {"namespace": NS, "owner": "alice"})
    # Sign-in state: a new key (its insert, then the undo of one whose read-back
    # never showed it), and a swap from a known value.
    with contextlib.suppress(claims.Busy):
        await claims.swap(ch, "code:abc", None, "v1", 0)
    await claims.swap(ch, "code:abc", "v1", "v2", 0)


def cases() -> list[Case]:
    return [Case("auth_reads_and_claims", "server.auth", _auth, {})]
