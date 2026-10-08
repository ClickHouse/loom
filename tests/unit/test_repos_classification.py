"""`classification` in LOOM_CODE_REPOS_JSON: restricted forces
org_readable off, an unknown value fails closed, and both are config states."""

from __future__ import annotations

import json

import pytest

import server.code.domain.index.repos as repos


@pytest.fixture
def cfg(monkeypatch: pytest.MonkeyPatch, scalar_code_repo):
    from server.settings import settings

    def _set(entries, org_readable: bool = True):
        monkeypatch.setattr(settings, "code_repos_json", json.dumps(entries))
        monkeypatch.setattr(settings, "code_org_readable", org_readable)
        repos._config_errors.clear()
        repos._config_errors_logged.clear()
        return {r.namespace: r for r in repos.configured_repos()}

    return _set


def test_default_is_standard_and_inherits_org_readable(cfg) -> None:
    got = cfg([{"namespace": "code-clickhouse"}])
    assert got["code-clickhouse"].classification == "standard"
    assert got["code-clickhouse"].org_readable is True


def test_restricted_forces_org_readable_off(cfg) -> None:
    got = cfg([{"namespace": "code-example-private", "classification": "restricted"}])
    entry = got["code-example-private"]
    assert entry.classification == "restricted"
    assert entry.org_readable is False
    # inherited (not stated) org_readable is not a contradiction
    assert repos.config_error_count() == 0


def test_restricted_with_explicit_org_readable_true_is_a_config_error(cfg) -> None:
    got = cfg([{"namespace": "p", "classification": "restricted", "org_readable": True}])
    assert got["p"].org_readable is False
    assert repos.config_error_count() == 1
    assert "restricted_org_readable:p" in repos._config_errors


def test_unknown_classification_fails_closed(cfg) -> None:
    got = cfg([{"namespace": "p", "classification": "secretish"}])
    assert got["p"].classification == "restricted"
    assert got["p"].org_readable is False
    assert "classification:p" in repos._config_errors


def test_scalar_config_is_standard(cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    got = cfg([{"namespace": "x"}])
    assert got
    repos._config_errors.clear()
    monkeypatch.setattr(settings, "code_repos_json", "")
    assert repos.configured_repos()[0].classification == "standard"


def test_no_scalar_repo_configures_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defaults name no repository: a fresh install lists no entry and indexes
    nothing until one is configured."""
    from server.settings import settings

    monkeypatch.setattr(settings, "code_repos_json", "")
    monkeypatch.setattr(settings, "code_repo", "")
    repos._config_errors.clear()
    assert repos.configured_repos() == []
    assert repos.config_error_count() == 0
    assert repos.default_git_dir() == ""


def test_scalar_repo_without_org_or_namespace_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_repos_json", "")
    monkeypatch.setattr(settings, "code_repo", "acme/widgets")
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "")
    repos._config_errors.clear()
    assert repos.configured_repos() == []
    assert "LOOM_CODE_NAMESPACE" in repos._config_errors["scalar"]


def test_scalar_repo_derives_its_clone_and_indexes_the_whole_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_repos_json", "")
    monkeypatch.setattr(settings, "code_repo", "acme/widgets")
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-widgets")
    monkeypatch.setattr(settings, "code_git_dir", "")
    monkeypatch.setattr(settings, "code_git_remote", "")
    monkeypatch.setattr(settings, "code_path_prefixes", "")
    (entry,) = repos.configured_repos()
    assert entry.git_dir == "var/code_repos/widgets.git"
    assert entry.git_remote == "https://github.com/acme/widgets.git"
    assert entry.git_branch == settings.code_git_branch
    assert entry.path_prefixes == repos.WHOLE_TREE


def test_entry_without_org_or_repo_is_skipped_when_the_scalar_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_org", "")
    monkeypatch.setattr(settings, "code_repo", "")
    monkeypatch.setattr(settings, "code_namespace", "")
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps(
            [
                {"namespace": "code-a", "repo": "acme/a"},
                {"namespace": "code-b", "org": "acme"},
                {"namespace": "code-c", "org": "acme", "repo": "acme/c"},
            ]
        ),
    )
    repos._config_errors.clear()
    got = {r.namespace: r for r in repos.configured_repos()}
    assert set(got) == {"code-c"}
    assert got["code-c"].git_dir == "var/code_repos/c.git"
    assert {"org:code-a", "repo:code-b"} <= set(repos._config_errors)
