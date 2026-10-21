"""The auth plane's own reads and writes (server/auth/): the grants a token's
claim is capped at, the subject revocations a mint reads, the sign-in state's
compare-and-set in Keeper, the JIT registry reads, and SCIM's user, subject and
revocation reads. The OAuth exchange and grant change claims are leases
(`server.core.leases`), whose statements are recorded with the leases."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.versions import next_version
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


async def _scim(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.auth import revocation, scim, service_accounts

    await scim.find("alice@x.io")
    await scim.page(1, 10)
    await scim.page(1, 10, email="alice@x.io")  # the emails filters
    await scim.page(1, 10, email="alice@x.io", work=True)
    await scim.deactivated("alice@x.io")  # every SSO sign-in reads it, SCIM on or off
    await scim._row(scim.user_id("alice@x.io"))
    await scim._subjects("alice@x.io")
    await scim._orgs_beyond("alice", {ORG})
    await scim._orgs_beyond("alice", set())  # a subject the email no longer reaches: NOT IN []
    await scim._held_by_another_inactive("id", "alice")
    await revocation.refresh()
    await revocation.revoked_outright("alice")
    await revocation.stored("alice")
    await revocation._next_version("alice")
    await service_accounts.revoke_created_by(["alice"], revoked_by="scim")
    # A new token's version, past every row its name held, and the ancestry its
    # former holder hands down first.
    await next_version(ch, "loom.service_accounts", {"org": ORG, "name": "bot"})
    await service_accounts.hand_down_lineage(ORG, "bot")


def _scim_rows(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    # The one-row answers a count and `next_version` read, and one live token
    # the person made, so its name's re-read under the claim runs too.
    if "AS live FROM loom.service_accounts" in sql and "alice" in params.get("subs", []):
        made = datetime(2026, 1, 1)
        token = {"org": ORG, "name": "bot", "sa_subject": "sa:bot-1", "created_by": "alice"}
        return [{**token, "created_at": made, "lineage": ["alice"], "live": 1}]
    if "SELECT sa_subject, created_by, lineage FROM" in sql:
        return [{"sa_subject": "sa:bot-1", "created_by": "alice", "lineage": ["alice"]}]
    if "SELECT name FROM loom.service_accounts" in sql:
        return [{"name": "child"}]
    if "count() AS n" in sql:
        return [{"n": 0}]
    if "max(version)" in sql:
        return [{"v": 1}]
    return []


def cases() -> list[Case]:
    return [
        Case("auth_reads_and_claims", "server.auth", _auth, {}),
        Case("scim_reads", "server.auth", _scim, {"scim_token": ["s" * 32]}, rows=_scim_rows),
    ]
