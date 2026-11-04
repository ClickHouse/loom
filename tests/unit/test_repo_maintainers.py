"""A repository's maintainer set is one across its branches."""

from __future__ import annotations

import itertools
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

# A revoke reads no directory ties here (`conftest.no_directory_ties`).
pytestmark = pytest.mark.usefixtures("no_directory_ties")


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

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        if "count()" in sql:  # a branch's live admins
            live = [r for r in self.rows if r["namespace"] == params["ns"] and r["role"] == "admin"]
            return {"n": sum(r["revoked_at"] is None and r["status"] == "active" for r in live)}
        key = (params["ns"], params["sub"])
        return next((r for r in self.rows if (r["namespace"], r["subject"]) == key), None)

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.namespace_grants"
        self.inserted.extend(rows)


T0 = datetime(2026, 9, 17, 10, 0, 0)
_VERSIONS = itertools.count(1)


def _admin(
    ns: str,
    subject: str,
    *,
    at: datetime = T0,
    status: str = "active",
    revoked_at: datetime | None = None,
    role: str = "admin",
    version: int | None = None,
) -> dict[str, Any]:
    # Versions follow the change's time, ties broken by the order rows are made.
    event = revoked_at or at
    return {
        "namespace": ns,
        "subject": subject,
        "role": role,
        "status": status,
        "revoked_at": revoked_at,
        "granted_at": at,
        "granted_by": "ops",
        "version": version
        if version is not None
        else int(event.timestamp()) * 10**9 + next(_VERSIONS),
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


async def test_revoke_leaves_every_branch_except_where_they_are_the_only_admin() -> None:
    ch = _FakeCH(
        [
            _admin("code-x-26-5", "alice"),
            _admin("code-x-26-5", "bob"),
            _admin("code-x-26-6", "alice"),  # alone there
        ]
    )
    n = await maintainers.revoke_repo_maintainer("clickhouse", "code-x", "alice", ch=ch)
    assert n == 1
    (row,) = ch.inserted
    assert (row["namespace"], row["subject"], row["role"]) == ("code-x-26-5", "alice", "admin")
    assert row["revoked_at"] is not None


async def test_sync_quietly_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(maintainers, "sync_repo_maintainers", boom)
    await maintainers.sync_quietly("clickhouse", "code-x")


# ── handler hooks ────────────────────────────────────────────────────────────


@pytest.fixture()
def _handler_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {"sync": [], "revoke": [], "rows": [], "events": []}

    async def _find(*_a: Any, **_k: Any) -> dict[str, Any] | None:
        return None

    async def _insert(row: dict[str, Any], **_k: Any) -> None:
        calls["rows"].append(row)
        calls["events"].append("write")

    async def _no_ceiling(*_a: Any, **_k: Any) -> None:
        return None

    async def _sync(org: str, ns: str, **_k: Any) -> None:
        calls["sync"].append((org, ns))

    async def _revoke(org: str, ns: str, subject: str, **_k: Any) -> None:
        calls["revoke"].append((org, ns, subject))
        calls["events"].append("fan-out")

    monkeypatch.setattr(grants_repo, "find_active_grant", _find)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(grants_mod, "_enforce_code_ceiling", _no_ceiling)
    monkeypatch.setattr(maintainers, "sync_quietly", _sync)
    monkeypatch.setattr(maintainers, "revoke_repo_maintainer", _revoke)
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
    # The other branches first, the grant last.
    assert out.revoked and _handler_env["events"] == ["fan-out", "write"]
    assert set(_handler_env["revoke"]) == {("clickhouse", "code-x", "alice")}


async def test_an_admin_revoked_meanwhile_stops_the_persons_later_row(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # dave holds admin under his handle and his verified email, beside one other
    # admin who is revoked between dave's two rows: the first row stays revoked,
    # the second is refused, and the namespace keeps an admin.
    from server.core.errors import PreconditionFailedError
    from server.identity.app import token_grants

    async def _any(org: str, ns: str, sub: str, **_k: Any) -> GrantState | None:
        if sub in {"dave", "dave@x.io"} and sub not in {r["subject"] for r in _handler_env["rows"]}:
            return GrantState.from_row({"role": "admin", "status": "active", "granted_by": "ops"})
        return None

    async def _verified(org: str, subject: str, ch: Any) -> list[str]:
        return ["dave", "dave@x.io"]

    async def _admins(*_a: Any, **_k: Any) -> int:
        # dave's two rows and eve's; eve is revoked as soon as dave's first row goes.
        written = len(_handler_env["rows"])
        return 3 - written - (1 if written else 0)

    async def _no_cascade(*_a: Any, **_k: Any) -> int:
        return 0

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    monkeypatch.setattr("server.auth.aliases.read_aliases", _verified)
    monkeypatch.setattr(grants_repo, "count_active_admins", _admins)
    monkeypatch.setattr(token_grants, "cascade_revoke_to_created_tokens", _no_cascade)
    with pytest.raises(PreconditionFailedError, match="only admin"):
        await grants_mod.handle_grants_revoke(
            GrantsRevokeInput(org="clickhouse", namespace="code-x", subject="dave")
        )
    assert [r["subject"] for r in _handler_env["rows"]] == ["dave"]


@pytest.mark.parametrize(
    ("lowered_after_the_admin_grant", "mirrored"), [(True, False), (False, True)]
)
async def test_a_maintainer_lowered_on_one_branch_is_not_made_admin_there_again(
    lowered_after_the_admin_grant: bool, mirrored: bool
) -> None:
    # The lower role set on 26-5 after the admin grant on code-x stands; one
    # set before it is older than the admin grant, which reaches 26-5.
    lowered_at = T0 + timedelta(hours=2 if lowered_after_the_admin_grant else -2)
    ch = _FakeCH(
        [
            _admin("code-x", "alice"),
            _admin("code-x-26-5", "alice", role="writer", at=lowered_at),
            _admin("code-x-26-6", "alice"),
        ]
    )
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    written = [(r["namespace"], r["subject"]) for r in ch.inserted]
    assert written == ([("code-x-26-5", "alice")] if mirrored else [])


async def test_a_lowering_that_lands_while_a_sync_plans_is_not_written_over() -> None:
    class _Lowered(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            planned = await super().query_all(sql, params)
            # An admin lowers alice on 26-6 after the sync read the branches.
            self.rows = [*self.rows, _admin("code-x-26-6", "alice", role="viewer", at=T0)]
            return planned

    ch = _Lowered([_admin("code-x", "alice")])
    assert await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch) == 1
    assert [r["namespace"] for r in ch.inserted] == ["code-x-26-5"]


async def test_a_branch_that_cannot_be_claimed_leaves_every_branch_for_the_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import grant_claims
    from server.core.errors import GrantBusyError

    ch = _FakeCH([_admin(ns, s) for ns in ("code-x-26-5", "code-x-26-6") for s in ("alice", "bob")])
    held = grant_claims.grant_change

    def _busy_on_26_5(org: str, ns: str, subject: str) -> Any:
        if ns == "code-x-26-5":
            raise GrantBusyError("busy")
        return held(org, ns, subject)

    monkeypatch.setattr(grant_claims, "grant_change", _busy_on_26_5)
    with pytest.raises(GrantBusyError):
        await maintainers.revoke_repo_maintainer("clickhouse", "code-x", "alice", ch=ch)
    assert ch.inserted == [], "one insert for every branch, or none"


async def test_a_mirror_onto_a_new_branch_does_not_override_a_lowering_on_another() -> None:
    class _Applied(_FakeCH):
        async def insert(self, table: str, rows: list[dict[str, Any]], **k: Any) -> None:
            await super().insert(table, rows, **k)
            keys = {(r["namespace"], r["subject"]) for r in rows}
            self.rows = [r for r in self.rows if (r["namespace"], r["subject"]) not in keys]
            self.rows += rows

    # alice is admin on code-x; lowered to viewer on 26-5 after that; 26-6 is new.
    ch = _Applied(
        [
            _admin("code-x", "alice"),
            _admin("code-x-26-5", "alice", role="viewer", at=T0 + timedelta(hours=1)),
        ]
    )
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-x-26-6"]
    assert ch.inserted[0]["granted_at"] == T0, "the mirror keeps its source's time"


async def test_a_revoke_leaves_a_grant_given_again_after_it_planned() -> None:
    class _Regranted(_FakeCH):
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
            row = await super().query_one(sql, params)
            if row and params["ns"] == "code-x-26-6":
                # Granted again within the same second: only the version tells.
                return {**row, "version": row["version"] + 1}
            return row

    ch = _Regranted(
        [_admin(ns, s) for ns in ("code-x-26-5", "code-x-26-6") for s in ("alice", "bob")]
    )
    assert await maintainers.revoke_repo_maintainer("clickhouse", "code-x", "alice", ch=ch) == 1
    assert [r["namespace"] for r in ch.inserted] == ["code-x-26-5"]


async def test_a_failed_branch_fan_out_leaves_the_grant_live_for_the_retry(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import GrantBusyError

    async def _any(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "alice", "role": "admin", "status": "active", "granted_by": "ops"}
        )

    async def _two(*_a: Any, **_k: Any) -> int:
        return 2

    async def _busy(*_a: Any, **_k: Any) -> None:
        raise GrantBusyError("busy")

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    monkeypatch.setattr(grants_repo, "count_active_admins", _two)
    monkeypatch.setattr(maintainers, "revoke_repo_maintainer", _busy)
    with pytest.raises(GrantBusyError):
        await grants_mod.handle_grants_revoke(
            GrantsRevokeInput(org="clickhouse", namespace="code-x", subject="alice")
        )
    assert _handler_env["rows"] == [], "the grant itself is revoked last"


async def test_a_grant_made_admin_while_its_revoke_waits_is_fanned_out_first(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The revoke reads a pending request; an approval makes it admin before the claim.
    reads = iter(["pending", "active", "active", "active"])

    async def _row(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "alice", "role": "admin", "status": next(reads), "granted_by": "ops"}
        )

    async def _two(*_a: Any, **_k: Any) -> int:
        return 2

    monkeypatch.setattr(grants_repo, "find_any_grant", _row)
    monkeypatch.setattr(grants_repo, "count_active_admins", _two)
    out = await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org="clickhouse", namespace="code-x", subject="alice")
    )
    assert out.revoked and _handler_env["events"] == ["fan-out", "write"]
    assert [r["revoked_at"] is not None for r in _handler_env["rows"]] == [True]


