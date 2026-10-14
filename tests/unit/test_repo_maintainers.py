"""A repository's maintainer set is one across its branches."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from server.code.app.index import maintainers
from server.code.domain.index import repos
from server.identity.api.models_grants import (
    GrantsApproveInput,
    GrantsCreateInput,
    GrantsRevokeInput,
)
from server.identity.app import grants as grants_mod
from server.identity.domain.grants import GrantState
from server.identity.store import grants as grants_repo


class _Cfg:
    def __init__(self, org: str, namespace: str, repo: str) -> None:
        self.org, self.namespace, self.repo = org, namespace, repo


REPOS = [
    _Cfg("clickhouse", "code-x", "Org/x"),
    _Cfg("clickhouse", "code-x-26-5", "Org/x"),
    _Cfg("clickhouse", "code-x-26-6", "Org/x"),
    _Cfg("clickhouse", "code-y", "Org/y"),
]


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.inserted: list[dict[str, Any]] = []
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append(sql)
        wanted = set(params["ns"])
        return [r for r in self.rows if r["namespace"] in wanted]

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.namespace_grants"
        self.inserted.extend(rows)


T0 = datetime(2026, 9, 17, 10, 0, 0)


def _admin(
    ns: str,
    subject: str,
    *,
    at: datetime = T0,
    status: str = "active",
    revoked_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "namespace": ns,
        "subject": subject,
        "status": status,
        "revoked_at": revoked_at,
        "granted_at": at,
        "granted_by": "ops",
    }


@pytest.fixture(autouse=True)
def _repos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: REPOS)


def test_branch_namespaces_groups_by_repository() -> None:
    assert maintainers.branch_namespaces("clickhouse", "code-x-26-5") == [
        "code-x",
        "code-x-26-5",
        "code-x-26-6",
    ]
    assert maintainers.branch_namespaces("clickhouse", "code-y") == ["code-y"]
    assert maintainers.branch_namespaces("clickhouse", "not-a-repo") == []
    assert maintainers.branch_namespaces("other-org", "code-x") == []


async def test_sync_writes_the_union_to_every_branch_that_lacks_it() -> None:
    ch = _FakeCH(
        [_admin("code-x", "alice"), _admin("code-x-26-6", "bob", at=T0 + timedelta(hours=1))]
    )
    n = await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    written = sorted((r["namespace"], r["subject"], r["granted_by"]) for r in ch.inserted)
    assert n == 4 and written == [
        ("code-x", "bob", "repo-maintainers:code-x-26-6"),
        ("code-x-26-5", "alice", "repo-maintainers:code-x"),
        ("code-x-26-5", "bob", "repo-maintainers:code-x-26-6"),
        ("code-x-26-6", "alice", "repo-maintainers:code-x"),
    ]
    assert all(
        r["role"] == "admin" and r["status"] == "active" and r["revoked_at"] is None
        for r in ch.inserted
    )


async def test_sync_ignores_pending_maintainer_requests_and_single_branch_repos() -> None:
    ch = _FakeCH([_admin("code-x", "alice"), _admin("code-x-26-5", "carol", status="pending")])
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert {r["subject"] for r in ch.inserted} == {"alice"}  # carol only asked
    ch2 = _FakeCH([_admin("code-y", "alice")])
    assert (
        await maintainers.sync_repo_maintainers("clickhouse", "code-y", ch=ch2) == 0
        and ch2.inserted == []
    )


async def test_sync_respects_a_newer_explicit_revocation_on_a_branch() -> None:
    ch = _FakeCH(
        [
            _admin("code-x", "alice"),
            _admin(
                "code-x-26-5", "alice", revoked_at=T0 + timedelta(hours=2)
            ),  # removed from 26-5 after the grant
            _admin(
                "code-x-26-6", "alice", revoked_at=T0 - timedelta(hours=2)
            ),  # an old revocation, superseded
        ]
    )
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert [(r["namespace"], r["subject"]) for r in ch.inserted] == [("code-x-26-6", "alice")]


async def test_a_revoke_plans_every_branch_except_where_they_are_the_only_admin() -> None:
    ch = _FakeCH(
        [
            _admin("code-x-26-5", "alice"),
            _admin("code-x-26-5", "bob"),
            _admin("code-x-26-6", "alice"),  # alone there
        ]
    )
    (row,) = await maintainers.plan_revoke("clickhouse", "code-x", "alice", ch=ch)
    assert (row["namespace"], row["subject"], row["role"]) == ("code-x-26-5", "alice", "admin")
    assert row["revoked_at"] is not None
    assert ch.inserted == [], "planned, not written"
    await maintainers.write_revokes("clickhouse", "code-x", "alice", [row], ch=ch)
    assert ch.inserted == [row]


async def test_a_failed_plan_or_write_never_fails_the_revoke() -> None:
    class _Down(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            raise RuntimeError("clickhouse down")

        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            raise RuntimeError("clickhouse down")

    down = _Down([])
    assert await maintainers.plan_revoke("clickhouse", "code-x", "alice", ch=down) == []
    await maintainers.write_revokes("clickhouse", "code-x", "alice", [{"namespace": "n"}], ch=down)


async def test_sync_quietly_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(maintainers, "sync_repo_maintainers", boom)
    await maintainers.sync_quietly("clickhouse", "code-x")


# ── handler hooks ────────────────────────────────────────────────────────────


@pytest.fixture()
def _handler_env(
    monkeypatch: pytest.MonkeyPatch, grant_cutoffs: list[tuple[str, str, list[str]]]
) -> dict[str, Any]:
    calls: dict[str, Any] = {"sync": [], "revoke": [], "rows": [], "cutoffs": grant_cutoffs}

    async def _find(*_a: Any, **_k: Any) -> dict[str, Any] | None:
        return None

    async def _insert(row: dict[str, Any], **_k: Any) -> None:
        calls["rows"].append(row)

    async def _no_ceiling(*_a: Any, **_k: Any) -> None:
        return None

    async def _sync(org: str, ns: str, **_k: Any) -> None:
        calls["sync"].append((org, ns))

    async def _plan(org: str, ns: str, subject: str, **_k: Any) -> list[dict[str, Any]]:
        return [{"namespace": "code-x-26-5", "subject": subject}]

    async def _apply(
        org: str, ns: str, subject: str, rows: list[dict[str, Any]], **_k: Any
    ) -> None:
        calls["revoke"].append((org, ns, subject, [r["namespace"] for r in rows]))

    monkeypatch.setattr(grants_repo, "find_active_grant", _find)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(grants_mod, "_enforce_code_ceiling", _no_ceiling)
    monkeypatch.setattr(maintainers, "sync_quietly", _sync)
    monkeypatch.setattr(maintainers, "plan_revoke", _plan)
    monkeypatch.setattr(maintainers, "write_revokes", _apply)
    return calls


async def test_grants_create_admin_fans_out_and_viewer_does_not(
    _handler_env: dict[str, Any],
) -> None:
    await grants_mod.handle_grants_create(
        GrantsCreateInput(
            org="clickhouse", namespace="code-x", subject="alice", role="admin", granted_by="ops"
        )
    )
    await grants_mod.handle_grants_create(
        GrantsCreateInput(
            org="clickhouse", namespace="code-x", subject="dave", role="viewer", granted_by="ops"
        )
    )
    assert _handler_env["sync"] == [("clickhouse", "code-x")]


async def test_grants_approve_as_maintainer_fans_out(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _any(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {
                "subject": "alice",
                "role": "admin",
                "status": "pending",
                "pending_role": None,
                "granted_by": "alice",
            }
        )

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    await grants_mod.handle_grants_approve(
        GrantsApproveInput(
            org="clickhouse", namespace="code-x-26-5", subject="alice", decision="approve"
        )
    )
    assert _handler_env["sync"] == [("clickhouse", "code-x-26-5")]
    _handler_env["sync"].clear()
    await grants_mod.handle_grants_approve(
        GrantsApproveInput(
            org="clickhouse",
            namespace="code-x-26-5",
            subject="alice",
            decision="approve",
            role="viewer",
        )
    )
    assert _handler_env["sync"] == []


async def test_grants_revoke_admin_fans_out(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _any(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "alice", "role": "admin", "status": "active", "granted_by": "ops"}
        )

    class _CH:
        async def query_one(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return {"n": 2}

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    import server.core.ch as ch_mod

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    out = await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org="clickhouse", namespace="code-x", subject="alice")
    )
    assert out.revoked
    assert _handler_env["revoke"] == [("clickhouse", "code-x", "alice", ["code-x-26-5"])]
    # One cutoff write for the branch asked and every sibling, before any row.
    assert _handler_env["cutoffs"] == [
        ("clickhouse", "code-x", ["alice"]),
        ("clickhouse", "code-x-26-5", ["alice"]),
    ]


async def test_a_failed_cutoff_fails_a_maintainer_revoke_with_nothing_written(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cutoffs for every branch are written before any grant row, so a
    # failure leaves the grants standing and the revoke can be retried; a
    # sibling revoked without its cutoff could never get one.
    async def _any(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "alice", "role": "admin", "status": "active", "granted_by": "ops"}
        )

    class _CH:
        async def query_one(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return {"n": 2}

    async def _down(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("replica away")

    import server.core.ch as ch_mod

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(grants_mod, "end_grant_claims", _down)
    with pytest.raises(RuntimeError):
        await grants_mod.handle_grants_revoke(
            GrantsRevokeInput(org="clickhouse", namespace="code-x", subject="alice")
        )
    assert _handler_env["rows"] == [] and _handler_env["revoke"] == []
