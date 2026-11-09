"""A repository's maintainers flow across its branches."""

from __future__ import annotations

import itertools
import time
from datetime import datetime, timedelta
from typing import Any

import pytest

from server.code.app.index import maintainers, repo_registry
from server.code.domain.index import repos
from server.core.errors import GrantBusyError
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
    def __init__(
        self,
        org: str,
        namespace: str,
        repo: str,
        source: str = "config",
        classification: str = "standard",
    ) -> None:
        self.org, self.namespace, self.repo, self.source = org, namespace, repo, source
        self.classification = classification


REPOS = [
    _Cfg("clickhouse", "code-x", "Org/x"),
    _Cfg("clickhouse", "code-x-26-5", "Org/x"),
    _Cfg("clickhouse", "code-x-26-6", "Org/x"),
    _Cfg("clickhouse", "code-y", "Org/y"),
]


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.restricted: set[str] = set()  # by their directory rows
        self.inserted: list[dict[str, Any]] = []
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append(sql)
        wanted = set(params["ns"])
        if "loom.namespaces" in sql:
            return [
                {"namespace": ns, "classification": "restricted"}
                for ns in params["ns"]
                if ns in self.restricted
            ]
        return [r for r in self.rows if r["namespace"] in wanted]

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        if "startsWith(granted_by" in sql:  # a subject's given operator admin grants
            given = [
                r
                for r in self.rows
                if r["namespace"] in params["ns"]
                and r["subject"] == params["sub"]
                and not r["granted_by"].startswith(params["prefix"])
            ]
            return {"n": sum(_live_admin_row(r) for r in given)}
        if "count()" in sql:  # a branch's live admins
            return {
                "n": sum(_live_admin_row(r) for r in self.rows if r["namespace"] == params["ns"])
            }
        key = (params["ns"], params["sub"])
        return next((r for r in self.rows if (r["namespace"], r["subject"]) == key), None)

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.namespace_grants"
        self.inserted.extend(rows)


def _live_admin_row(r: dict[str, Any]) -> bool:
    return r["role"] == "admin" and r["revoked_at"] is None and r["status"] == "active"


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
    monkeypatch.setattr(repo_registry, "registry_read", lambda: True)
    maintainers.reset_for_tests()

    async def _refresh(**_k: Any) -> list[Any]:
        return []

    async def _read(_ch: Any = None) -> bool:
        return True

    monkeypatch.setattr(repo_registry, "refresh_db_repos", _refresh)
    monkeypatch.setattr(repo_registry, "read_registry", _read)


def _revoke(namespace: str, subject: str, **k: Any) -> Any:
    """A fan-out under the repository `read_repo` would answer for `namespace`."""
    repo = maintainers.repo_of("clickhouse", namespace)
    return maintainers.revoke_repo_maintainer("clickhouse", namespace, subject, repo=repo, **k)


def test_branches_groups_by_repository() -> None:
    assert list(maintainers._branches("clickhouse", "Org/x")) == [
        "code-x",
        "code-x-26-5",
        "code-x-26-6",
    ]
    assert maintainers.repo_of("clickhouse", "code-x-26-5") == "Org/x"
    assert maintainers._branches("clickhouse", "Org/y") == {"code-y": "config"}
    assert maintainers.repo_of("clickhouse", "not-a-repo") is None
    assert maintainers.repo_of("other-org", "code-x") is None


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
    n = await _revoke("code-x", "alice", ch=ch)
    assert n == 1
    (row,) = ch.inserted
    assert (row["namespace"], row["subject"], row["role"]) == ("code-x-26-5", "alice", "admin")
    assert row["revoked_at"] is not None


# An operator entry and a self-service entry of one repository.
MIXED = [
    _Cfg("clickhouse", "code-z", "Org/z"),
    _Cfg("clickhouse", "code-z-dev", "Org/z", source="registry"),
    _Cfg("clickhouse", "code-z-26-5", "Org/z", source="registry"),
]


async def test_a_self_service_branch_admin_is_never_mirrored_onto_an_operator_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    ch = _FakeCH([_admin("code-z", "ops"), _admin("code-z-dev", "mallory")])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z-dev", ch=ch)
    written = sorted((r["namespace"], r["subject"]) for r in ch.inserted)
    # The operator's admin reaches the self-service branches; the creator of one
    # reaches the other self-service branch and never the operator's entry.
    assert written == [
        ("code-z-26-5", "mallory"),
        ("code-z-26-5", "ops"),
        ("code-z-dev", "ops"),
    ]


async def test_a_mirror_on_a_self_service_branch_never_stands_in_for_an_operator_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    mirror = _admin("code-z-dev", "mallory") | {"granted_by": "repo-maintainers:code-gone"}
    ch = _FakeCH([_admin("code-z", "ops"), mirror])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert ("code-z", "mallory") not in {(r["namespace"], r["subject"]) for r in ch.inserted}