async def test_a_mirror_is_not_written_once_its_source_was_revoked() -> None:
    class _RevokedSource(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            planned = await super().query_all(sql, params)
            # alice's revoke on code-x lands after the sync read the branches.
            self.rows = [_admin("code-x", "alice", revoked_at=T0 + timedelta(hours=1))]
            return planned

    ch = _RevokedSource([_admin("code-x", "alice")])
    assert await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch) == 0
    assert ch.inserted == []


async def test_one_subject_changes_one_repository_at_a_time_across_its_branches() -> None:
    import asyncio

    log: list[str] = []

    async def _hold(ns: str, subject: str, name: str, hold: float) -> None:
        async with maintainers.repo_change("clickhouse", ns, subject):
            log.append(f"{name} in")
            await asyncio.sleep(hold)
            log.append(f"{name} out")

    await asyncio.gather(
        _hold("code-x", "alice", "revoke", 0.05), _hold("code-x-26-5", "alice", "sync", 0)
    )
    assert log == ["revoke in", "revoke out", "sync in", "sync out"]
    log.clear()
    await asyncio.gather(_hold("code-x", "alice", "a", 0.05), _hold("code-y", "alice", "b", 0))
    assert log == ["a in", "b in", "b out", "a out"], "another repository does not wait"
    async with maintainers.repo_change("clickhouse", "team", "alice"):
        pass  # no repository: nothing held


