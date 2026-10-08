"""Multi-repo configuration contract (code/domain/index/repos.py).

GATE: an invalid LOOM_CODE_REPOS_JSON must degrade loudly to the scalar
single-repo config — a typo in one env var must never silently stop all
indexing — and per-entry fields fall back to the scalars so repo #2 only
declares what differs.
"""

from __future__ import annotations

import pytest

from server.code.domain.index.repos import configured_repos, repo_for_namespace
from server.settings import settings


@pytest.fixture(autouse=True)
def _scalars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_org", "clickhouse")
    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")
    monkeypatch.setattr(settings, "code_repo", "ClickHouse/ClickHouse")
    monkeypatch.setattr(settings, "code_git_dir", "var/code_repos/clickhouse.git")
    monkeypatch.setattr(settings, "code_path_prefixes", "src/,base/")
    monkeypatch.setattr(settings, "code_github_token", "tok-scalar")
    monkeypatch.setattr(settings, "code_repos_json", "")


def test_empty_json_yields_scalar_repo() -> None:
    repos = configured_repos()
    assert len(repos) == 1
    assert repos[0].namespace == "code-clickhouse"
    assert repos[0].path_prefixes == ("src/", "base/")


def test_entries_fall_back_to_scalars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"namespace": "code-clickhouse"},'
        ' {"namespace": "code-loom", "repo": "ClickHouse/loom",'
        '  "git_dir": "var/code_repos/loom.git", "path_prefixes": "server/,sdk/"}]',
    )
    repos = configured_repos()
    assert [r.namespace for r in repos] == ["code-clickhouse", "code-loom"]
    # entry 1 inherits every scalar — except the prefixes: an
    # entry that names none indexes its whole tree, the scalar default being
    # ClickHouse-shaped
    assert repos[0].repo == "ClickHouse/ClickHouse"
    assert repos[0].github_token == "tok-scalar"
    assert repos[0].path_prefixes == ("",)
    assert "src/Foo.cpp".startswith(repos[0].path_prefixes) and "docs/a.md".startswith(
        repos[0].path_prefixes
    )
    # entry 2 overrides what it declares, inherits the rest
    assert repos[1].repo == "ClickHouse/loom"
    assert repos[1].path_prefixes == ("server/", "sdk/")
    assert repos[1].github_token == "tok-scalar"
    assert repos[1].git_branch == repos[0].git_branch


def test_invalid_json_degrades_to_scalar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_repos_json", "{not json")
    repos = configured_repos()
    assert len(repos) == 1 and repos[0].namespace == "code-clickhouse"


@pytest.mark.parametrize("raw", ["[]", '{"namespace": "code-loom"}', '"code-loom"'])
def test_json_that_is_not_a_non_empty_array_degrades_to_scalar(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setattr(settings, "code_repos_json", raw)
    repos = configured_repos()
    assert len(repos) == 1 and repos[0].namespace == "code-clickhouse"


def test_duplicate_and_nameless_entries_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"namespace": "a"}, {"namespace": "a"}, {"repo": "x/y"}]',
    )
    repos = configured_repos()
    assert [r.namespace for r in repos] == ["a"]


def test_repo_for_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"namespace": "a"}, {"namespace": "b", "org": "other"}]',
    )
    assert repo_for_namespace("clickhouse", "a") is not None
    assert repo_for_namespace("other", "b") is not None
    assert repo_for_namespace("clickhouse", "b") is None
    assert repo_for_namespace("clickhouse", "zzz") is None


def test_backfill_index_exit_code_tolerates_rate_not_perfection() -> None:
    from server.cli.code_backfill import backfill_index_exit_code

    # one pathological generated file in 10K must not exit 1 and abort a
    # `set -e` deploy before embeddings
    assert backfill_index_exit_code(10234, 1) == 0
    assert backfill_index_exit_code(10234, 102) == 0  # exactly 1%
    assert backfill_index_exit_code(10234, 103) == 1  # above the rate
    # small delta passes stay strict: one error IS the signal
    assert backfill_index_exit_code(3, 1) == 1
    assert backfill_index_exit_code(99, 1) == 1
    assert backfill_index_exit_code(100, 1) == 0
    assert backfill_index_exit_code(0, 0) == 0


def test_config_error_is_logged_once_then_read_as_a_gauge(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A config error would otherwise be logged once per worker pass and per
    metrics export. A condition is logged once per process; `config_error_count()` carries
    whether it still holds, for the `loom.code.repos_config_errors` gauge."""
    import logging

    from server.code.domain.index import repos

    monkeypatch.setattr(repos, "_config_errors", {})
    monkeypatch.setattr(repos, "_config_errors_logged", set())
    monkeypatch.setattr(repos, "_config_evaluated", False)
    assert repos.config_error_count() is None, "no observation before the first call"

    monkeypatch.setattr(settings, "code_repos_json", "{not json")
    with caplog.at_level(logging.ERROR, logger="loom.code_index.repos"):
        for _ in range(5):
            assert [r.namespace for r in configured_repos()] == ["code-clickhouse"]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    assert "logged once per process" in errors[0].getMessage()
    assert repos.config_error_count() == 1

    # Per-entry conditions are states too, keyed so each logs once.
    caplog.clear()
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"namespace": "code-clickhouse"}, {"repo": "x/y"}, {"namespace": "code-clickhouse"}]',
    )
    with caplog.at_level(logging.ERROR, logger="loom.code_index.repos"):
        configured_repos()
        configured_repos()
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2
    assert repos.config_error_count() == 2

    # The condition clears: the gauge reads 0 and nothing more is logged.
    caplog.clear()
    monkeypatch.setattr(settings, "code_repos_json", '[{"namespace": "code-clickhouse"}]')
    with caplog.at_level(logging.ERROR, logger="loom.code_index.repos"):
        configured_repos()
    assert repos.config_error_count() == 0
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]
