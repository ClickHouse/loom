"""The auth plane's own reads and writes (server/auth/): the grants a token's
claim is capped at, the subject revocations a mint reads, the OAuth exchange
claim, and the JIT registry reads."""

from __future__ import annotations

from typing import Any

from tests.analyzer.cases._common import NS, ORG
from tests.analyzer.corpus import Case, RecordingCH


async def _auth(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.auth import grant_claims, jit, oauth_claims, revocation
    from server.core import grant_cache
    from server.identity.store import grants as grants_repo

    await grant_cache._read(ORG, "alice@x.io")
    await grants_repo.admin_revoked_at(ORG, NS, "alice")
    await revocation._read_revocations(["alice"])
    await revocation.list_active()
    await jit._registry_row(ORG, NS)
    await jit._accounts_naming(ORG, "alice@x.io")
    await jit._ever_granted(ORG, {"namespace": NS, "owner": "alice"})
    claim = {"k": "code:abc", "me": "h1", "stale": oauth_claims.STALE_S}
    await ch.execute(oauth_claims.CLAIMS.insert_sql, params=claim)
    await ch.query_one(oauth_claims.CLAIMS.holder_sql, params=claim)
    await ch.execute(oauth_claims.CLAIMS.take_over_sql, params=claim)
    await ch.execute(oauth_claims.CLAIMS.release_sql, params=claim)
    await ch.execute(oauth_claims.CLAIMS.sweep_sql, params=claim)
    # The grant change claims are the same statements on loom.grant_claims.
    await ch.execute(grant_claims.CLAIMS.insert_sql, params=claim)
    await ch.execute(grant_claims.CLAIMS.take_over_sql, params=claim)


def cases() -> list[Case]:
    return [Case("auth_reads_and_claims", "server.auth", _auth, {})]