async def test_a_revoke_on_a_self_service_branch_never_reaches_an_operator_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    rows = [_admin(ns, s) for ns in ("code-z", "code-z-dev", "code-z-26-5") for s in ("ops", "x")]
    ch = _FakeCH(rows)
    await _revoke("code-z-dev", "ops", ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-z-26-5"]
    # From the operator entry, the mirrors of its admin leave both other branches.
    mirrored = {"granted_by": "repo-maintainers:code-z"}
    rows = [
        r | mirrored if r["subject"] == "ops" and r["namespace"] != "code-z" else r for r in rows
    ]
    ch = _FakeCH(rows)
    await _revoke("code-z", "ops", ch=ch)
    assert sorted(r["namespace"] for r in ch.inserted) == ["code-z-26-5", "code-z-dev"]


async def test_an_operator_revoke_takes_only_its_mirrors_off_a_self_service_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    leaked = _admin("code-z", "mallory") | {"granted_by": "repo-maintainers:code-z-dev"}
    ops = [_admin(ns, "ops") for ns in ("code-z", "code-z-dev", "code-z-26-5")]
    among = _admin("code-z-26-5", "mallory") | {"granted_by": "repo-maintainers:code-z-dev"}
    ch = _FakeCH([leaked, _admin("code-z-dev", "mallory"), among, *ops])
    # The grant given on the branch mallory added stays, and so does its mirror
    # among the branches added from the page.
    assert await _revoke("code-z", "mallory", ch=ch) == 0
    ours = _admin("code-z-26-5", "carol") | {"granted_by": "repo-maintainers:code-z"}
    mirrored = _admin("code-z-dev", "carol") | {"granted_by": "repo-maintainers:code-z"}
    ch = _FakeCH([_admin("code-z", "carol"), ours, mirrored, *ops])
    await _revoke("code-z", "carol", ch=ch)
    assert sorted(r["namespace"] for r in ch.inserted) == ["code-z-26-5", "code-z-dev"]


async def test_an_operator_revoke_keeps_every_self_service_branch_of_a_page_maintainer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # mallory added code-z-dev; a newer operator grant on code-z was what reached
    # code-z-26-5. Her own grant still reaches it, so the operator revoke keeps it.
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    ops = [_admin(ns, "ops") for ns in ("code-z", "code-z-dev", "code-z-26-5")]
    via_op = _admin("code-z-26-5", "mallory") | {"granted_by": "repo-maintainers:code-z"}
    ch = _FakeCH([_admin("code-z", "mallory"), _admin("code-z-dev", "mallory"), via_op, *ops])
    assert await _revoke("code-z", "mallory", ch=ch) == 0


async def test_a_lowering_on_a_self_service_branch_never_reaches_an_operator_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    rows = [_admin(ns, s) for ns in ("code-z", "code-z-dev", "code-z-26-5") for s in ("ops", "x")]
    ch = _FakeCH(rows)
    await _revoke("code-z-dev", "ops", ch=ch, lowered_to="writer")
    assert [(r["namespace"], r["role"]) for r in ch.inserted] == [("code-z-26-5", "writer")]


# A restricted branch added from the page, beside a standard one any org member may add.
PRIVATE = [
    _Cfg("clickhouse", "code-z", "Org/z"),
    _Cfg("clickhouse", "code-z-dev", "Org/z", source="registry"),
    _Cfg("clickhouse", "code-z-26-5", "Org/z", source="registry", classification="restricted"),
]


def _copy(ns: str, subject: str, source: str) -> dict[str, Any]:
    return _admin(ns, subject) | {"granted_by": f"repo-maintainers:{source}"}


def _on(ch: _FakeCH, ns: str) -> set[str]:
    """The subjects the sync made admin on `ns`."""
    return {r["subject"] for r in ch.inserted if r["namespace"] == ns and r["revoked_at"] is None}


@pytest.mark.parametrize("by", ["snapshot", "directory row"])
async def test_a_restricted_page_branch_takes_only_the_operators_admins(
    monkeypatch: pytest.MonkeyPatch, by: str
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE if by == "snapshot" else MIXED)
    ch = _FakeCH([_admin("code-z", "ops"), _admin("code-z-dev", "mallory")])
    ch.restricted = {"code-z-26-5"} if by == "directory row" else set()
    await maintainers.sync_repo_maintainers("clickhouse", "code-z-dev", ch=ch)
    assert _on(ch, "code-z-26-5") == {"ops"}
    assert _on(ch, "code-z-dev") == {"ops"}


async def test_a_sync_revokes_the_copies_a_restricted_page_branch_must_not_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Copies made while it was standard go, even the branch's last admin; a copy of an
    operator admin and a grant given there stay."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    rows = [
        _admin("code-z", "ops"),
        _admin("code-z-dev", "mallory"),
        _copy("code-z-26-5", "mallory", "code-z-dev"),
        _copy("code-z-26-5", "carol", "code-z-gone"),
        _copy("code-z-26-5", "ops", "code-z"),
        _admin("code-z-26-5", "alice"),
    ]
    ch = _FakeCH(rows)
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    revoked = sorted(
        (r["namespace"], r["subject"]) for r in ch.inserted if r["revoked_at"] is not None
    )
    assert revoked == [("code-z-26-5", "carol"), ("code-z-26-5", "mallory")]
    ch = _FakeCH([_admin("code-z-dev", "mallory"), _copy("code-z-26-5", "mallory", "code-z-dev")])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z-dev", ch=ch)
    assert [(r["subject"], r["revoked_at"] is not None) for r in ch.inserted] == [("mallory", True)]


async def test_a_lone_restricted_page_branch_is_cleaned_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lone = [_Cfg("clickhouse", "code-z-26-5", "Org/z", "registry", "restricted")]
    monkeypatch.setattr(repos, "configured_repos", lambda: lone)
    ch = _FakeCH([_admin("code-z-26-5", "alice"), _copy("code-z-26-5", "mallory", "code-z-dev")])
    assert await maintainers.sync_repo_maintainers("clickhouse", "code-z-26-5", ch=ch) == 1
    assert [(r["subject"], r["revoked_at"] is not None) for r in ch.inserted] == [("mallory", True)]


async def test_a_copy_relayed_by_an_operator_entry_s_stand_in_goes_once_restricted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mallory's copy on code-z stands in for a removed page branch, and a sync wrote
    it onto code-z-26-5 while that was standard, named after code-z. Restricted, the
    branch does not keep it: mallory holds no operator admin grant there."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    ch = _FakeCH(
        [
            _admin("code-z", "ops"),
            _copy("code-z", "mallory", "code-z-gone"),
            _copy("code-z-26-5", "mallory", "code-z"),
            _copy("code-z-26-5", "ops", "code-z"),
        ]
    )
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    on_it = [r for r in ch.inserted if r["namespace"] == "code-z-26-5"]
    assert [(r["subject"], r["revoked_at"] is not None) for r in on_it] == [("mallory", True)]


