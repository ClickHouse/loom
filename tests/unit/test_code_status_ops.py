"""code.index_status freshness fields, code.repos per-entry authz, and the
code-namespace directory registration — the backend of the dashboard's Repositories page."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app import context as ctx_mod
from server.code.app import index_status, repos
from server.code.app import ingest as refresh_mod
from server.code.app.index import registry as reg_mod
from server.code.domain.index.repos import RepoConfig
from server.operations.base import Role
from server.settings import settings


@pytest.fixture(autouse=True)
def _enable(monkeypatch: pytest.MonkeyPatch):
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


_STATE = {
    "git_head": {"value": "a" * 40, "detail": "base 1234", "updated_at": "2026-09-08 10:00:00"},
    "scip_commit": {"value": "b" * 40, "detail": "", "updated_at": "2026-09-08 09:00:00"},
    "last_refresh_at": {
        "value": "2026-09-08T09:30:00+00:00",
        "detail": "agent=ci",
        "updated_at": "x",
    },
}


class _FakeRepo:
    async def all_state(self, org: str, ns: str) -> dict[str, dict[str, Any]]:
        return dict(_STATE)

    async def table_counts(self, org: str, ns: str, **_: Any) -> dict[str, int]:
        return {"code_files": 3, "code_chunks": 10}

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return "a" * 40 if key == "git_head" else ""

    async def coverage_gaps(self, org: str, ns: str) -> dict[str, int]:
        return {
            "functions_scanned": 122579,
            "functions_uncalled": 66788,
            "source_files": 8900,
            "source_files_untested": 4859,
            "code_files": 12636,
            "code_files_no_symbols": 1624,
        }

    async def coverage(self, org: str, ns: str) -> dict[str, Any]:
        return {
            "files": [
                {
                    "lang": "cpp",
                    "files": 9983,
                    "errored": 0,
                    "first_at": "2026-09-08 05:36:38",
                    "last_at": "2026-09-08 22:52:48",
                },
                {
                    "lang": "python",
                    "files": 2620,
                    "errored": 1,
                    "first_at": "2026-09-08 05:37:33",
                    "last_at": "2026-09-08 22:52:55",
                },
            ],
            "chunks": [
                {
                    "lang": "cpp",
                    "chunks": 155919,
                    "embedded": 155913,
                    "described": 6587,
                    "last_at": "2026-09-08 22:52:43",
                },
                {
                    "lang": "",
                    "chunks": 66,
                    "embedded": 66,
                    "described": 0,
                    "last_at": "2026-08-23 08:49:15",
                },
            ],
            "symbols": [
                {
                    "lang": "cpp",
                    "tier": "treesitter",
                    "symbols": 103718,
                    "last_at": "2026-09-08 21:17:10",
                },
                {
                    "lang": "cpp",
                    "tier": "scip",
                    "symbols": 207238,
                    "last_at": "2026-09-08 21:31:14",
                },
                {
                    "lang": "python",
                    "tier": "scip",
                    "symbols": 19127,
                    "last_at": "2026-09-08 23:03:12",
                },
            ],
            "edges": [
                {"tier": "treesitter", "edges": 206365, "last_at": "2026-09-08 22:52:54"},
                {"tier": "scip", "edges": 1226159, "last_at": "2026-09-08 23:03:12"},
            ],
            "misc": {
                "pr_from": "2026-05-14 00:15:11",
                "pr_to": "2026-08-24 06:37:26",
                "pr_last_at": "2026-08-24 10:50:06",
                "pack_last_at": "2026-09-08 22:52:55",
                "summary_last_at": "1970-01-01 00:00:00",
            },
        }

    async def list_context_packs(self, org, ns, *, limit, offset, pr_number=0):
        rows = [
            {
                "pr_number": 7,
                "merge_commit_sha": b"c" * 40,
                "pack_tokens": 900,
                "created_at": "2026-09-08 08:00:00",
                "title": "Fix",
                "author": "u",
                "merged_at": "2026-09-08 07:00:00",
            },
        ]
        return rows[offset : offset + limit], 1


class _FakeGit:
    tip = "c" * 40

    @staticmethod
    async def head_sha(git_dir: str, branch: str) -> str:
        return _FakeGit.tip

    @staticmethod
    async def commit_count_between(git_dir: str, since: str, until: str) -> int:
        if since == "a" * 40:
            return 2
        if since == "b" * 40:
            raise RuntimeError("not an ancestor")
        return 0


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch):
    index_status.reset_repo_status_cache()  # statuses are cached per (org, ns)
    monkeypatch.setattr(index_status, "CodeRepo", _FakeRepo)
    monkeypatch.setattr(ctx_mod, "CodeRepo", _FakeRepo)
    monkeypatch.setattr(repos, "CodeRepo", _FakeRepo)
    import server.code.app.index.gitrepo as gitrepo

    monkeypatch.setattr(gitrepo, "head_sha", _FakeGit.head_sha)
    monkeypatch.setattr(gitrepo, "commit_count_between", _FakeGit.commit_count_between)
    from server.code.app.index import mirrors

    monkeypatch.setattr(mirrors, "_present", lambda cfg: True)  # the fake git's


@pytest.mark.asyncio
async def test_index_status_reports_lag_and_refresh_breadcrumb() -> None:
    from server.code.api.models_status import CodeIndexStatusInput

    out = await index_status.handle_code_index_status(CodeIndexStatusInput())
    assert out.namespace == "code-clickhouse" and out.branch == "master"
    assert out.clone_head == "c" * 40
    assert out.fast_tier_lag == 2  # two merges behind the fetched tip
    assert out.scip_lag == -1  # unknown: watermark not an ancestor
    assert out.scip_py_lag == -1  # unset watermark
    assert out.last_refresh_at.startswith("2026-09-08T09:30")
    assert out.last_refresh_detail == "agent=ci"
    assert out.state["git_head"]["detail"] == "base 1234"
    # An entry that names no prefixes covers its whole tree
    assert out.path_prefixes == [""]


@pytest.mark.asyncio
async def test_index_status_uses_repo_entry_branch_not_scalar() -> None:
    from server.code.api.models_status import CodeIndexStatusInput

    out = await index_status.handle_code_index_status(
        CodeIndexStatusInput(org="clickhouse", namespace="code-clickhouse-25-8")
    )
    assert out.branch == "25.8"


@pytest.mark.asyncio
async def test_index_status_survives_missing_clone(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.code.app.index.gitrepo as gitrepo
    from server.code.api.models_status import CodeIndexStatusInput

    async def _boom(git_dir: str, branch: str) -> str:
        raise RuntimeError("no such dir")

    monkeypatch.setattr(gitrepo, "head_sha", _boom)
    out = await index_status.handle_code_index_status(CodeIndexStatusInput())
    assert out.clone_head == "" and out.fast_tier_lag == -1
    assert out.git_head == "a" * 40  # watermarks still render


class _Auth:
    """Minimal AuthInfo stand-in: readable set drives check_authorized."""

    def __init__(self, readable: set[str], admin: bool = False, org: str = "clickhouse"):
        self.readable = readable
        self.admin = admin
        self.org = org

    def has_role(self, required: Role) -> bool:
        return self.admin and required.value <= Role.ADMIN.value


@pytest.mark.asyncio
async def test_code_repos_filters_to_readable_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.auth.authz as authz
    import server.auth.context as actx
    from server.code.api.models_repos import CodeReposInput

    async def _check(auth, required_role, org=None, namespace=None, **_kw):
        if namespace not in auth.readable:
            raise PermissionError("nope")

    monkeypatch.setattr(authz, "check_authorized", _check)
    monkeypatch.setattr(actx, "current_auth", lambda: _Auth({"code-clickhouse-25-8"}))
    out = await repos.handle_code_repos(CodeReposInput())
    # The entry this caller may read carries its index data;
    # the one it may still ask to join is listed request-only — name and
    # branch, nothing from the index.
    by_ns = {r.namespace: r for r in out.repos}
    assert set(by_ns) == {"code-clickhouse", "code-clickhouse-25-8"}
    ok = by_ns["code-clickhouse-25-8"]
    assert ok.readable and ok.branch == "25.8" and ok.fast_tier_lag == 2
    assert ok.indexed_at == "2026-09-08 10:00:00"
    ask = by_ns["code-clickhouse"]
    assert ask.readable is False and ask.git_head == "" and ask.counts == {}


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("fatal: unable to access 'https://other-org.example/x.git/'", "git clone failed"),
        ("clone failed: PermissionError: [Errno 13] /srv/loom/x.git", "git clone failed"),
        ("bwrap: Can't mount proc on /srv/loom/sandbox/newroot/proc", "git clone failed"),
        ("git clone failed (exit 128)", "git clone failed (exit 128)"),
        ("clone failed: PermissionError", "clone failed: PermissionError"),
        ("clone timed out; retrying next pass", "clone timed out; retrying next pass"),
        ("RuntimeError", "git clone failed"),  # a raised job's kind is never a stage reason
        ("cancelled", "git clone failed"),
        ("ERROR: Repository not found.", "git clone failed"),
        ("FATAL: remote rejected ssh://git@10.0.0.7/acme.git", "git clone failed"),
        (
            "repository job refused: LOOM_CODE_SANDBOX_REQUIRED is set and the sandbox "
            "cannot run here (the 'sandbox probe failed' warning says why)",
            "repository job refused: LOOM_CODE_SANDBOX_REQUIRED is set and the sandbox "
            "cannot run here (the 'sandbox probe failed' warning says why)",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_stored_clone_reason_is_shown_only_in_a_shape_this_build_writes(
    monkeypatch: pytest.MonkeyPatch, stored: str, shown: str
) -> None:
    """`code.repos` and `code.index_status` return the bootstrap stage's JSON detail;
    a reason an older build wrote (git's stderr, a path) reads as the fixed reason."""
    import json

    import server.auth.context as actx
    from server.code.api.models_repos import CodeReposInput
    from server.code.api.models_status import CodeIndexStatusInput

    detail = json.dumps({"reason": stored})
    monkeypatch.setitem(
        _STATE, "bootstrap_stage", {"value": "error", "detail": detail, "updated_at": "x"}
    )
    monkeypatch.setattr(actx, "current_auth", lambda: None)
    listed = await repos.handle_code_repos(CodeReposInput(org="clickhouse"))
    assert json.loads(listed.repos[0].stage_detail) == {"reason": shown}
    index_status.reset_repo_status_cache()
    status = await index_status.handle_code_index_status(CodeIndexStatusInput())
    assert json.loads(status.state["bootstrap_stage"]["detail"]) == {"reason": shown}


@pytest.mark.asyncio
async def test_code_repos_shows_unreadable_entries_to_flat_admins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.auth.authz as authz
    import server.auth.context as actx
    from server.code.api.models_repos import CodeReposInput

    async def _check(auth, required_role, org=None, namespace=None, **_kw):
        if namespace not in auth.readable:
            raise PermissionError("nope")

    monkeypatch.setattr(authz, "check_authorized", _check)
    monkeypatch.setattr(actx, "current_auth", lambda: _Auth({"code-clickhouse-25-8"}, admin=True))
    out = await repos.handle_code_repos(CodeReposInput())
    by_ns = {r.namespace: r for r in out.repos}
    assert (
        by_ns["code-clickhouse-25-8"].readable and by_ns["code-clickhouse-25-8"].fast_tier_lag == 2
    )
    # visible but flagged, with no index data leaked (bootstrap-only shape)
    ur = by_ns["code-clickhouse"]
    assert ur.readable is False and ur.repo == "ClickHouse/ClickHouse" and ur.branch == "master"
    assert ur.git_head == "" and ur.counts == {}


@pytest.mark.asyncio
async def test_code_repos_lists_all_when_auth_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.auth.context as actx
    from server.code.api.models_repos import CodeReposInput

    monkeypatch.setattr(actx, "current_auth", lambda: None)
    out = await repos.handle_code_repos(CodeReposInput(org="clickhouse"))
    assert [r.namespace for r in out.repos] == ["code-clickhouse", "code-clickhouse-25-8"]
    # The caller's own org is authz identity, not a filter: dev mode's "dev"
    # user (or a federated login) still sees the entries it may read.
    out = await repos.handle_code_repos(CodeReposInput(org="dev"))
    assert len(out.repos) == 2 and out.repos[0].org == "clickhouse"


@pytest.mark.asyncio
async def test_code_repos_is_org_level_viewer_op() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["code.repos"]
    # The listing is authorized per entry by the handler; a transport gate on the
    # default code namespace would refuse the whole page to anyone who has not
    # joined that one entry.
    assert op.required_role == Role.VIEWER and op.idempotent and op.skip_namespace_authz
    assert OPERATIONS_BY_NAME["code.context_packs"].required_role == Role.VIEWER


@pytest.mark.asyncio
async def test_context_packs_decorates_with_pr_history() -> None:
    from server.code.api.models_context import CodeContextPacksInput

    out = await ctx_mod.handle_code_context_packs(CodeContextPacksInput(limit=10))
    assert out.total == 1
    row = out.rows[0]
    assert row.pr_number == 7 and row.title == "Fix" and row.merge_commit_sha == "c" * 40
    assert row.merged_at.startswith("2026-09-08")


@pytest.mark.asyncio
async def test_coverage_groups_by_language_and_tier_with_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_status import CodeCoverageInput

    monkeypatch.setattr(settings, "code_pr_history_enabled", False)
    out = await index_status.handle_code_coverage(CodeCoverageInput())
    assert out.as_of_commit == "a" * 40
    by = {row.lang: row for row in out.languages}
    assert [row.lang for row in out.languages] == ["cpp", "python", ""]  # most files first
    cpp = by["cpp"]
    assert cpp.files == 9983 and cpp.files_last_at == "2026-09-08 22:52:48"
    assert cpp.chunks_embedded == 155913 and cpp.chunks_described == 6587
    assert cpp.symbols_treesitter == 103718 and cpp.symbols_scip == 207238
    assert cpp.symbols_scip_last_at == "2026-09-08 21:31:14"
    py = by["python"]
    assert py.files_errored == 1 and py.symbols_scip == 19127 and py.symbols_treesitter == 0
    assert by[""].chunks == 66  # rows whose file is outside code_files
    assert {t.tier: t.edges for t in out.tiers} == {"treesitter": 206365, "scip": 1226159}
    assert out.pr_history_to == "2026-08-24 06:37:26" and out.pr_history_enabled is False
    assert out.summaries_last_at == ""  # epoch == unknown
    # the honesty column: what the index does not know, as shares
    assert out.gaps is not None
    assert out.gaps.functions_uncalled == 66788 and out.gaps.uncalled_share == 0.5449
    assert out.gaps.untested_share == 0.546 and out.gaps.no_symbols_share == 0.1285


def test_gaps_from_counts_handles_empty_namespace() -> None:
    from server.code.api.models_status import CodeCoverageGaps

    g = index_status.gaps_from_counts(CodeCoverageGaps, {})
    assert g.functions_scanned == 0 and g.uncalled_share == 0.0 and g.no_symbols_share == 0.0


@pytest.mark.asyncio
async def test_refresh_writes_last_refresh_breadcrumb(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_ingest import CodeRefreshInput
    from server.code.app.index import ingest_pass as worker_mod
    from server.code.app.index.pass_budget import CodeIngestResult

    written: list[tuple[str, str, str]] = []

    class _R:
        async def get_state(self, org, ns, key):
            return "h"

        async def set_state(self, org, ns, key, value, detail=""):
            written.append((ns, key, detail))

    async def _pass(org: str = "", namespace: str = "") -> CodeIngestResult:
        return CodeIngestResult()

    async def _no_lease(org: str, ns: str) -> None:
        return None

    monkeypatch.setattr(refresh_mod, "CodeRepo", _R)
    monkeypatch.setattr(refresh_mod, "_REFRESH_LOCKS", {})
    monkeypatch.setattr(refresh_mod, "_take_lease", _no_lease)
    monkeypatch.setattr(worker_mod, "run_pass", _pass)
    out = await refresh_mod.handle_code_refresh(
        CodeRefreshInput(agent="ci-bot", expected_head="d" * 40, wait=True)
    )
    assert out.triggered
    assert written == [
        ("code-clickhouse", "last_refresh_at", f"agent=ci-bot expected_head={'d' * 12}")
    ]


class _FakeCH:
    def __init__(self, existing: bool):
        self.existing = existing
        self.inserted: list[dict[str, Any]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        return {"namespace": params["ns"]} if self.existing else None

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.extend(rows)


@pytest.mark.asyncio
async def test_registry_inserts_code_kind_row_once() -> None:
    reg_mod.reset_for_tests()
    cfg = RepoConfig(
        org="clickhouse",
        namespace="code-clickhouse",
        repo="ClickHouse/ClickHouse",
        git_dir="x",
        git_remote="r",
        git_branch="master",
        path_prefixes=("src/",),
        github_token="",
    )
    ch = _FakeCH(existing=False)
    assert await reg_mod.ensure_registered(cfg, ch=ch) is True
    row = ch.inserted[0]
    assert row["kind"] == "code" and row["visibility"] == "private"
    assert row["active"] is True and "ClickHouse/ClickHouse @ master" in row["description"]
    # second call is a no-op without touching CH
    assert await reg_mod.ensure_registered(cfg, ch=_FakeCH(existing=False)) is False


@pytest.mark.asyncio
async def test_registry_registers_public_when_org_readable() -> None:
    reg_mod.reset_for_tests()
    cfg = RepoConfig(
        org="clickhouse",
        namespace="code-clickhouse",
        repo="r",
        git_dir="x",
        git_remote="r",
        git_branch="master",
        path_prefixes=(),
        github_token="",
        org_readable=True,
    )
    ch = _FakeCH(existing=False)
    assert await reg_mod.ensure_registered(cfg, ch=ch) is True
    # The directory row never widens reads; org_readable is a
    # config rule authz applies on its own, and the row stays private.
    assert ch.inserted[0]["visibility"] == "private"


def test_repos_json_org_readable_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.domain.index.repos import configured_repos

    monkeypatch.setattr(settings, "code_org_readable", True)
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"namespace": "code-clickhouse"}, {"namespace": "code-secret", "org_readable": false}]',
    )
    by_ns = {c.namespace: c for c in configured_repos()}
    assert by_ns["code-clickhouse"].org_readable is True  # inherits the setting
    assert by_ns["code-secret"].org_readable is False  # explicit per-entry opt-out


