"""The last-admin guards count live admin grants through the repository.

`tests/unit/test_pending_grant_confers_nothing.py` runs the count on
`clickhouse local`; this file checks, without a binary, that the count skips
pending rows and that the guards count through it.
"""

from __future__ import annotations

from typing import Any

import pytest


class _Recorder:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.sql.append(sql)
        return None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.sql.append(sql)
        return []


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr("server.core.ch.get_client", lambda: rec)

    async def aliases(org: str, subject: str, **_: Any) -> list[str]:
        return [subject]

    monkeypatch.setattr("server.auth.aliases.subject_aliases", aliases)
    return rec


async def test_count_active_admins_skips_pending_rows(ch: _Recorder) -> None:
    from server.identity.store import grants as grants_repo

    assert await grants_repo.count_active_admins("o", "ns") == 0
    (sql,) = ch.sql
    assert "status = 'active'" in sql and "status = ''" not in sql


async def test_leave_refuses_the_last_live_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import PreconditionFailedError
    from server.identity.api.models import NamespaceLeaveInput
    from server.identity.app import namespace_membership as namespace
    from server.identity.domain.grants import GrantState
    from server.identity.store import grants as grants_repo

    async def active(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row({"role": "admin", "status": "active"})

    async def one_admin(*_a: Any, **_k: Any) -> int:
        return 1

    monkeypatch.setattr(grants_repo, "find_active_grant", active)
    monkeypatch.setattr(grants_repo, "count_active_admins", one_admin)
    monkeypatch.setattr(namespace, "get_client", lambda: _Recorder())
    with pytest.raises(PreconditionFailedError):
        await namespace.handle_namespace_leave(
            NamespaceLeaveInput(org="o", namespace="ns", subject="eve")
        )
