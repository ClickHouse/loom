"""The caller API reads a person's grants in any case, as the gate does
(`authz.grant_role_for`): a grant stored as `Alice@Acme.com` is the grant of the
person signed in as `alice@acme.com`."""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.app import me_callers

GRANTS = [{"subject": "Alice@Acme.com", "namespace": "secret", "role": "admin"}]


class _CH:
    """Answers a grant read the way ClickHouse would for the statement it is sent."""

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        folds = "lower(subject) IN" in sql
        for g in GRANTS:
            subject = g["subject"].lower() if folds else g["subject"]
            if g["namespace"] == params["ns"] and subject in params["subs"]:
                return {"role": g["role"], "x": 1}
        return None


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _aliases(org: str, subject: str) -> list[str]:
        return [subject]

    monkeypatch.setattr(me_callers, "subject_aliases_of", _aliases)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())


@pytest.mark.asyncio
@pytest.mark.usefixtures("wired")
async def test_a_grant_stored_in_another_case_is_the_callers() -> None:
    assert await me_callers.caller_role_on("o", "secret", "alice@acme.com") == "admin"
    assert await me_callers.caller_admin_of("o", "secret", "ALICE@ACME.COM")
    assert await me_callers.caller_role_on("o", "secret", "bob@acme.com") is None
