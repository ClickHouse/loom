"""CLI and op input guards.

1. An explicitly addressed, unconfigured namespace fails hard. When a shell
   `source` of .env mangles LOOM_CODE_REPOS_JSON, configured_repos() degrades
   to the scalar entry, and `loom code backfill --namespace <ns>` would
   otherwise index the default branch under that namespace.
2. `code.context_pack` with a negative pr_number is a validation error, not a
   UInt32 bind failure that surfaces as a 500.
3. `otel.shutdown()` must detach the root-logger handler so records logged
   at interpreter teardown cannot hit a shut-down provider.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.settings import settings

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")


def _args(ns: str | None) -> argparse.Namespace:
    return argparse.Namespace(org=None, namespace=ns)


def test_repo_cfg_refuses_an_unconfigured_explicit_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import code_run

    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([{"namespace": "code-clickhouse"}]))
    with pytest.raises(SystemExit) as ex:
        code_run._repo_cfg(_args("code-clickhouse-26-1"))
    assert "not a configured code repo entry" in str(ex.value)


def test_repo_cfg_names_why_an_entry_was_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import code_run

    entries = [{"namespace": "code-a"}, {"namespace": "code-a.python"}]
    monkeypatch.setattr(settings, "code_repos_json", json.dumps(entries))
    with pytest.raises(SystemExit) as ex:
        code_run._repo_cfg(_args("code-a.python"))
    assert "code repo code-a.python skipped: a namespace has no '.' or '/'" in str(ex.value)


def test_no_default_code_namespace_is_a_clear_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no scalar default the CLI and every code.* handler name what is missing
    instead of reading namespace ''."""
    from server.cli import code_run
    from server.code.app.common import resolve_code_identity
    from server.core.errors import ValidationError

    monkeypatch.setattr(settings, "code_org", "")
    monkeypatch.setattr(settings, "code_namespace", "")
    with pytest.raises(SystemExit, match="--namespace"):
        code_run._identity(argparse.Namespace(org="acme", namespace=None))
    with pytest.raises(ValidationError, match="namespace"):
        resolve_code_identity("acme", "")
    with pytest.raises(ValidationError, match="org"):
        resolve_code_identity("", "code-widgets")
    assert resolve_code_identity("acme", "code-widgets") == ("acme", "code-widgets")


def test_repo_cfg_with_no_configured_repo_says_how_to_add_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import code_run

    monkeypatch.setattr(settings, "code_repo", "")
    monkeypatch.setattr(settings, "code_repos_json", "")
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    with pytest.raises(SystemExit, match="LOOM_CODE_REPO"):
        code_run._repo_cfg(argparse.Namespace(org="acme", namespace="code-widgets"))


def test_repo_cfg_resolves_configured_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import code_run

    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps(
            [
                {"namespace": "code-clickhouse"},
                {"namespace": "code-clickhouse-26-1", "git_branch": "26.1"},
            ]
        ),
    )
    assert code_run._repo_cfg(_args("code-clickhouse-26-1")).git_branch == "26.1"
    assert code_run._repo_cfg(_args(None)).namespace == "code-clickhouse"


def test_context_pack_rejects_negative_pr_number() -> None:
    from server.code.api.models_context import CodeContextPackInput

    with pytest.raises(ValidationError):
        CodeContextPackInput(pr_number=-5)
    assert CodeContextPackInput(pr_number=0).pr_number == 0


def test_otel_shutdown_detaches_the_root_log_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import otel

    class _Provider:
        def shutdown(self) -> None:
            pass

    handler = logging.NullHandler()
    logging.getLogger().addHandler(handler)
    monkeypatch.setattr(otel, "_logger_provider", _Provider())
    monkeypatch.setattr(otel, "_log_handler", handler)
    monkeypatch.setattr(otel, "_tracer_provider", None)
    monkeypatch.setattr(otel, "_meter_provider", None)
    otel.shutdown()
    assert handler not in logging.getLogger().handlers
    assert otel._log_handler is None


def test_repack_merge_diff_is_none_when_git_fails(tmp_path: Path) -> None:
    import asyncio

    from server.cli.code_maintenance import _merge_diff

    # Not a git dir: git exits non-zero — no diff, not an empty one.
    assert asyncio.run(_merge_diff(str(tmp_path), "0" * 40)) is None


def test_repack_merge_diff_reads_the_merge_diff_with_paths_verbatim(tmp_path: Path) -> None:
    """The diff comes back whole, and a non-ASCII path is named as it is, not
    C-quoted (a quoted `+++` line never matches an indexed path)."""
    import asyncio
    import subprocess

    from server.cli.code_maintenance import _merge_diff

    work = tmp_path / "w"
    work.mkdir()

    def git(*a: str) -> str:
        return subprocess.run(
            ["git", "-C", str(work), *a], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (work / "café.py").write_text("a = 1\n")
    git("add", ".")
    git("commit", "-qm", "one")
    (work / "café.py").write_text("a = 2\n")
    git("commit", "-qam", "two")
    diff = asyncio.run(_merge_diff(str(work / ".git"), git("rev-parse", "HEAD")))
    assert diff is not None
    assert "+++ b/café.py" in diff
    assert "+a = 2" in diff


def test_repack_merge_diff_refuses_a_sha_git_would_read_as_an_option(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    from server.cli.code_maintenance import _merge_diff
    from server.code.app.index import gitrepo

    async def _no_git(*a, **k):
        raise AssertionError("git must not run for a non-hex sha")

    monkeypatch.setattr(gitrepo, "run", _no_git)
    for bad in ("--output=/tmp/x", "HEAD", "", "0" * 40 + ";x"):
        assert asyncio.run(_merge_diff(str(tmp_path), bad)) is None
