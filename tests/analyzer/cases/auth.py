"""The auth plane's own reads and writes (server/auth/): grant cutoffs and the
spellings they cover, the OAuth exchange claim, the JIT registry reads, and
the grant reads a grant change decides from."""

from __future__ import annotations

from typing import Any

from tests.analyzer.cases._common import NS, ORG
from tests.analyzer.corpus import Case, RecordingCH


async def _auth(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.auth import aliases, jit, oauth_claims, revocation
    from server.identity.store import grants as G

    # One shape whether deactivated accounts count or not: it is a parameter.
    await aliases._read_spellings(ORG, "alice@x.io", ch, include_inactive=True)
    await revocation._read_revocations(["grant:o/n/k1", "alice"])
    await revocation.list_active()
    await jit._registry_row(ORG, NS)
    await jit._accounts_naming(ORG, "alice@x.io")
    await G.active_roles(ORG, NS, ["alice"], ch=ch)
    await G.find_active_grant(ORG, NS, "alice", ch=ch)
    await G.find_any_grant(ORG, NS, "alice", ch=ch)
    claim = {"k": "code:abc", "me": "h1", "stale": oauth_claims.STALE_S}
    await ch.query_one(oauth_claims._HOLDER, params=claim)
    await ch.execute(oauth_claims._TAKE_OVER, params=claim)
    await ch.execute(oauth_claims._RELEASE, params=claim)
    await ch.execute(oauth_claims._SWEEP, params=claim)


def cases() -> list[Case]:
    return [Case("auth_reads_and_claims", "server.auth", _auth, {})]