async def test_a_busy_rewrite_does_not_stop_the_copies(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import grant_claims

    held = grant_claims.grant_change

    def _ops_busy(org: str, ns: str, subject: str) -> Any:
        if subject == "ops":
            raise GrantBusyError("busy")
        return held(org, ns, subject)

    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    monkeypatch.setattr(grant_claims, "grant_change", _ops_busy)
    rows = [_admin("code-z", "ops"), _admin("code-z", "carol")]
    ch = _FakeCH([*rows, _copy("code-z-26-5", "ops", "code-z-dev")])
    with pytest.raises(GrantBusyError):
        await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert _on(ch, "code-z-26-5") == {"carol"}


async def test_a_stray_whose_rewrite_is_refused_is_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ops's operator grant is revoked after the plan: the stray is not rewritten from
    it, and does not stay either. Its revoke is versioned at the plan's read, so an
    operator grant made after that read stays newer and the next sync copies it."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    revoked = _admin("code-z", "ops", revoked_at=T0 + timedelta(hours=1))
    after_plan: list[int] = []

    class _RevokedMeanwhile(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            planned = await super().query_all(sql, params)
            if "loom.namespaces" not in sql:
                self.rows = [revoked, *self.rows[1:]]
                after_plan.append(time.time_ns())
            return planned

    stray = _copy("code-z-26-5", "ops", "code-z-dev")
    ch = _RevokedMeanwhile([_admin("code-z", "ops"), stray])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert [(r["granted_by"], r["revoked_at"] is not None) for r in ch.inserted] == [
        ("repo-maintainers:code-z-dev", True)
    ]
    assert ch.inserted[0]["version"] < after_plan[0]


async def test_a_refused_rewrite_leaves_the_stray_to_another_operator_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ops is admin on two operator entries. The one the rewrite copies is revoked after
    the plan: the stray is left, and the next sync rewrites it from the other one."""
    entries = [
        _Cfg("clickhouse", "code-z", "Org/z"),
        _Cfg("clickhouse", "code-z-26-4", "Org/z"),
        _Cfg("clickhouse", "code-z-dev", "Org/z", source="registry"),
        _Cfg("clickhouse", "code-z-26-5", "Org/z", source="registry", classification="restricted"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    older = _admin("code-z-26-4", "ops")
    newer = _admin("code-z", "ops")
    revoked = newer | {"revoked_at": T0 + timedelta(hours=1), "version": newer["version"] + 10}

    class _RevokedMeanwhile(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            planned = await super().query_all(sql, params)
            if "loom.namespaces" not in sql and newer in self.rows:
                self.rows = [revoked if r is newer else r for r in self.rows]
            return planned

    stray = _copy("code-z-26-5", "ops", "code-z-dev")
    ch = _RevokedMeanwhile([newer, older, stray])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert ch.inserted == []
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert [
        (r["granted_by"], r["revoked_at"]) for r in ch.inserted if r["namespace"] == "code-z-26-5"
    ] == [("repo-maintainers:code-z-26-4", None)]


async def test_a_sync_plans_from_sequentially_consistent_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lagging replica could miss an operator grant just made, and the stray's revoke
    planned from it would be newer than that grant."""
    from server.core.ch import sequential_reads_active

    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    reads: list[bool] = []

    class _Recorded(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            reads.append(sequential_reads_active())
            return await super().query_all(sql, params)

    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=_Recorded([]))
    assert reads and all(reads)


def test_a_stray_s_revoke_outranks_a_stray_written_after_the_read() -> None:
    stray = _copy("code-z-26-5", "mallory", "code-z-dev") | {"version": 2_000}
    assert maintainers._revoke_row("clickhouse", stray, read_at=1_000)["version"] == 2_001
    assert maintainers._revoke_row("clickhouse", stray, read_at=5_000)["version"] == 5_000


async def test_a_stray_s_revoke_is_older_than_an_operator_grant_made_after_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next sync then copies that grant onto the branch: the revoke does not end it."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    granted: list[dict[str, Any]] = []

    class _GrantedMeanwhile(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "loom.namespaces" in sql and not granted:
                granted.append(_admin("code-z", "mallory", version=time.time_ns()))
                self.rows = [*self.rows, *granted]
            return await super().query_all(sql, params)

    ch = _GrantedMeanwhile([_admin("code-z", "ops"), _copy("code-z-26-5", "mallory", "code-z-dev")])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    (revoke,) = [r for r in ch.inserted if r["namespace"] == "code-z-26-5" and r["revoked_at"]]
    assert revoke["version"] < granted[0]["version"]


async def test_a_stray_whose_operator_grant_was_only_given_again_is_left_for_the_next_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A revoke written now would be newer than ops's new grant, and keep them off for
    good."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    again = _admin("code-z", "ops", at=T0 + timedelta(hours=1))

    class _GivenAgain(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            planned = await super().query_all(sql, params)
            if "loom.namespaces" not in sql:
                self.rows = [again, *self.rows[1:]]
            return planned

    ch = _GivenAgain([_admin("code-z", "ops"), _copy("code-z-26-5", "ops", "code-z-dev")])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert [r for r in ch.inserted if r["namespace"] == "code-z-26-5"] == []


async def test_a_page_copy_an_operator_entry_kept_never_reaches_a_restricted_page_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator entry that adopted a page namespace keeps its copies; their page
    source gone or not, none is a source for a restricted branch."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    for source in ("code-z-dev", "code-z-gone"):
        ch = _FakeCH([_admin("code-z", "ops"), _copy("code-z", "mallory", source)])
        await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
        assert _on(ch, "code-z-26-5") == {"ops"}, source


async def test_a_copy_planned_before_a_restriction_is_not_written_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)

    class _RestrictedMeanwhile(_FakeCH):
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
            self.restricted = {"code-z-26-5"}  # lands once the plan was made
            return await super().query_one(sql, params)

    ch = _RestrictedMeanwhile([_admin("code-z", "ops"), _admin("code-z-dev", "mallory")])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z-dev", ch=ch)
    assert _on(ch, "code-z-26-5") == {"ops"}
    # A stand-in on the operator entry is written under its name: read again too.
    stand_in = _copy("code-z", "mallory", "code-z-gone")
    ch = _RestrictedMeanwhile([_admin("code-z", "ops"), stand_in])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert _on(ch, "code-z-26-5") == {"ops"}


async def test_a_copy_is_traced_across_operator_entries_to_a_grant_of_its_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy named after code-z, whose row there is a copy of a grant given on
    code-z-ops, stays; a copy above the role of the grant it leads to goes."""
    two = [_Cfg("clickhouse", "code-z-ops", "Org/z"), *PRIVATE]
    monkeypatch.setattr(repos, "configured_repos", lambda: two)
    rows = [
        _admin("code-z-ops", "ops"),
        _copy("code-z", "ops", "code-z-ops"),
        _copy("code-z-26-5", "ops", "code-z"),
        _admin("code-z-ops", "carol", role="viewer"),
        _copy("code-z", "carol", "code-z-ops") | {"role": "viewer"},
        _copy("code-z-26-5", "carol", "code-z") | {"role": "writer"},
    ]
    ch = _FakeCH(rows)
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    on_it = [r for r in ch.inserted if r["namespace"] == "code-z-26-5"]
    assert [(r["subject"], r["revoked_at"] is not None) for r in on_it] == [("carol", True)]


async def test_a_lowering_from_an_operator_entry_stands_on_a_restricted_page_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    ch = _FakeCH(
        [
            _admin("code-z", "ops", role="writer"),
            _admin("code-z", "alice"),
            _copy("code-z-26-5", "ops", "code-z") | {"role": "writer"},
            _copy("code-z-26-5", "alice", "code-z"),
        ]
    )
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert [r for r in ch.inserted if r["namespace"] == "code-z-26-5"] == []


async def test_an_unreadable_classification_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)

    class _NoDirectory(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "loom.namespaces" in sql:
                raise ConnectionError("ClickHouse is not answering")
            return await super().query_all(sql, params)

    ch = _NoDirectory([_admin("code-z", "ops"), _admin("code-z-dev", "mallory")])
    with pytest.raises(ConnectionError):
        await maintainers.sync_repo_maintainers("clickhouse", "code-z-dev", ch=ch)
    rows = [_admin(ns, s) for ns in ("code-z-dev", "code-z-26-5") for s in ("alice", "bob")]
    ch = _NoDirectory(rows)
    with pytest.raises(ConnectionError):  # not skipped: it may be a standard one
        await _revoke("code-z-dev", "alice", ch=ch)
    assert ch.inserted == []


async def test_a_page_revoke_takes_off_a_restricted_page_branch_only_a_stray_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant given there and a copy of an operator admin stay; a copy from a page
    branch goes, even as the branch's last admin."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    rows = [_admin(ns, s) for ns in ("code-z-dev", "code-z-26-5") for s in ("alice", "bob")]
    assert await _revoke("code-z-dev", "alice", ch=_FakeCH(rows)) == 0
    rows = [_admin("code-z-dev", "alice"), _copy("code-z-26-5", "alice", "code-z")]
    assert await _revoke("code-z-dev", "alice", ch=_FakeCH(rows)) == 0
    ch = _FakeCH([_admin("code-z-dev", "mallory"), _copy("code-z-26-5", "mallory", "code-z-dev")])
    assert await _revoke("code-z-dev", "mallory", ch=ch) == 1
    assert [(r["namespace"], r["revoked_at"] is not None) for r in ch.inserted] == [
        ("code-z-26-5", True)
    ]


async def test_an_operator_revoke_leaves_a_stray_of_an_admin_of_another_entry_for_the_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """alice stays admin of code-z-ops: the next sync copies that grant over her stray,
    which a revoke now would end for good."""
    two = [_Cfg("clickhouse", "code-z-ops", "Org/z"), *PRIVATE]
    monkeypatch.setattr(repos, "configured_repos", lambda: two)
    rows = [
        _admin("code-z", "alice"),
        _admin("code-z", "x"),
        _admin("code-z-ops", "alice"),
        _copy("code-z-26-5", "alice", "code-z-dev"),
    ]
    ch = _FakeCH(rows)
    await _revoke("code-z", "alice", ch=ch)
    assert "code-z-26-5" not in {r["namespace"] for r in ch.inserted}


@pytest.mark.parametrize("lowered_to", [None, "writer"])
async def test_an_operator_revoke_takes_the_stray_of_an_admin_of_that_entry_alone(
    monkeypatch: pytest.MonkeyPatch, lowered_to: str | None
) -> None:
    """The grant revoked is still live while the fan-out runs: it does not count as one
    the subject keeps."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    rows = [
        _admin("code-z", "alice"),
        _admin("code-z", "x"),
        _copy("code-z-26-5", "alice", "code-z-dev"),
    ]
    ch = _FakeCH(rows)
    await _revoke("code-z", "alice", ch=ch, lowered_to=lowered_to)
    assert [(r["namespace"], r["revoked_at"] is not None) for r in ch.inserted] == [
        ("code-z-26-5", True)
    ]


async def test_an_operator_admin_s_copy_from_a_page_branch_becomes_a_copy_of_the_operator_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ops added code-z-dev after their operator grant, so their copy came from it:
    the restricted branch keeps them, now as a copy of the operator entry's grant."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    rows = [
        _admin("code-z", "ops"),
        _admin("code-z-dev", "ops", at=T0 + timedelta(hours=1)),
        _copy("code-z-26-5", "ops", "code-z-dev"),
    ]
    ch = _FakeCH(rows)
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert [(r["namespace"], r["granted_by"], r["revoked_at"]) for r in ch.inserted] == [
        ("code-z-26-5", "repo-maintainers:code-z", None)
    ]
    # Lowered there after their operator grant: admin is not copied back, and the
    # lowered stray copy is revoked.
    lowered = _copy("code-z-26-5", "ops", "code-z-dev") | {"role": "writer"}
    ch = _FakeCH([*rows[:2], lowered])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert [(r["role"], r["revoked_at"] is not None) for r in ch.inserted] == [("writer", True)]


async def test_an_operator_revoke_takes_only_a_copy_off_a_restricted_page_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """alice's grant on her own page branch does not reach the restricted one, so the
    copy of her operator grant there goes; a grant given there stays."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    rows = [
        _admin("code-z", "alice"),
        _admin("code-z", "ops"),
        _admin("code-z-dev", "alice"),
        _admin("code-z-dev", "bob"),
        _copy("code-z-26-5", "alice", "code-z"),
        _admin("code-z-26-5", "bob"),
    ]
    ch = _FakeCH(rows)
    await _revoke("code-z", "alice", ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-z-26-5"]
    ch = _FakeCH([*rows[:4], _admin("code-z-26-5", "alice"), _admin("code-z-26-5", "bob")])
    assert await _revoke("code-z", "alice", ch=ch) == 0


async def test_a_page_revoke_leaves_an_operator_admin_s_stray_for_the_sync_to_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    rows = [
        _admin("code-z", "ops"),
        _admin("code-z-dev", "ops"),
        _admin("code-z-dev", "bob"),
        _copy("code-z-26-5", "ops", "code-z-dev"),
    ]
    assert await _revoke("code-z-dev", "ops", ch=_FakeCH(rows)) == 0


_SOURCES = [("config", "standard"), ("registry", "standard"), ("registry", "restricted")]


@pytest.mark.parametrize(
    "kinds", list(itertools.product(_SOURCES, repeat=3)), ids=lambda k: "-".join(map(str, k))
)
async def test_no_sync_or_revoke_leaves_a_restricted_page_branch_a_copy_from_a_page_branch(
    monkeypatch: pytest.MonkeyPatch, kinds: tuple[tuple[str, str], ...]
) -> None:
    """Every mix of three branches, each with its own admin, a copy of every other
    branch's and a lowered one: after a sync, or a revoke or a lowering from any
    branch, a restricted branch added from the page holds no live copy, of any role,
    whose source is not an operator entry."""
    names = [f"code-z-{i}" for i in range(3)]
    cfgs = [_Cfg("clickhouse", ns, "Org/z", s, c) for ns, (s, c) in zip(names, kinds, strict=True)]
    monkeypatch.setattr(repos, "configured_repos", lambda: cfgs)
    rows = [_admin(ns, f"own-{ns}") for ns in names]
    rows += [_copy(ns, f"own-{src}", src) for ns in names for src in names if src != ns]
    rows += [
        _copy(ns, f"low-{src}", src) | {"role": "writer"}
        for ns in names
        for src in names
        if src != ns
    ]
    restricted = {
        c.namespace for c in cfgs if c.source == "registry" and c.classification == "restricted"
    }
    operator = {c.namespace for c in cfgs if c.source == "config"}

    def strays(written: list[dict[str, Any]], subject: str = "") -> list[tuple[str, str]]:
        final = {(r["namespace"], r["subject"]): r for r in [*rows, *written]}
        return [
            (ns, r["granted_by"])
            for (ns, who), r in final.items()
            if ns in restricted
            and subject in ("", who)
            and r["revoked_at"] is None
            and r["granted_by"].startswith("repo-")
            and r["granted_by"].removeprefix("repo-maintainers:") not in operator
        ]

    ch = _FakeCH(rows)
    await maintainers.sync_repo_maintainers("clickhouse", names[0], ch=ch)
    assert strays(ch.inserted) == []
    for origin in names:
        for subject in (f"own-{origin}", f"low-{origin}"):
            for lowered_to in (None, "writer"):
                ch = _FakeCH(rows)
                await _revoke(origin, subject, ch=ch, lowered_to=lowered_to)
                assert strays(ch.inserted, subject) == [], (origin, subject, lowered_to)


def _code_rows(monkeypatch: pytest.MonkeyPatch, code: set[str]) -> list[str]:
    """Directory rows: `code` are code indexes, any other namespace a memory one.
    Returns the namespaces whose row was read."""
    from server.identity.app import registration

    read: list[str] = []

    async def _row(org: str, ns: str, **_k: Any) -> dict[str, Any] | None:
        from server.core.ch import sequential_reads_active

        assert sequential_reads_active(), "a lagging replica would skip the fan-out"
        read.append(ns)
        return {"kind": "code" if ns in code else "memory"}

    monkeypatch.setattr(registration, "namespace_row", _row)
    return read


async def test_no_revoke_fan_out_while_the_self_service_registry_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _failed(_ch: Any = None) -> bool:
        return False  # a read failed, after an earlier one succeeded or not

    monkeypatch.setattr(repo_registry, "read_registry", _failed)
    _code_rows(monkeypatch, {"code-y-dev"})
    with pytest.raises(GrantBusyError):
        await maintainers.read_repo("clickhouse", "code-x", ch=object())
    # One operator entry: the branches added from the page are all it cannot see.
    with pytest.raises(GrantBusyError):
        await maintainers.read_repo("clickhouse", "code-y", ch=object())
    # A branch added from the page that the snapshot does not list yet.
    with pytest.raises(GrantBusyError):
        await maintainers.read_repo("clickhouse", "code-y-dev", ch=object())
    assert await maintainers.read_repo("clickhouse", "team-kb", ch=object()) is None


async def test_a_revoke_off_a_code_namespace_reads_no_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[bool] = []

    async def _read(_ch: Any = None) -> bool:
        reads.append(True)
        return True

    monkeypatch.setattr(repo_registry, "read_registry", _read)
    rows = _code_rows(monkeypatch, set())
    assert await maintainers.read_repo("clickhouse", "team-kb", ch=object()) is None
    assert (rows, reads) == (["team-kb"], [])
    assert await maintainers.read_repo("clickhouse", "code-x", ch=object()) == "Org/x"
    assert reads == [True]


async def test_a_revoke_on_a_process_that_never_read_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unread, the snapshot refuses: a memory namespace is told by its directory row and
    # reads no registry; a code namespace reads it, and answers busy while that fails.
    monkeypatch.setattr(repos, "_registry_loaded", False)
    monkeypatch.setattr(repo_registry, "registry_read", lambda: repos._registry_loaded)
    reads: list[bool] = []

    async def _failed(_ch: Any = None) -> bool:
        reads.append(True)
        return False

    monkeypatch.setattr(repo_registry, "read_registry", _failed)
    _code_rows(monkeypatch, {"code-x"})
    assert await maintainers.read_repo("clickhouse", "team-kb", ch=object()) is None
    assert reads == []
    with pytest.raises(GrantBusyError):
        await maintainers.read_repo("clickhouse", "code-x", ch=object())
    assert reads == [True]
    # The revoke itself, on that memory namespace: nothing to fan out, nothing refused.
    assert (
        await maintainers.revoke_repo_maintainer(
            "clickhouse", "team-kb", "alice", repo=None, ch=object()
        )
        == 0
    )
    # An operator entry whose directory row is not (yet) code: its configuration says so.
    monkeypatch.setattr(repos, "config_entry", lambda org, ns: object() if ns == "code-y" else None)
    with pytest.raises(GrantBusyError):
        await maintainers.read_repo("clickhouse", "code-y", ch=object())
    assert reads == [True, True]


async def test_a_branch_added_after_read_repo_is_reached_by_the_revoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.ch import sequential_reads_active

    # code-x-dev is added, and given alice's copy, after `read_repo` read the
    # registry and before the revoke held the repository's claims.
    known = [*REPOS]
    consistent: list[bool] = []

    async def _read(_ch: Any = None) -> bool:
        consistent.append(sequential_reads_active())
        if len(consistent) == 2:
            known.append(_Cfg("clickhouse", "code-x-dev", "Org/x", source="registry"))
        return True

    monkeypatch.setattr(repo_registry, "read_registry", _read)
    monkeypatch.setattr(repos, "configured_repos", lambda: known)
    repo = await maintainers.read_repo("clickhouse", "code-x", ch=object())
    mirror = {"granted_by": "repo-maintainers:code-x"}
    ch = _FakeCH(
        [
            _admin("code-x", "alice"),
            _admin("code-x-dev", "alice") | mirror,
            _admin("code-x-dev", "bob"),
        ]
    )
    await maintainers.revoke_repo_maintainer("clickhouse", "code-x", "alice", repo=repo, ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-x-dev"]
    assert consistent == [True, True], "both reads see every replica's writes"


async def test_a_revoke_answers_busy_when_the_registry_read_under_its_claims_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _failed(_ch: Any = None) -> bool:
        return False

    monkeypatch.setattr(repo_registry, "read_registry", _failed)
    ch = _FakeCH([_admin(ns, s) for ns in ("code-x-26-5", "code-x-26-6") for s in ("alice", "bob")])
    with pytest.raises(GrantBusyError):
        await _revoke("code-x", "alice", ch=ch)
    assert ch.inserted == []


async def test_a_revoke_whose_repository_changed_after_its_claims_answers_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The claims were taken before the snapshot listed code-x-dev.
    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [*REPOS, _Cfg("clickhouse", "code-x-dev", "Org/x", source="registry")],
    )
    ch = _FakeCH([_admin(ns, s) for ns in ("code-x", "code-x-dev") for s in ("alice", "bob")])
    with pytest.raises(GrantBusyError):
        await maintainers.revoke_repo_maintainer(
            "clickhouse", "code-x-dev", "alice", repo=None, ch=ch
        )
    assert ch.inserted == []


async def test_an_operator_revoke_keeps_a_mirror_that_is_a_page_branch_s_only_admin(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    mirror = {"granted_by": "repo-maintainers:code-z"}
    ch = _FakeCH(
        [
            _admin("code-z", "ops"),
            _admin("code-z", "x"),
            _admin("code-z-dev", "ops") | mirror,
            _admin("code-z-26-5", "ops") | mirror,
            _admin("code-z-26-5", "x") | mirror,
        ]
    )
    with caplog.at_level("WARNING", logger=maintainers.log.name):
        await _revoke("code-z", "ops", ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-z-26-5"]
    assert "repo maintainers: clickhouse/code-z-dev keeps 'ops' — only admin of that branch" in [
        r.message for r in caplog.records
    ]


async def test_a_copy_an_operator_revoke_keeps_as_a_restricted_branch_s_last_admin_goes_at_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The revoke keeps the branch's last admin; the sync after it takes the copy once its
    operator grant is gone."""
    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    rows = [_admin("code-z", "ops"), _admin("code-z", "x"), _copy("code-z-26-5", "ops", "code-z")]
    assert await _revoke("code-z", "ops", ch=_FakeCH(rows)) == 0
    gone = _admin("code-z", "ops", revoked_at=T0 + timedelta(hours=1))
    ch = _FakeCH([gone, *rows[1:]])
    await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    on_it = [r for r in ch.inserted if r["namespace"] == "code-z-26-5"]
    # ops's copy goes; x, an operator admin, is copied on.
    assert {(r["subject"], r["revoked_at"] is not None) for r in on_it} == {
        ("ops", True),
        ("x", False),
    }


async def test_an_operator_revoke_keeps_a_restricted_branch_s_last_admin_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ops's copy of their code-z-ops grant is the restricted branch's only admin: the
    guard keeps it. With another admin there it goes, under the guard."""
    two = [_Cfg("clickhouse", "code-z-ops", "Org/z"), *PRIVATE]
    monkeypatch.setattr(repos, "configured_repos", lambda: two)
    rows = [
        _admin("code-z", "ops"),
        _admin("code-z", "x"),
        _admin("code-z-ops", "ops"),
        _copy("code-z-26-5", "ops", "code-z-ops"),
    ]
    assert await _revoke("code-z", "ops", ch=_FakeCH(rows)) == 0
    ch = _FakeCH([*rows, _admin("code-z-26-5", "alice")])
    await _revoke("code-z", "ops", ch=ch)
    assert ("code-z-26-5", "ops") in {(r["namespace"], r["subject"]) for r in ch.inserted}


async def test_an_operator_revoke_takes_a_mirror_of_an_entry_no_longer_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    gone = {"granted_by": "repo-maintainers:code-z-old"}
    ops = [_admin(ns, "ops") for ns in ("code-z", "code-z-dev", "code-z-26-5")]
    ch = _FakeCH([_admin("code-z", "carol") | gone, _admin("code-z-dev", "carol") | gone, *ops])
    await _revoke("code-z", "carol", ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-z-dev"]


async def test_no_sync_before_the_self_service_registry_is_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(repo_registry, "registry_read", lambda: False)
    _code_rows(monkeypatch, {"code-x", "code-y"})  # told by their directory rows
    ch = _FakeCH([_admin("code-x", "alice")])
    assert await maintainers.sync_repo_maintainers("clickhouse", "code-x", ch=ch) == 0
    assert ch.inserted == []
    # One operator entry may still have page-added siblings: the skip is logged, once.
    with caplog.at_level("WARNING", logger=maintainers.log.name):
        for _ in range(2):
            assert await maintainers.sync_repo_maintainers("clickhouse", "code-y", ch=ch) == 0
    assert [r.message for r in caplog.records].count(
        "repo maintainers: clickhouse/code-y not synced — the self-service registry is not read yet"
    ) == 1


# Two operator entries and a branch added from the page.
TWO_OPS = [
    _Cfg("clickhouse", "code-w", "Org/w"),
    _Cfg("clickhouse", "code-w-26-5", "Org/w"),
    _Cfg("clickhouse", "code-w-dev", "Org/w", source="registry"),
]


async def test_operator_entries_share_admins_and_never_a_leaked_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: TWO_OPS)
    # A mirror written onto code-w from the page-added branch before the one-way rule.
    leaked = _admin("code-w", "mallory") | {"granted_by": "repo-maintainers:code-w-dev"}
    ch = _FakeCH([_admin("code-w-26-5", "ops"), _admin("code-w-dev", "mallory"), leaked])
    await maintainers.sync_repo_maintainers("clickhouse", "code-w", ch=ch)
    written = sorted((r["namespace"], r["subject"]) for r in ch.inserted)
    assert written == [("code-w", "ops"), ("code-w-dev", "ops")]


async def test_an_operator_revoke_takes_a_copy_of_a_copy_of_an_operator_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # carol's grant was given on an operator entry no longer configured; the copy on
    # code-z-26-5 was made from the stand-in on code-z-dev.
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    gone = {"granted_by": "repo-maintainers:code-z-old"}
    via_dev = {"granted_by": "repo-maintainers:code-z-dev"}
    ops = [_admin(ns, "ops") for ns in ("code-z", "code-z-dev", "code-z-26-5")]
    ch = _FakeCH(
        [
            _admin("code-z", "carol") | gone,
            _admin("code-z-dev", "carol") | gone,
            _admin("code-z-26-5", "carol") | via_dev,
            *ops,
        ]
    )
    await _revoke("code-z", "carol", ch=ch)
    assert sorted(r["namespace"] for r in ch.inserted) == ["code-z-26-5", "code-z-dev"]


async def test_an_operator_revoke_takes_a_copy_it_cannot_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The copy names a page-added branch where carol holds no live grant.
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    via_dev = {"granted_by": "repo-maintainers:code-z-dev"}
    ops = [_admin(ns, "ops") for ns in ("code-z", "code-z-dev", "code-z-26-5")]
    ch = _FakeCH([_admin("code-z", "carol"), _admin("code-z-26-5", "carol") | via_dev, *ops])
    await _revoke("code-z", "carol", ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-z-26-5"]


async def test_an_operator_revoke_takes_a_copy_caught_in_a_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    ops = [_admin(ns, "ops") for ns in ("code-z", "code-z-dev", "code-z-26-5")]
    loop = [
        _admin("code-z-dev", "carol") | {"granted_by": "repo-maintainers:code-z-26-5"},
        _admin("code-z-26-5", "carol") | {"granted_by": "repo-maintainers:code-z-dev"},
    ]
    ch = _FakeCH([_admin("code-z", "carol"), *loop, *ops])
    await _revoke("code-z", "carol", ch=ch)
    assert sorted(r["namespace"] for r in ch.inserted) == ["code-z-26-5", "code-z-dev"]


async def test_the_registrar_syncs_a_branch_once_the_registry_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.code.app.index import registry

    synced: list[str] = []

    async def _sync(org: str, ns: str, **_k: Any) -> None:
        synced.append(ns)

    monkeypatch.setattr(maintainers, "sync_quietly", _sync)
    monkeypatch.setattr(registry, "_DONE", {("clickhouse", "code-x")})
    monkeypatch.setattr(registry, "_SYNCED", set())
    cfg = SimpleNamespace(org="clickhouse", namespace="code-x")
    monkeypatch.setattr(repo_registry, "registry_read", lambda: False)
    assert await registry.ensure_registered(cfg, ch=object()) is False
    assert ("clickhouse", "code-x") not in registry._SYNCED  # not marked synced
    monkeypatch.setattr(repo_registry, "registry_read", lambda: True)
    await registry.ensure_registered(cfg, ch=object())
    await registry.ensure_registered(cfg, ch=object())
    assert synced == ["code-x", "code-x"]  # once while unread, once after, then done


async def test_the_ingest_pass_syncs_each_repository_with_a_restricted_page_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On every pass, once per repository: a restriction whose own sync failed still has
    its strays go. code-z-2 is restricted by its directory row alone, as a restriction
    cut short before its access row leaves it."""
    from types import SimpleNamespace

    synced: list[str] = []

    async def _sync(org: str, ns: str, **_k: Any) -> None:
        synced.append(ns)

    monkeypatch.setattr(maintainers, "sync_quietly", _sync)

    def entry(ns: str, classification: str) -> Any:
        return SimpleNamespace(
            org="clickhouse",
            namespace=ns,
            repo="Org/z",
            source="registry",
            classification=classification,
        )

    ch = _FakeCH([])
    ch.restricted = {"code-z-2"}
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    standard = [entry("code-z-3", "standard"), entry("code-z-2", "standard")]

    def going() -> bool:
        return False

    await maintainers.sync_restricted_page_branches(standard, exhausted=going)
    snapshot = [entry("code-z-1", "restricted"), *standard]
    monkeypatch.setattr(repos, "configured_repos", lambda: snapshot)
    await maintainers.sync_restricted_page_branches(snapshot, exhausted=going)
    # Once for the repository, from whichever of its restricted branches comes first.
    assert synced[0] == "code-z-2" and synced[1:] in (["code-z-1"], ["code-z-2"])
    await maintainers.sync_restricted_page_branches(standard, exhausted=lambda: True)
    assert len(synced) == 2  # past the pass budget

    # An org whose directory cannot be read waits for the next pass; the others go on.
    class _Unreadable(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "loom.namespaces" in sql and params["org"] == "other":
                raise ConnectionError("ClickHouse is not answering")
            return await super().query_all(sql, params)

    ch = _Unreadable([])
    ch.restricted = {"code-z-2"}
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    other = SimpleNamespace(**{**vars(entry("code-o", "restricted")), "org": "other"})
    synced.clear()
    await maintainers.sync_restricted_page_branches([other, *standard], exhausted=going)
    assert synced == ["code-z-2"]


async def test_code_init_reads_the_registry_before_it_registers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.cli import code_maintenance
    from server.code.app.index import registry

    read: list[bool] = [False]
    seen: list[bool] = []

    async def _refresh(**_k: Any) -> list[Any]:
        read[0] = True
        return []

    async def _ensure(cfg: Any, **_k: Any) -> bool:
        seen.append(repo_registry.registry_read())
        return True

    monkeypatch.setattr(repo_registry, "registry_read", lambda: read[0])
    monkeypatch.setattr(repo_registry, "refresh_db_repos", _refresh)
    monkeypatch.setattr(registry, "ensure_registered", _ensure)
    await code_maintenance._register_after_init(SimpleNamespace(org="o", namespace="code-x"))
    assert seen == [True]  # the registry was read when the namespace was registered


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


async def test_lowering_an_admin_fans_out_before_the_write(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _admin_row(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "alice", "role": "admin", "status": "active", "granted_by": "ops"}
        )

    async def _two(*_a: Any, **_k: Any) -> int:
        return 2

    async def _fan_out(org: str, ns: str, subject: str, **k: Any) -> None:
        _handler_env["revoke"].append((ns, subject, k["repo"], k["lowered_to"]))
        _handler_env["events"].append("fan-out")

    monkeypatch.setattr(grants_repo, "find_any_grant", _admin_row)
    monkeypatch.setattr(grants_repo, "find_active_grant", _admin_row)
    monkeypatch.setattr(grants_repo, "count_active_admins", _two)
    monkeypatch.setattr(maintainers, "revoke_repo_maintainer", _fan_out)
    await grants_mod.handle_grants_create(
        GrantsCreateInput(
            org="clickhouse",
            namespace="code-x",
            subject="alice",
            role="viewer",
            granted_by="ops",
            replace=True,
        )
    )
    assert _handler_env["revoke"] == [("code-x", "alice", "Org/x", "viewer")]
    assert _handler_env["events"] == ["fan-out", "write"]


async def test_a_replace_that_takes_no_admin_needs_no_registry_read(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _writer(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "dave", "role": "writer", "status": "active", "granted_by": "ops"}
        )

    async def _failed(_ch: Any = None) -> bool:
        return False

    monkeypatch.setattr(grants_repo, "find_any_grant", _writer)
    monkeypatch.setattr(repo_registry, "read_registry", _failed)
    await grants_mod.handle_grants_create(
        GrantsCreateInput(
            org="clickhouse",
            namespace="code-x",
            subject="dave",
            role="viewer",
            granted_by="ops",
            replace=True,
        )
    )
    assert [r["role"] for r in _handler_env["rows"]] == ["viewer"]
    assert _handler_env["revoke"] == []


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

    cascaded: list[tuple[str, tuple[str, ...]]] = []

    async def _cascade(org: str, namespace: str, subject: str, **kw: Any) -> int:
        cascaded.append((subject, kw["taken"]))
        return 0

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    monkeypatch.setattr("server.auth.aliases.read_aliases", _verified)
    monkeypatch.setattr(grants_repo, "count_active_admins", _admins)
    monkeypatch.setattr(token_grants, "cascade_revoke_to_created_tokens", _cascade)
    audited: list[Any] = []

    async def _emit(event_type: str, **kw: Any) -> str:
        audited.append((event_type, kw["subject"]))
        return ""

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    with pytest.raises(PreconditionFailedError, match="only admin"):
        await grants_mod.handle_grants_revoke(
            GrantsRevokeInput(org="clickhouse", namespace="code-x", subject="dave")
        )
    assert [r["subject"] for r in _handler_env["rows"]] == ["dave"]
    # The call failed, so no op audit row: the row that went is audited on its own.
    assert ("grants.revoke", {"namespace": "code-x", "subject": "dave", "named": "dave"}) in audited
    # The tokens dave made lose their grants for the row that went, once.
    assert cascaded == [("dave", ("dave",))]


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

    ch = _FakeCH([_admin(ns, s) for ns in ("code-x-26-5", "code-x-26-6") for s in ("alice", "bob")])
    held = grant_claims.grant_change

    def _busy_on_26_5(org: str, ns: str, subject: str) -> Any:
        if ns == "code-x-26-5":
            raise GrantBusyError("busy")
        return held(org, ns, subject)

    monkeypatch.setattr(grant_claims, "grant_change", _busy_on_26_5)
    with pytest.raises(GrantBusyError):
        await _revoke("code-x", "alice", ch=ch)
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
    assert await _revoke("code-x", "alice", ch=ch) == 1
    assert [r["namespace"] for r in ch.inserted] == ["code-x-26-5"]


async def test_a_failed_branch_fan_out_leaves_the_grant_live_for_the_retry(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
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
        async with maintainers.repo_change(
            "clickhouse", maintainers.repo_of("clickhouse", ns), subject
        ):
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
    async with maintainers.repo_change("clickhouse", None, "alice"):
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
    # The repository's admins, as a revoke holds them, then every branch's admins,
    # which a restriction is written under, then the grants.
    assert log == [
        "+repo:Org/x",
        "+repo:Org/x/admins",
        "+code-x-26-5/admins",
        "+code-x-26-6/admins",
        "+code-x-26-5",
        "+code-x-26-6",
        "insert ['code-x-26-5', 'code-x-26-6']",
        "-code-x-26-6",
        "-code-x-26-5",
        "-code-x-26-6/admins",
        "-code-x-26-5/admins",
        "-repo:Org/x/admins",
        "-repo:Org/x",
    ]


@pytest.mark.parametrize(
    ("namespace", "reached"),
    [("code-z-dev", ["code-z-26-5"]), ("code-z", ["code-z-26-5", "code-z-dev"])],
)
async def test_grants_revoke_fans_out_one_way(
    monkeypatch: pytest.MonkeyPatch, namespace: str, reached: list[str]
) -> None:
    from server.core import ch as ch_mod

    monkeypatch.setattr(repos, "configured_repos", lambda: MIXED)
    mirror = {"granted_by": "repo-maintainers:code-z"}
    ch = _FakeCH(
        [
            *(_admin(ns, "x") for ns in ("code-z", "code-z-dev", "code-z-26-5")),
            _admin("code-z", "ops"),
            _admin("code-z-dev", "ops") | mirror,
            _admin("code-z-26-5", "ops") | mirror,
        ]
    )
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)

    async def _any(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "ops", "role": "admin", "status": "active", "granted_by": "it"}
        )

    async def _two(*_a: Any, **_k: Any) -> int:
        return 2

    written: list[str] = []

    async def _insert(row: dict[str, Any], **_k: Any) -> None:
        written.append(row["namespace"])

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    monkeypatch.setattr(grants_repo, "count_active_admins", _two)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org="clickhouse", namespace=namespace, subject="ops")
    )
    assert sorted(r["namespace"] for r in ch.inserted) == reached
    assert written == [namespace]


