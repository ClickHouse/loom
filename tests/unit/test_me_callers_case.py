"""The caller API reads a person's grants in any case, as the gate does
(`authz.grant_role_for`): a grant stored as `Alice@Acme.com` is the grant of the
person signed in as `alice@acme.com`."""

from __future__ import annotations

import pytest

from server.identity.app import me_callers
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _aliases(org: str, subject: str) -> list[str]:
        return [subject]

    ch = LocalCH(
        ddl("namespace_grants"),
        "INSERT INTO loom.namespace_grants (org, namespace, subject, role, granted_at, "
        "granted_by, version, status) VALUES "
        "('o', 'secret', 'Alice@Acme.com', 'admin', now(), 'op', 1, 'active')",
    )
    monkeypatch.setattr(me_callers, "subject_aliases_of", _aliases)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
@pytest.mark.asyncio
@pytest.mark.usefixtures("wired")
async def test_a_grant_stored_in_another_case_is_the_callers() -> None:
    assert await me_callers.caller_role_on("o", "secret", "alice@acme.com") == "admin"
    assert await me_callers.caller_admin_of("o", "secret", "ALICE@ACME.COM")
    assert await me_callers.caller_role_on("o", "secret", "bob@acme.com") is None