def _recording_claims(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    """Record every grant claim taken and dropped, in order."""
    from contextlib import asynccontextmanager

    from server.auth import grant_claims

    held, held_admins = grant_claims.grant_change, grant_claims.admins_change

    @asynccontextmanager
    async def _recorded(org: str, ns: str, subject: str) -> Any:
        async with held(org, ns, subject):
            log.append(f"+{ns}")
            yield
            log.append(f"-{ns}")

    @asynccontextmanager
    async def _recorded_admins(org: str, ns: str) -> Any:
        async with held_admins(org, ns):
            log.append(f"+{ns}/admins")
            yield
            log.append(f"-{ns}/admins")

    for module in (grant_claims, grants_mod):
        monkeypatch.setattr(module, "grant_change", _recorded)
        monkeypatch.setattr(module, "admins_change", _recorded_admins)


async def test_a_sync_takes_the_repository_claim_before_the_branch_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    _recording_claims(monkeypatch, log)

    class _Logged(_FakeCH):
        async def insert(self, table: str, rows: list[dict[str, Any]], **k: Any) -> None:
            log.append(f"insert {sorted(r['namespace'] for r in rows)}")
            await super().insert(table, rows, **k)

    ch = _Logged([_admin("code-x", "alice")])
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert log == [
        "+repo:Org/x",
        "+code-x-26-5",
        "+code-x-26-6",
        "insert ['code-x-26-5', 'code-x-26-6']",
        "-code-x-26-6",
        "-code-x-26-5",
        "-repo:Org/x",
    ]


async def test_a_revoke_holds_the_repository_claim_across_its_fan_out_and_write(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    log: list[str] = []
    _recording_claims(monkeypatch, log)

    async def _any(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "alice", "role": "admin", "status": "active", "granted_by": "ops"}
        )

    async def _two(*_a: Any, **_k: Any) -> int:
        return 2

    async def _fan_out(*_a: Any, **_k: Any) -> None:
        log.append("fan-out")

    async def _insert(row: dict[str, Any], **_k: Any) -> None:
        log.append("write")

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    monkeypatch.setattr(grants_repo, "count_active_admins", _two)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(maintainers, "revoke_repo_maintainer", _fan_out)
    await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org="clickhouse", namespace="code-x", subject="alice")
    )
    # The repository's, then the namespace's admins from the last-admin count to the
    # write; the other branches first, the grant last.
    assert log == [
        "+repo:Org/x",
        "+repo:Org/x/admins",
        "+code-x/admins",
        "fan-out",
        "+code-x",
        "write",
        "-code-x",
        "-code-x/admins",
        "-repo:Org/x/admins",
        "-repo:Org/x",
    ]