async def test_a_page_branch_known_from_its_directory_row_is_read_then_fanned_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    known = [*MIXED[:2]]  # code-z-26-5 is not in this process's snapshot yet

    async def _read(_ch: Any = None) -> bool:
        if MIXED[2] not in known:
            known.append(MIXED[2])
        return True

    monkeypatch.setattr(repo_registry, "read_registry", _read)
    monkeypatch.setattr(repos, "configured_repos", lambda: known)
    _code_rows(monkeypatch, {"code-z-26-5"})
    repo = await maintainers.read_repo("clickhouse", "code-z-26-5", ch=object())
    assert repo == "Org/z"
    rows = [_admin(ns, s) for ns in ("code-z", "code-z-dev", "code-z-26-5") for s in ("ops", "x")]
    ch = _FakeCH(rows)
    await maintainers.revoke_repo_maintainer("clickhouse", "code-z-26-5", "ops", repo=repo, ch=ch)
    assert [r["namespace"] for r in ch.inserted] == ["code-z-dev"]


async def test_a_revoke_answers_busy_and_writes_nothing_while_the_registry_cannot_be_read(
    _handler_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _any(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {"subject": "alice", "role": "admin", "status": "active", "granted_by": "ops"}
        )

    async def _two(*_a: Any, **_k: Any) -> int:
        return 2

    async def _failed(_ch: Any = None) -> bool:
        return False

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    monkeypatch.setattr(grants_repo, "count_active_admins", _two)
    monkeypatch.setattr(repo_registry, "read_registry", _failed)
    with pytest.raises(GrantBusyError):
        await grants_mod.handle_grants_revoke(
            GrantsRevokeInput(org="clickhouse", namespace="code-x", subject="alice")
        )
    assert _handler_env["revoke"] == [] and _handler_env["rows"] == [], "the grant stays live"


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

    async def _read(_ch: Any = None) -> bool:
        log.append("registry read")
        return True

    monkeypatch.setattr(grants_repo, "find_any_grant", _any)
    monkeypatch.setattr(grants_repo, "count_active_admins", _two)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(maintainers, "revoke_repo_maintainer", _fan_out)
    monkeypatch.setattr(repo_registry, "read_registry", _read)
    await grants_mod.handle_grants_revoke(
        GrantsRevokeInput(org="clickhouse", namespace="code-x", subject="alice")
    )
    # The registry before the claims that name the repository; the repository's, then
    # the namespace's admins from the last-admin count to the write; the other
    # branches first, the grant last.
    assert log == [
        "registry read",
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


async def test_a_stray_that_cannot_be_claimed_does_not_stop_the_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import grant_claims

    held = grant_claims.grant_change

    def _mallory_busy(org: str, ns: str, subject: str) -> Any:
        if subject == "mallory":
            raise GrantBusyError("busy")
        return held(org, ns, subject)

    monkeypatch.setattr(repos, "configured_repos", lambda: PRIVATE)
    monkeypatch.setattr(grant_claims, "grant_change", _mallory_busy)
    ch = _FakeCH([_admin("code-z", "ops"), _copy("code-z-26-5", "mallory", "code-z-dev")])
    with pytest.raises(GrantBusyError):
        await maintainers.sync_repo_maintainers("clickhouse", "code-z", ch=ch)
    assert _on(ch, "code-z-26-5") == {"ops"}


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
    assert await _revoke("code-x", "alice", ch=ch) == 1
    assert [r["namespace"] for r in ch.inserted] == ["code-x-26-6"]


async def test_a_lowered_maintainer_is_lowered_on_every_other_branch() -> None:
    ch = _FakeCH([_admin(ns, s) for ns in ("code-x-26-5", "code-x-26-6") for s in ("alice", "bob")])
    assert await _revoke("code-x", "alice", ch=ch, lowered_to="viewer") == 2
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