@pytest.mark.asyncio
async def test_registry_never_rewrites_existing_row() -> None:
    reg_mod.reset_for_tests()
    cfg = RepoConfig(
        org="clickhouse",
        namespace="code-clickhouse",
        repo="r",
        git_dir="x",
        git_remote="r",
        git_branch="master",
        path_prefixes=(),
        github_token="",
    )
    ch = _FakeCH(existing=True)
    assert await reg_mod.ensure_registered(cfg, ch=ch) is False
    assert ch.inserted == []


def test_namespace_register_input_kind_defaults_to_preserve() -> None:
    from server.identity.api.models import NamespaceRegisterInput

    inp = NamespaceRegisterInput(org="o", namespace="n", owner_email="e")
    assert inp.kind is None  # handler keeps existing kind / 'memory' for fresh rows
    assert (
        NamespaceRegisterInput(org="o", namespace="n", owner_email="e", kind="code").kind == "code"
    )


@pytest.mark.asyncio
async def test_index_status_reads_worker_passes_from_the_ledger_not_this_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The web container never runs a worker, so its process-local registry
    says "never" for every code_* worker; the ledger (`loom.worker_status`)
    is what the workers container writes and is what the status reports."""
    from server.code.api.models_status import CodeIndexStatusInput

    async def _ledger(days: int = 14):
        return {
            "code_pr_history": {
                "last_run_at": "2026-09-18T06:27:52+00:00",
                "last_duration_ms": 4120,
                "consecutive_failures": 0,
                "total_runs": 99,
                "last_error_kind": "",
            }
        }

    monkeypatch.setattr(index_status, "_worker_passes_from_ledger", _ledger)
    out = await index_status.handle_code_index_status(CodeIndexStatusInput())
    w = out.workers.get("code_pr_history")
    assert w is not None and w.last_run_at.startswith("2026-09-18T06:27") and w.total_runs == 99
    assert (
        w.last_duration_ms == 4120 and w.interval_seconds > 0
    )  # interval still from the registry meta


@pytest.mark.asyncio
async def test_a_failed_worker_pass_shows_a_viewer_its_kind_not_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Code workers run for every org, so a viewer's status reads the ledger's error
    class (or this replica's `caller_text`), never the error's text."""
    from server.code.api.models_status import CodeIndexStatusInput
    from server.workers.status import get_registry, record_pass

    text = "Code: 210. Connection refused (ch-internal-7:9440) SELECT * FROM loom.memory"

    class _Ledger:
        """Answers the pass summary with what its columns select, the error's text too."""

        sql = ""

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            _Ledger.sql = sql
            row = {
                "worker": "code_pr_history",
                "last_error": f"RuntimeError: {text}",
                "last_error_class": "RuntimeError",
                "last_status": "failed",
                "consecutive_failures": 1,
                "total_runs": 2,
            }
            selected = {"worker", "last_status", "consecutive_failures", "total_runs"}
            selected |= {c for c in ("last_error", "last_error_class") if f"AS {c}," in sql}
            return [{k: v for k, v in row.items() if k in selected}]

    import server.core.ch as ch_mod

    index_status._ledger_passes_cache.clear()
    monkeypatch.setattr(ch_mod, "get_client", lambda: _Ledger())
    try:
        out = await index_status.handle_code_index_status(CodeIndexStatusInput())
    finally:
        index_status._ledger_passes_cache.clear()
    assert out.workers["code_pr_history"].last_error == "RuntimeError"
    assert "AS last_error_class," in _Ledger.sql and "AS last_error," not in _Ledger.sql

    async def _none(days: int = 14):
        return {}

    monkeypatch.setattr(index_status, "_worker_passes_from_ledger", _none)
    with pytest.raises(RuntimeError), record_pass("code_pr_history"):
        raise RuntimeError(text)
    try:
        out = await index_status.handle_code_index_status(CodeIndexStatusInput())
    finally:
        get_registry().reset()
    assert out.workers["code_pr_history"].last_error == "RuntimeError"


