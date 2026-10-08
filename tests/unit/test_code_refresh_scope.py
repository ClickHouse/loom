"""code.refresh namespace scoping (code/app/ingest.py + code/workers/ingest.py).

GATE: only a branch an operator configured ever gets indexed. A refresh
addressed to an unconfigured namespace must 404 (not silently run a global
pass), and a refresh for one namespace must run the ingest pass for that
repo entry alone — never the whole fleet.
"""

from __future__ import annotations

import pytest

from server.code.api.models_ingest import CodeRefreshInput
from server.code.app import ingest as handler_mod
from server.code.app.index.ingest_pass import selected_repos
from server.code.app.index.pass_budget import CodeIngestResult
from server.code.app.ingest import handle_code_refresh
from server.core.errors import NotFoundError
from server.settings import settings


@pytest.fixture(autouse=True)
def _scalars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "clickhouse")
    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")
    monkeypatch.setattr(settings, "code_repo", "ClickHouse/ClickHouse")
    monkeypatch.setattr(settings, "code_git_dir", "var/code_repos/clickhouse.git")
    monkeypatch.setattr(settings, "code_git_branch", "master")
    monkeypatch.setattr(settings, "code_path_prefixes", "src/,base/")
    monkeypatch.setattr(settings, "code_github_token", "")
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"namespace": "code-clickhouse"},'
        ' {"namespace": "code-clickhouse-25-8", "git_branch": "25.8"}]',
    )
    # per-test lock table: a leaked lock from one test must not coalesce
    # another test's trigger into already_running
    monkeypatch.setattr(handler_mod, "_REFRESH_LOCKS", {})


class _FakeCodeRepo:
    written: list[tuple[str, str, str, str, str]] = []

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return "head-" + ns

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        _FakeCodeRepo.written.append((org, ns, key, value, detail))


def test_selected_repos_defaults_to_all() -> None:
    assert [c.namespace for c in selected_repos()] == [
        "code-clickhouse",
        "code-clickhouse-25-8",
    ]


def test_selected_repos_scopes_to_addressed_namespace() -> None:
    repos = selected_repos("clickhouse", "code-clickhouse-25-8")
    assert [c.namespace for c in repos] == ["code-clickhouse-25-8"]
    assert repos[0].git_branch == "25.8"
    # the release entry inherits the shared bare clone
    assert repos[0].git_dir == "var/code_repos/clickhouse.git"


def test_selected_repos_empty_for_unconfigured() -> None:
    assert selected_repos("clickhouse", "code-clickhouse-99-9") == []


async def test_refresh_unconfigured_namespace_404s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler_mod, "CodeRepo", _FakeCodeRepo)
    with pytest.raises(NotFoundError):
        await handle_code_refresh(
            CodeRefreshInput(org="clickhouse", namespace="code-clickhouse-99-9")
        )


async def test_refresh_runs_pass_scoped_to_addressed_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler_mod, "CodeRepo", _FakeCodeRepo)
    calls: list[tuple[str, str]] = []

    async def _fake_run_pass(org: str = "", namespace: str = "") -> CodeIngestResult:
        calls.append((org, namespace))
        return CodeIngestResult(head="abc123")

    import server.code.app.index.ingest_pass as worker_mod

    monkeypatch.setattr(worker_mod, "run_pass", _fake_run_pass)
    out = await handle_code_refresh(
        CodeRefreshInput(org="clickhouse", namespace="code-clickhouse-25-8", wait=True)
    )
    assert out.triggered is True
    assert calls == [("clickhouse", "code-clickhouse-25-8")]
    assert out.head_before == "head-code-clickhouse-25-8"


async def test_refresh_empty_identity_defaults_to_scalar_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler_mod, "CodeRepo", _FakeCodeRepo)
    calls: list[tuple[str, str]] = []

    async def _fake_run_pass(org: str = "", namespace: str = "") -> CodeIngestResult:
        calls.append((org, namespace))
        return CodeIngestResult(head="abc123")

    import server.code.app.index.ingest_pass as worker_mod

    monkeypatch.setattr(worker_mod, "run_pass", _fake_run_pass)
    out = await handle_code_refresh(CodeRefreshInput(wait=True))
    assert out.triggered is True
    assert calls == [("clickhouse", "code-clickhouse")]


async def test_refresh_breadcrumb_leaves_out_fields_the_caller_left_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler_mod, "CodeRepo", _FakeCodeRepo)
    _FakeCodeRepo.written.clear()

    async def _fake_run_pass(org: str = "", namespace: str = "") -> CodeIngestResult:
        return CodeIngestResult(head="abc123")

    import server.code.app.index.ingest_pass as worker_mod

    monkeypatch.setattr(worker_mod, "run_pass", _fake_run_pass)
    await handle_code_refresh(
        CodeRefreshInput(org="clickhouse", namespace="code-clickhouse-25-8", agent="ci", wait=True)
    )
    details = [d for (_, _, key, _, d) in _FakeCodeRepo.written if key == "last_refresh_at"]
    assert details == ["agent=ci"]
