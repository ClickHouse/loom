"""A grant on a namespace with no directory row, such as a repository listed only in
LOOM_CODE_REPOS_JSON, is still a membership: `namespace.list_mine` lists it, as a code
namespace, so the token form can offer that repository by name and branch. The join's
missing side reads NULL, not ClickHouse's defaults (`active = false`), which drop the
row."""

from __future__ import annotations

import json

import pytest

import server.identity.app.namespace_membership as membership
from server.settings import settings
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

_PARAMS = {"org": "acme", "subs": ["kim", "kim@acme.dev"], "visitors": ["kim", "kim@acme.dev"]}


def _grant(namespace: str, role: str) -> str:
    return (
        "INSERT INTO loom.namespace_grants (org, namespace, subject, role, granted_at, "
        "granted_by, version, status) VALUES "
        f"('acme', '{namespace}', 'kim@acme.dev', '{role}', now(), 'op', 1, 'active')"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_a_grant_on_a_namespace_without_a_directory_row_is_listed() -> None:
    setup = [
        ddl("namespace_grants"),
        ddl("namespaces"),
        ddl("namespace_visits"),
        _grant("team-private", "admin"),
        _grant("code-acme-private", "viewer"),
        _grant("retired", "writer"),
        "INSERT INTO loom.namespaces (org, namespace, owner_email, active, kind) VALUES "
        "('acme', 'team-private', 'kim@acme.dev', true, 'memory'), "
        "('acme', 'retired', 'kim@acme.dev', false, 'memory')",
    ]
    rows = run(setup, membership._MEMBERSHIP_SQL, _PARAMS)
    by_ns = {r["namespace"]: r for r in rows}
    # The deactivated namespace stays out; the one with no directory row is in.
    assert set(by_ns) == {"team-private", "code-acme-private"}
    assert by_ns["team-private"]["kind"] == "memory"
    assert by_ns["code-acme-private"]["kind"] == ""  # no row: `_kind` decides
    assert by_ns["code-acme-private"]["visibility"] == "private"


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_without_join_use_nulls_the_grant_is_dropped() -> None:
    setup = [ddl("namespace_grants"), ddl("namespaces"), ddl("namespace_visits")]
    setup.append(_grant("code-acme-private", "viewer"))
    plain = membership._MEMBERSHIP_SQL.replace("SETTINGS join_use_nulls = 1", "")
    assert run(setup, plain, _PARAMS) == []
    assert [r["namespace"] for r in run(setup, membership._MEMBERSHIP_SQL, _PARAMS)] == [
        "code-acme-private"
    ]


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_a_visit_under_another_spelling_clears_is_new() -> None:
    setup = [
        ddl("namespace_grants"),
        ddl("namespaces"),
        ddl("namespace_visits"),
        "INSERT INTO loom.namespace_grants (org, namespace, subject, role, granted_at, "
        "granted_by, version, status) VALUES "
        "('acme', 'team-a', 'kim@acme.dev', 'viewer', now() - 60, 'op', 1, 'active'), "
        "('acme', 'team-b', 'kim@acme.dev', 'viewer', now() - 60, 'op', 1, 'active')",
        "INSERT INTO loom.namespace_visits (org, namespace, subject, last_seen_at) VALUES "
        "('acme', 'team-a', 'kim', now())",
    ]
    rows = {r["namespace"]: r["is_new"] for r in run(setup, membership._MEMBERSHIP_SQL, _PARAMS)}
    assert rows == {"team-a": 0, "team-b": 1}


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_a_grant_stored_in_another_case_is_listed() -> None:
    setup = [
        ddl("namespace_grants"),
        ddl("namespaces"),
        ddl("namespace_visits"),
        "INSERT INTO loom.namespace_grants (org, namespace, subject, role, granted_at, "
        "granted_by, version, status) VALUES "
        "('acme', 'team-public', 'Kim@Acme.dev', 'viewer', now(), 'op', 1, 'active')",
    ]
    rows = run(setup, membership._MEMBERSHIP_SQL, _PARAMS)
    assert [r["namespace"] for r in rows] == ["team-public"]


@pytest.mark.asyncio
async def test_a_token_inherits_no_restricted_repository_listed_only_in_the_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The directory knows no restricted namespace; the repository entry says restricted.
    import server.auth.aliases as aliases
    import server.auth.service_accounts as sa
    import server.identity.app.namespace_classification as nc

    entry = {
        "org": "acme",
        "namespace": "code-acme-private",
        "repo": "acme/private",
        "git_branch": "master",
        "classification": "restricted",
    }
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))

    async def _creator(org: str, sub: str, *, ch: object = None) -> str:
        return "kim@acme.dev"

    async def _aliases(org: str, subject: str, *, ch: object = None) -> list[str]:
        return [subject]

    async def _rows(ch: object, org: str, subjects: list[str]) -> list[dict[str, object]]:
        return [
            {"namespace": "team-public", "role": "viewer", "status": "active", "kind": "memory"},
            {"namespace": "code-acme-private", "role": "viewer", "status": "active", "kind": ""},
        ]

    async def _none_restricted(org: str) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(sa, "inheriting_creator", _creator)
    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(membership, "_membership_rows", _rows)
    monkeypatch.setattr(nc, "restricted_set", _none_restricted)
    rows = await membership._inherited_rows(object(), "acme", "sa:t-1", set())
    assert [r["namespace"] for r in rows] == ["team-public"]


def test_a_namespace_without_a_directory_row_is_code_when_a_repository_entry_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = {
        "org": "acme",
        "namespace": "code-acme-private",
        "repo": "acme/private",
        "git_branch": "master",
        "classification": "restricted",
    }
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))
    from server.identity.app.namespace_kinds import configured_kinds

    configured = configured_kinds("acme")
    assert membership._kind(configured, "code-acme-private", "") == "code"
    assert membership._kind(configured, "team-private", "") == "memory"
    assert membership._kind(configured, "code-acme-private", "qa") == "qa"


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", [[], None], ids=["no-row", "unreadable"])
async def test_a_repository_listed_only_in_the_config_is_never_where_a_token_saves(
    monkeypatch: pytest.MonkeyPatch, directory: list | None
) -> None:
    from server.identity.app import caller_home, namespace_kinds

    entry = {"org": "acme", "namespace": "code-acme-private", "repo": "acme/private"}
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))

    class _Directory:
        _client = object()

        async def query_all(self, sql: str, params: object = None) -> list:
            if directory is None:
                raise RuntimeError("directory down")
            return directory

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Directory())
    namespace_kinds.reset_cache()
    try:
        kinds = await caller_home._kinds_or_empty("acme")
    finally:
        namespace_kinds.reset_cache()
    assert not caller_home.is_memory_home(kinds, "code-acme-private")
    assert caller_home.is_memory_home(kinds, "team-private")


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_a_grant_row_carries_the_directory_title() -> None:
    setup = [
        ddl("namespace_grants"),
        ddl("namespaces"),
        ddl("namespace_visits"),
        _grant("col-research", "viewer"),
        _grant("team-private", "admin"),
        "INSERT INTO loom.namespaces (org, namespace, owner_email, active, kind, title) VALUES "
        "('acme', 'col-research', 'kim@acme.dev', true, 'collection', 'Research')",
    ]
    by_ns = {r["namespace"]: r for r in run(setup, membership._MEMBERSHIP_SQL, _PARAMS)}
    assert by_ns["col-research"]["title"] == "Research"
    assert by_ns["team-private"]["title"] == ""  # no directory row
