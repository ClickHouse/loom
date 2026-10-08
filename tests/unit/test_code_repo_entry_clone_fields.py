"""A repository entry's clone fields never come from another
repository.

An entry that names `repo` and `git_dir` but no `git_remote` must not
inherit the scalar remote of another repository: otherwise `loom code init`
seeds `repo_remote` with the other repository's URL and the ingest worker
fetches the wrong history into this clone on its first pass.
"""

from __future__ import annotations

import json

import pytest

from server.code.domain.index import repos
from server.settings import settings

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")


def _configure(monkeypatch: pytest.MonkeyPatch, entries: list[dict]) -> None:
    monkeypatch.setattr(settings, "code_repo", "ClickHouse/ClickHouse")
    monkeypatch.setattr(settings, "code_git_dir", "var/code_repos/clickhouse.git")
    monkeypatch.setattr(settings, "code_git_remote", "https://github.com/ClickHouse/ClickHouse.git")
    monkeypatch.setattr(settings, "code_repos_json", json.dumps(entries))
    monkeypatch.setattr(repos, "_publish_config_errors", lambda errors: captured.update(errors))


captured: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _reset() -> None:
    captured.clear()


def test_release_branch_of_the_scalar_repo_still_inherits_its_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(
        monkeypatch,
        [
            {"namespace": "code-clickhouse"},
            {"namespace": "code-clickhouse-26-1", "git_branch": "26.1"},
        ],
    )
    by_ns = {c.namespace: c for c in repos._configured_from_env()}
    rel = by_ns["code-clickhouse-26-1"]
    assert rel.git_dir == "var/code_repos/clickhouse.git"
    assert rel.git_remote == "https://github.com/ClickHouse/ClickHouse.git"
    assert not captured


def test_a_different_repo_derives_its_remote_from_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(
        monkeypatch,
        [
            {
                "namespace": "code-langfuse",
                "repo": "langfuse/langfuse",
                "git_dir": "var/code_repos/langfuse.git",
                "git_branch": "main",
            }
        ],
    )
    (cfg,) = repos._configured_from_env()
    assert cfg.git_remote == "https://github.com/langfuse/langfuse.git"
    assert cfg.git_dir == "var/code_repos/langfuse.git"
    assert not captured


def test_an_explicit_remote_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(
        monkeypatch,
        [
            {
                "namespace": "code-x",
                "repo": "acme/x",
                "git_dir": "var/code_repos/x.git",
                "git_remote": "git@github.com:acme/x.git",
            }
        ],
    )
    (cfg,) = repos._configured_from_env()
    assert cfg.git_remote == "git@github.com:acme/x.git"


def test_a_different_repo_without_git_dir_gets_a_clone_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(
        monkeypatch,
        [
            {"namespace": "code-clickhouse"},
            {"namespace": "code-langfuse", "repo": "langfuse/langfuse"},
        ],
    )
    by_ns = {c.namespace: c for c in repos._configured_from_env()}
    lf = by_ns["code-langfuse"]
    assert lf.git_dir == "var/code_repos/langfuse.git"
    assert lf.git_dir != by_ns["code-clickhouse"].git_dir
    assert lf.git_remote == "https://github.com/langfuse/langfuse.git"
    assert not captured