async def test_a_subject_that_cannot_be_claimed_does_not_stop_a_sync_for_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import grant_claims
    from server.core.errors import GrantBusyError

    held = grant_claims.grant_change

    def _alice_busy(org: str, ns: str, subject: str) -> Any:
        if subject == "alice":
            raise GrantBusyError("busy")
        return held(org, ns, subject)

    monkeypatch.setattr(grant_claims, "grant_change", _alice_busy)
    ch = _FakeCH([_admin("code-x", "alice"), _admin("code-x", "bob")])
    with pytest.raises(GrantBusyError):
        await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert {(r["namespace"], r["subject"]) for r in ch.inserted} == {
        ("code-x-26-5", "bob"),
        ("code-x-26-6", "bob"),
    }


async def test_a_keeper_that_cannot_answer_stops_a_sync_at_the_first_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import grant_claims
    from server.core.errors import BackendUnavailableError

    tried: list[str] = []

    def _down(org: str, ns: str, subject: str) -> Any:
        tried.append(subject)
        raise BackendUnavailableError("keeper away")

    monkeypatch.setattr(grant_claims, "grant_change", _down)
    ch = _FakeCH([_admin("code-x", "alice"), _admin("code-x", "bob")])
    with pytest.raises(BackendUnavailableError):
        await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert len(set(tried)) == 1, "no wait per subject on a Keeper that is down"


async def test_a_promotion_in_the_same_second_as_a_lowering_elsewhere_reaches_it() -> None:
    # Lowered to viewer on 26-5, then made admin on code-x within the same second:
    # the promotion is the later change, and only the row version says so.
    ch = _FakeCH(
        [
            _admin("code-x-26-5", "alice", role="viewer", version=1_000),
            _admin("code-x", "alice", version=1_001),
        ]
    )
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert {r["namespace"] for r in ch.inserted} == {"code-x-26-5", "code-x-26-6"}


async def test_a_lowering_in_the_same_second_after_a_promotion_stands() -> None:
    ch = _FakeCH(
        [
            _admin("code-x", "alice", version=1_000),
            _admin("code-x-26-5", "alice", role="viewer", version=1_001),
        ]
    )
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-x-26-6"]


async def test_a_request_older_than_the_admin_grant_is_mirrored_over() -> None:
    ch = _FakeCH(
        [
            _admin("code-x-26-5", "alice", status="pending", version=1_000),
            _admin("code-x", "alice", version=1_001),
        ]
    )
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert {r["namespace"] for r in ch.inserted} == {"code-x-26-5", "code-x-26-6"}


