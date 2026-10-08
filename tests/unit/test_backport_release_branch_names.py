"""A release-branch namespace whose git branch is
named `release/26.9` (the private repository's convention) carries the
version `26.9` for `code.backport_gap`, like the public `26.9` branch does:
such namespaces stay in `release_namespaces` (no empty version, no `unknown` /
`no_branch_version` verdict), and a bare `26.8` in `branches` resolves to its
release branch instead of being refused as a namespace named `26.8`."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from server.code.app import backport as h
from server.tracker.domain import backport as bp
from server.tracker.domain.bisect import parse_version


def test_release_branch_names_parse_to_their_version() -> None:
    assert parse_version("release/26.9") == (26, 9)
    assert parse_version("refs/heads/release/26.4") == (26, 4)
    assert parse_version("releases/26.4.1") == (26, 4)
    assert parse_version("26.4") == (26, 4)
    assert parse_version("v26.4") == (26, 4)
    assert parse_version("master") is None
    assert parse_version("release/master") is None
    assert parse_version("code-clickhouse-26-4") is None
    assert bp.version_key("release/26.9") == (26, 9)


def _repos():
    mk = lambda ns, repo, br: SimpleNamespace(  # noqa: E731
        org="clickhouse", namespace=ns, repo=repo, git_branch=br
    )
    return [
        mk("code-clickhouse", "ClickHouse/ClickHouse", "master"),
        mk("code-clickhouse-26-8", "ClickHouse/ClickHouse", "26.8"),
        mk("code-example-private", "ClickHouse/example-private", "master"),
        mk("code-example-private-26-9", "ClickHouse/example-private", "release/26.9"),
        mk("code-example-private-26-8", "ClickHouse/example-private", "release/26.8"),
        mk("code-example-private-26-4", "ClickHouse/example-private", "release/26.4"),
    ]


def test_private_release_namespaces_carry_their_version_newest_first() -> None:
    with patch("server.code.domain.index.repos.configured_repos", return_value=_repos()):
        got = h.release_namespaces("clickhouse", "code-example-private")
    assert got == [
        ("code-example-private-26-9", "26.9"),
        ("code-example-private-26-8", "26.8"),
        ("code-example-private-26-4", "26.4"),
    ]
    # the public repository's set is untouched and never mixes repositories
    with patch("server.code.domain.index.repos.configured_repos", return_value=_repos()):
        assert h.release_namespaces("clickhouse", "code-clickhouse") == [
            ("code-clickhouse-26-8", "26.8")
        ]


def test_requested_branches_resolve_by_namespace_or_bare_version() -> None:
    with patch("server.code.domain.index.repos.configured_repos", return_value=_repos()):
        got = h.resolve_branches(
            "clickhouse",
            "code-example-private",
            ["code-example-private-26-9", "26.8", "release/26.4"],
        )
    assert got == [
        ("code-example-private-26-9", "26.9"),
        ("code-example-private-26-8", "26.8"),
        ("code-example-private-26-4", "26.4"),
    ]


def test_another_orgs_entries_never_join_the_branch_set() -> None:
    """Configuration is per org: an entry of another org indexing the same repository
    (or reusing the namespace name) is neither a release branch nor a mirror to read."""
    other = [
        SimpleNamespace(
            org="acme",
            namespace="code-clickhouse",
            repo="ClickHouse/ClickHouse",
            git_branch="master",
            git_dir="/acme/mirror",
        ),
        SimpleNamespace(
            org="acme",
            namespace="acme-26-3",
            repo="ClickHouse/ClickHouse",
            git_branch="26.3",
            git_dir="/acme/mirror",
        ),
    ]
    ours = [SimpleNamespace(**{**vars(r), "git_dir": "/ours/mirror"}) for r in _repos()]
    with patch("server.code.domain.index.repos.configured_repos", return_value=other + ours):
        assert h.release_namespaces("clickhouse", "code-clickhouse") == [
            ("code-clickhouse-26-8", "26.8")
        ]
        git_dir, refs = h.branch_refs("clickhouse", "code-clickhouse")
        assert git_dir == "/ours/mirror" and refs == {"code-clickhouse-26-8": "26.8"}
        # a namespace this org does not configure has no branches, whoever else does
        assert h.release_namespaces("nobody", "code-clickhouse") == []
        assert h.branch_refs("nobody", "code-clickhouse") == ("", {})
