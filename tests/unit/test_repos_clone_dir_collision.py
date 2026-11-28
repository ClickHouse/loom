"""Two entries on one clone dir share the bare clone only when their `git_remote`
values, after the defaults, are the same: a fork and its upstream derive the same
clone dir, and one clone would let each fetch move the other's refs."""

from __future__ import annotations

import json

import pytest

import server.code.domain.index.repos as repos


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch, scalar_code_repo):
    from server.settings import settings

    def _set(entries):
        monkeypatch.setattr(settings, "code_repos_json", json.dumps(entries))
        repos._config_errors.clear()
        repos._config_errors_logged.clear()
        return {r.namespace: r for r in repos.configured_repos()}

    return _set


def test_a_fork_and_its_upstream_do_not_share_a_clone(cfg) -> None:
    got = cfg(
        [
            {
                "namespace": "code-widgets-fork",
                "repo": "git.example.com/acme/widgets",
                "git_remote": "https://git.example.com/acme/widgets.git",
            },
            {"namespace": "code-widgets-upstream", "repo": "widgets-org/widgets"},
        ]
    )
    assert "code-widgets-fork" in got
    assert "code-widgets-upstream" not in got
    msg = repos._config_errors["git_dir:code-widgets-upstream"]
    assert "code-widgets-fork" in msg
    assert "use one spelling of the remote, or set a separate git_dir" in msg


def test_branches_with_one_spelling_of_the_remote_share_their_clone(cfg) -> None:
    got = cfg(
        [
            {"namespace": "code-widgets", "repo": "acme/widgets", "git_branch": "main"},
            {
                "namespace": "code-widgets-2-4",
                "repo": "acme/widgets",
                "git_branch": "release/2.4",
                "git_remote": "https://github.com/acme/widgets.git",
            },
        ]
    )
    assert got["code-widgets"].git_dir == got["code-widgets-2-4"].git_dir
    assert repos.config_error_count() == 0


def test_two_spellings_of_one_remote_are_refused(cfg) -> None:
    got = cfg(
        [
            {"namespace": "code-widgets", "repo": "acme/widgets", "git_branch": "main"},
            {
                "namespace": "code-widgets-2-4",
                "repo": "acme/widgets",
                "git_branch": "release/2.4",
                "git_remote": "git@github.com:acme/widgets.git",
            },
        ]
    )
    assert "code-widgets" in got
    assert "code-widgets-2-4" not in got
    msg = repos._config_errors["git_dir:code-widgets-2-4"]
    assert "code-widgets" in msg
    assert "use one spelling of the remote, or set a separate git_dir" in msg


def test_a_separate_git_dir_lets_both_in(cfg) -> None:
    got = cfg(
        [
            {
                "namespace": "code-widgets-fork",
                "repo": "git.example.com/acme/widgets",
                "git_remote": "https://git.example.com/acme/widgets.git",
            },
            {
                "namespace": "code-widgets-upstream",
                "repo": "widgets-org/widgets",
                "git_dir": "var/code_repos/widgets-upstream.git",
            },
        ]
    )
    assert got["code-widgets-fork"].git_dir != got["code-widgets-upstream"].git_dir
    assert repos.config_error_count() == 0