async def test_a_request_after_a_revoke_on_a_branch_does_not_bring_admin_back() -> None:
    # Revoked on 26-5 after the admin grant on code-x, then asked for reader there:
    # the request replaced the revoke's row, and is respected as it was.
    ch = _FakeCH(
        [
            _admin("code-x", "alice", version=1_000),
            _admin("code-x-26-5", "alice", role="viewer", status="pending", version=1_002),
        ]
    )
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-x-26-6"]


async def test_a_maintainer_granted_on_a_branch_no_longer_configured_reaches_a_new_one() -> None:
    # Every row alice has is a mirror: one stands in as the source, onto a branch
    # she has no row on.
    mirror = {**_admin("code-x", "alice"), "granted_by": "repo-maintainers:code-x-26-4"}
    ch = _FakeCH([mirror])
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert {r["namespace"] for r in ch.inserted} == {"code-x-26-5", "code-x-26-6"}


async def test_a_mirror_alone_is_no_source_for_another_branch() -> None:
    # Lowered on the branch admin was given on; a mirror left elsewhere does not
    # carry it on to a branch that lacks it.
    mirrored = {**_admin("code-x-26-5", "alice"), "granted_by": "repo-maintainers:code-x"}
    ch = _FakeCH([_admin("code-x", "alice", role="viewer"), mirrored])
    assert await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch) == 0


async def test_a_mirror_only_maintainer_skips_just_the_branches_with_a_row_of_theirs() -> None:
    mirror = {**_admin("code-x", "alice"), "granted_by": "repo-maintainers:code-x-26-4"}
    ch = _FakeCH([mirror, _admin("code-x-26-6", "alice", role="viewer", at=T0 - timedelta(days=1))])
    await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-x-26-5"]


async def test_a_fan_out_keeps_a_branch_whose_other_admin_went_meanwhile() -> None:
    class _BobGoes(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            planned = await super().query_all(sql, params)
            # bob is revoked on 26-5 after the fan-out planned, before its claim.
            self.rows = [
                r for r in self.rows if (r["namespace"], r["subject"]) != ("code-x-26-5", "bob")
            ]
            self.rows.append(_admin("code-x-26-5", "bob", revoked_at=T0 + timedelta(hours=1)))
            return planned

    ch = _BobGoes(
        [_admin(ns, s) for ns in ("code-x-26-5", "code-x-26-6") for s in ("alice", "bob")]
    )
    assert await maintainers.revoke_repo_maintainer("clickhouse", "code-x", "alice", ch=ch) == 1
    assert [r["namespace"] for r in ch.inserted] == ["code-x-26-6"]


async def test_a_lowered_maintainer_is_lowered_on_every_other_branch() -> None:
    ch = _FakeCH([_admin(ns, s) for ns in ("code-x-26-5", "code-x-26-6") for s in ("alice", "bob")])
    assert (
        await maintainers.revoke_repo_maintainer(
            "clickhouse", "code-x", "alice", ch=ch, lowered_to="viewer"
        )
        == 2
    )
    assert {(r["namespace"], r["role"], r["revoked_at"]) for r in ch.inserted} == {
        ("code-x-26-5", "viewer", None),
        ("code-x-26-6", "viewer", None),
    }


async def test_two_maintainer_removals_in_one_repository_run_one_after_the_other(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    log: list[str] = []
    _recording_claims(monkeypatch, log)

    async def _any(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "x", "role": "admin", "status": "active", "granted_by": "ops"}
        )

    async def _three(*_a: Any, **_k: Any) -> int:
        return 3

    async def _slow_fan_out(*_a: Any, **_k: Any) -> None:
        await asyncio.sleep(0.02)

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    monkeypatch.setattr(grants_repo, "count_active_admins", _three)
    monkeypatch.setattr(maintainers, "revoke_repo_maintainer", _slow_fan_out)
    await asyncio.gather(
        *(
            grants_mod.handle_grants_revoke(
                GrantsRevokeInput(org="clickhouse", namespace=ns, subject=subject)
            )
            for ns, subject in (("code-x", "alice"), ("code-x-26-5", "bob"))
        )
    )
    held = [e for e in log if e.endswith("repo:Org/x/admins")]
    assert held == ["+repo:Org/x/admins", "-repo:Org/x/admins"] * 2
