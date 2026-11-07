"""A grant on a namespace with no directory row, such as a repository listed only in
LOOM_CODE_REPOS_JSON, is still a membership: `namespace.list_mine` lists it, as a code
namespace, so the token form can offer that repository by name and branch. The join's
missing side reads NULL, not ClickHouse's defaults (`active = false`), which used to drop
the row."""

from __future__ import annotations

import json

import pytest

import server.identity.app.namespace_membership as membership
from server.settings import settings
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


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
    rows = run(setup, membership._MEMBERSHIP_SQL, {"org": "acme", "subs": ["kim@acme.dev"]})
    by_ns = {r["namespace"]: r for r in rows}
    # The deactivated namespace stays out; the one with no directory row is in.
    assert set(by_ns) == {"team-private", "code-acme-private"}
    assert by_ns["team-private"]["kind"] == "memory"
    assert by_ns["code-acme-private"]["kind"] == ""  # no row: `_kind` decides
    assert by_ns["code-acme-private"]["visibility"] == "private"


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
    assert membership._kind("acme", "code-acme-private", "") == "code"
    assert membership._kind("acme", "team-private", "") == "memory"
    assert membership._kind("acme", "code-acme-private", "qa") == "qa"