@pytest.mark.asyncio
async def test_index_status_falls_back_to_the_registry_when_the_ledger_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_status import CodeIndexStatusInput

    async def _ledger(days: int = 14):
        return {}

    monkeypatch.setattr(index_status, "_worker_passes_from_ledger", _ledger)
    out = await index_status.handle_code_index_status(CodeIndexStatusInput())
    assert (
        "code_pr_history" in out.workers
    )  # rendered from the registry meta, last_run_at empty here


@pytest.mark.asyncio
async def test_a_commit_question_gets_the_compact_answer_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poller asking about one commit gets identity,
    watermarks, lags, tiers and commit_coverage — not the 99 KB `state` dump
    or the op catalogue. `brief=False` restores them; no `commit` = unchanged."""
    from server.code.api.models_status import CodeIndexStatusInput
    from server.code.app.index import commit_coverage as cc

    async def _cov(git_dir, commit, head, tiers, paths=None, gap=""):
        return {
            "commit": commit,
            "in_mirror": True,
            "fast": True,
            "tiers": [],
            "ready": True,
            "waiting_on": [],
        }

    monkeypatch.setattr(cc, "commit_coverage", _cov)
    out = await index_status.handle_code_index_status(CodeIndexStatusInput(commit="a" * 40))
    assert out.commit_coverage is not None and out.commit_coverage.ready
    assert out.state == {} and out.ops == [] and out.request_fields == {} and out.workers == {}
    assert out.namespace == "code-clickhouse" and out.fast_tier_lag == 2  # the watermark facts stay
    full = await index_status.handle_code_index_status(
        CodeIndexStatusInput(commit="a" * 40, brief=False)
    )
    assert full.state and full.ops
    plain = await index_status.handle_code_index_status(CodeIndexStatusInput())
    assert plain.commit_coverage is None and plain.state and plain.ops
