"""A tier that fails on an entry is recorded per entry with its kind and the one fix,
counts its failed passes in a row, and clears on the next pass that succeeds; the
record reaches `as_of`, code.index_status and code.repos."""

from __future__ import annotations

import importlib
import json
import sys
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index import compiler_tier
from server.code.app.index import tier_health as recorder
from server.code.domain.index import tier_health
from server.workers import pass_context

# A C++ tier's error for a private submodule with no credential.
SUBMODULE_NO_TOKEN = (
    "RuntimeError: git submodule failed: fatal: could not read Username for "
    "'https://github.com': terminal prompts disabled\n"
    "fatal: Fetched in submodule path 'contrib/amber', but it did not contain "
    "2d70290e18f53e91cd093db820370d4ba571b366. Direct fetching of that commit failed.\n"
)
NO_TOKEN_SHORT = (
    "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
)
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
FAILING_FULL = tier_health.failed(None, "clang_full", "harness exit 1: x", T0).to_json()


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (SUBMODULE_NO_TOKEN, "credential"),
        ("fatal: Authentication failed for 'https://github.com/o/r.git/'", "credential"),
        ("remote: Repository not found.", "credential"),
        (
            "fatal: Fetched in submodule path 'contrib/x', but it did not contain "
            "abcdef1234567. Direct fetching of that commit failed.",
            "pin_missing",
        ),
        ("error: Server does not allow request for unadvertised object abc1234", "pin_missing"),
        (
            "fatal: unable to access 'https://h.example/r/': Received HTTP code 403 from "
            "proxy after CONNECT",
            "egress",
        ),
        ("fatal: unable to access 'https://h/': CONNECT tunnel failed, response 405", "egress"),
        (
            "fatal: unable to access 'https://h/': Received HTTP code 502 from proxy after CONNECT",
            "network",
        ),
        ("fatal: unable to access 'https://h/': CONNECT tunnel failed, response 503", "network"),
        ("fatal: unable to access 'https://h/': Could not resolve host: h", "network"),
        (
            "fatal: unable to access 'https://h/o/sub/': Received HTTP code 403 from proxy after "
            "CONNECT\nfatal: Fetched in submodule path 'contrib/sub', but it did not contain "
            "2d70290e18f5. Direct fetching of that commit failed.",
            "egress",
        ),
        (
            "fatal: unable to access 'https://h/o/sub/': Could not resolve host: h\n"
            "fatal: Fetched in submodule path 'contrib/sub', but it did not contain "
            "2d70290e18f5. Direct fetching of that commit failed.",
            "network",
        ),
        (
            "harness exit 1: CMake Error: Could not resolve host: h\n>> cmake configure failed\n",
            "cmake",
        ),
        (
            "harness exit 1: CMake Error: No space left on device\n>> cmake configure failed\n",
            "disk",
        ),
        ("RuntimeError: harness timed out after 600.0s", "timeout"),
        ("TimeoutError: git fetch origin in /c exceeded 600s", "timeout"),
        ("write error: No space left on device", "disk"),
        ("RuntimeError: harness exit 2: cmake: error in CMakeLists.txt", "harness"),
        ("ValueError: something else", "unknown"),
        ("harness exit 1: curl: (22) The requested URL returned error: 404", "harness"),
        (
            "harness exit 255: error: cannot open '.git/FETCH_HEAD': Permission denied",
            "permission",
        ),
        ("PermissionError: [Errno 13] Permission denied: '/r/checkout/.git/index'", "permission"),
        ("PermissionError: [Errno 1] Operation not permitted: '/r/checkout'", "permission"),
        ('PermissionError: [Errno 13] Permission denied: "/r/docs/it\'s.md"', "permission"),
        ("PermissionError: [Errno 13] Permission denied: b'/r/x'", "permission"),
        (
            "fatal: Unable to create '/r/checkout/.git/index.lock': Permission denied",
            "permission",
        ),
        ("bwrap: setting up uid map: Permission denied", "unknown"),
        ("harness exit 1: x\nPermissionError: [Errno 1] Operation not permitted", "harness"),
        ("harness exit 1: registry: insufficient permissions for this scope", "harness"),
        ("harness exit 1: ld: Operation not permitted", "harness"),
        ("git@github.com: Permission denied (publickey).", "credential"),
        ("git@h.example: Permission denied (publickey,password).", "credential"),
        ("git@h.example: Permission denied (password).", "credential"),
        (
            "error: insufficient permission for adding an object to repository database "
            "./objects\nfatal: failed to write object",
            "permission",
        ),
    ],
)
def test_an_error_is_classified_into_its_kind(text: str, kind: str) -> None:
    assert tier_health.classify(text) == kind


def test_a_harness_exit_that_names_a_network_error_is_a_network_failure() -> None:
    text = "RuntimeError: harness exit 1: fatal: unable to access: Connection refused"
    assert tier_health.classify(text) == "network"


def test_a_file_error_is_recorded_with_the_file_and_a_permission_fix() -> None:
    error = PermissionError(13, "Permission denied", "/r/checkout/.git/FETCH_HEAD")
    text = recorder.error_text(error)
    assert text == "PermissionError: [Errno 13] Permission denied: '/r/checkout/.git/FETCH_HEAD'"
    health = tier_health.failed(None, "clang_full", text, T0)
    assert health.kind == "permission"
    assert "LOOM_CODE_REPOS_SHARED_GID" in health.fix


def test_an_os_error_without_a_file_is_recorded_by_its_kind_only() -> None:
    assert recorder.error_text(ConnectionRefusedError(111, "refused by 10.0.0.1")) == (
        "ConnectionRefusedError"
    )


@pytest.mark.parametrize(
    "text", ["git@h: Permission denied (publickey).", "git@h: Permission denied (password)."]
)
def test_an_ssh_refusal_gets_the_ssh_fix(text: str) -> None:
    assert "ssh key" in tier_health.fix_for("credential", text)


def test_a_credential_fix_names_the_host_the_submodule_and_the_setting() -> None:
    fix = tier_health.fix_for("credential", SUBMODULE_NO_TOKEN)
    assert "github.com" in fix and "submodule contrib/amber" in fix
    assert "LOOM_CODE_GITHUB_TOKEN" in fix and "github_token" in fix


@pytest.mark.parametrize(
    "secret",
    [
        "https://x-access-token:ghs_abcdefghijklmnopqrstuvwxyz0123@github.com/o/r",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "Authorization: Basic eC1hY2Nlc3MtdG9rZW46c2VjcmV0",
        "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
    ],
)
def test_a_recorded_error_never_holds_a_credential(secret: str) -> None:
    health = tier_health.failed(None, "fetch", f"fatal: {secret} refused", T0)
    for part in ("ghs_abc", "ghp_abc", "eC1hY2Nl", "github_pat_11"):
        assert part not in health.error


def test_failures_count_in_a_row_from_the_first_and_a_success_clears_them() -> None:
    first = tier_health.failed(None, "clang", SUBMODULE_NO_TOKEN, T0)
    second = tier_health.failed(first, "clang", SUBMODULE_NO_TOKEN, T0 + timedelta(minutes=30))
    assert (second.state, second.kind, second.failed_passes) == ("failing", "credential", 2)
    assert second.failing_since == T0.isoformat(timespec="seconds")
    assert second.last_failed_at == (T0 + timedelta(minutes=30)).isoformat(timespec="seconds")
    ok = tier_health.recovered("clang", T0 + timedelta(hours=1))
    assert not ok.failing and ok.failed_passes == 0 and ok.failing_since == ""
    again = tier_health.failed(ok, "clang", "harness exit 1: x", T0 + timedelta(hours=2))
    assert again.failed_passes == 1 and again.failing_since == again.last_failed_at
    assert tier_health.parse("clang", again.to_json()) == again


def test_an_unreadable_record_reads_as_none() -> None:
    assert tier_health.parse("clang", "") is None
    assert tier_health.parse("clang", "not json") is None
    assert tier_health.parse("clang", "[1]") is None
    assert tier_health.parse("clang", '{"state": "failing", "failed_passes": "x"}') is None


class _StateRepo:
    """`get_state` / `set_state` over a dict, as code_index_state answers them."""

    def __init__(self, state: dict[str, str] | None = None) -> None:
        self.state = dict(state or {})
        self.writes: list[tuple[str, str]] = []

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return self.state.get(key, "")

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        self.state[key] = value
        self.writes.append((key, value))


@pytest.mark.asyncio
async def test_record_writes_a_failure_clears_it_once_and_then_writes_nothing() -> None:
    repo = _StateRepo()
    assert await recorder.record(repo, "o", "ns", "clang") is None  # healthy: no write
    assert repo.writes == []
    await recorder.record(repo, "o", "ns", "clang", recorder.ToolOutputError(SUBMODULE_NO_TOKEN))
    await recorder.record(repo, "o", "ns", "clang", recorder.ToolOutputError(SUBMODULE_NO_TOKEN))
    stored = json.loads(repo.state["tier_health:clang"])
    assert (stored["state"], stored["kind"], stored["failed_passes"]) == (
        "failing",
        "credential",
        2,
    )
    cleared = await recorder.record(repo, "o", "ns", "clang")
    assert cleared is not None and cleared.state == "ok"
    assert await recorder.record(repo, "o", "ns", "clang") is None
    assert len(repo.writes) == 3


@pytest.mark.asyncio
async def test_a_store_that_fails_never_fails_the_pass() -> None:
    repo = AsyncMock()
    repo.get_state.side_effect = RuntimeError("clickhouse down")
    assert await recorder.record(repo, "o", "ns", "fetch", "boom") is None


class _Cfg:
    org = "o"
    namespace = "code-x"
    git_branch = ""
    scip_producer = ""
    scip_extra_producers: tuple[str, ...] = ()
    source = "config"
    git_dir = "/nonexistent/repo.git"
    git_remote = "/nonexistent/mirror.git"
    scip_checkout_dir = "/nonexistent/checkout"
    label = "o/code-x"
    github_token = ""


async def _run_tier(module: str, repo: _StateRepo, run_repo: Any, *, owns: bool = True) -> Any:
    w = importlib.import_module(module)

    @asynccontextmanager
    async def _lock(*_: Any, **__: Any) -> Any:
        yield

    pass_context.begin("w")
    try:
        with (
            patch("server.code.store.index.CodeRepo", return_value=repo),
            patch("server.code.domain.index.repos.configured_repos", return_value=[_Cfg()]),
            patch.object(compiler_tier, "worker_owns", AsyncMock(return_value=owns)),
            patch.object(compiler_tier, "entry_lock", _lock),
            patch.object(w, "_run_repo", run_repo),
        ):
            return await w.run_pass()
    finally:
        pass_context.clear()


@pytest.mark.asyncio
async def test_a_compiler_tier_records_its_failure_and_clears_it_on_the_next_pass() -> None:
    repo = _StateRepo()

    async def boom(*_: Any) -> None:
        raise recorder.ToolOutputError(SUBMODULE_NO_TOKEN)

    async def ok(*_: Any) -> None:
        return None

    async def skipped(_repo: Any, cfg: Any, result: Any) -> None:
        result.skipped.append(f"{cfg.namespace}: scip-clang missing at /x")

    module = "server.code.workers.scip_delta"
    await _run_tier(module, repo, boom)
    assert tier_health.parse("clang", repo.state["tier_health:clang"]).kind == "credential"
    await _run_tier(module, repo, skipped)  # a skip did not run the tier: still failing
    assert tier_health.parse("clang", repo.state["tier_health:clang"]).failing
    await _run_tier(module, repo, ok)
    assert not tier_health.parse("clang", repo.state["tier_health:clang"]).failing


@pytest.mark.asyncio
async def test_an_entry_the_tier_no_longer_runs_drops_its_record() -> None:
    failing = tier_health.failed(None, "clang", SUBMODULE_NO_TOKEN, T0).to_json()
    full = tier_health.failed(None, "clang_full", "harness exit 1: x", T0).to_json()
    repo = _StateRepo({"tier_health:clang": failing, "tier_health:clang_full": full})

    async def never(*_: Any) -> None:
        raise AssertionError("an entry the tier does not own is not run")

    await _run_tier("server.code.workers.scip_delta", repo, never, owns=False)
    assert tier_health.from_state({k: {"value": v} for k, v in repo.state.items()}) == {}
    writes = len(repo.writes)
    await _run_tier("server.code.workers.scip_delta", repo, never, owns=False)
    assert len(repo.writes) == writes  # nothing left to drop: no write


@pytest.mark.asyncio
async def test_a_gone_base_is_not_a_failure_to_report() -> None:
    repo = _StateRepo()

    async def gone(*_: Any) -> None:
        raise compiler_tier.BaseUnknown("base gone")

    await _run_tier("server.code.workers.scip_py_delta", repo, gone)
    assert "tier_health:python" not in repo.state


@pytest.mark.asyncio
async def test_entry_lock_with_a_tier_relays_the_output_and_records_the_exit(
    capfd: pytest.CaptureFixture[str],
) -> None:
    from server.cli import code_scip

    tail = bytearray()
    rc = await code_scip._run_while_held(
        [sys.executable, "-c", "import sys; print('out'); sys.exit('Could not resolve host: h')"],
        tail=tail,
    )
    assert rc == 1 and b"Could not resolve host: h" in bytes(tail)
    assert "out" in capfd.readouterr().out
    repo = _StateRepo()
    with patch("server.code.store.index.CodeRepo", return_value=repo):
        await code_scip._record_tier("o", "ns", "python", rc, bytes(tail))
        health = tier_health.parse("python", repo.state["tier_health:python"])
        assert health is not None and health.kind == "network"
        assert health.error.startswith("harness exit 1:")
        await code_scip._record_tier("o", "ns", "python", 0, b"")
    assert not tier_health.parse("python", repo.state["tier_health:python"]).failing


def _state(**values: str) -> dict[str, Any]:
    return {k: {"value": v} for k, v in values.items()}


def test_as_of_lists_only_the_failing_tiers_with_the_fix_and_the_lag() -> None:
    from server.code.app.common import failing_tiers

    failing = tier_health.failed(None, "clang", SUBMODULE_NO_TOKEN, T0).to_json()
    ok = tier_health.recovered("fetch", T0).to_json()
    state = _state(
        **{"tier_health:clang": failing, "tier_health:fetch": ok, "scip_commit": "a" * 40}
    )
    rows = failing_tiers(state, None)
    assert [r["tier"] for r in rows] == ["clang"]
    assert rows[0]["kind"] == "credential" and rows[0]["failed_passes"] == 1
    assert rows[0]["failing_since"] == T0.isoformat(timespec="seconds")
    assert "LOOM_CODE_GITHUB_TOKEN" in rows[0]["fix"] and rows[0]["lag_commits"] == -1


def test_status_rows_carry_each_recorded_tier_with_its_own_lag() -> None:
    from server.code.app.index_status import tier_health_rows

    state = _state(
        **{
            "tier_health:clang": tier_health.failed(
                None, "clang", "harness exit 1: x", T0
            ).to_json(),
            "tier_health:ingest": tier_health.recovered("ingest", T0).to_json(),
        }
    )
    rows = {r.tier: r for r in tier_health_rows(state, {"git_head": 0, "scip_commit": 413})}
    assert rows["clang"].state == "failing" and rows["clang"].lag_commits == 413
    assert rows["clang"].kind == "harness"
    assert rows["ingest"].state == "ok" and rows["ingest"].lag_commits == 0


def test_a_fix_names_the_repository_url_over_a_bare_host() -> None:
    text = (
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
        "fatal: clone of 'https://github.com/o/private.git' into submodule path "
        "'/srv/loom/checkout/contrib/private' failed"
    )
    fix = tier_health.fix_for("credential", text)
    # with no relative form in the output, the path as git printed it
    assert "(github.com/o/private, submodule /srv/loom/checkout/contrib/private)" in fix


@pytest.mark.asyncio
async def test_entry_lock_records_a_stopped_command_as_a_timeout() -> None:
    from server.cli import code_scip

    repo = _StateRepo()
    with patch("server.code.store.index.CodeRepo", return_value=repo):
        await code_scip._record_tier("o", "ns", "python", -15, b"", stopped=True)
        assert tier_health.parse("python", repo.state["tier_health:python"]).kind == "timeout"
        # killed by something other than the caller's limit (the OOM killer): not a timeout
        await code_scip._record_tier("o", "ns", "python", -9, b"Killed")
        assert tier_health.parse("python", repo.state["tier_health:python"]).kind == "harness"
        # stopped, but it finished first: the run succeeded
        await code_scip._record_tier("o", "ns", "python", 0, b"", stopped=True)
    assert not tier_health.parse("python", repo.state["tier_health:python"]).failing


@pytest.mark.asyncio
async def test_a_lost_lease_records_nothing() -> None:
    from server.code.app.index.entry_lock import EntryLockLost

    repo = _StateRepo()

    async def lost(*_: Any) -> None:
        raise EntryLockLost("entry lease lost")

    await _run_tier("server.code.workers.scip_delta", repo, lost)
    assert repo.writes == []


@pytest.mark.asyncio
async def test_the_full_run_records_its_own_tier_and_an_entry_not_due_records_nothing() -> None:
    repo = _StateRepo()

    async def not_due(*_: Any) -> bool:
        return False

    async def boom(*_: Any) -> bool:
        raise RuntimeError("harness exit 1: cmake failed")

    async def ran(*_: Any) -> bool:
        return True

    module = "server.code.workers.scip_full"
    await _run_tier(module, repo, boom)
    assert tier_health.parse("clang_full", repo.state["tier_health:clang_full"]).failing
    assert "tier_health:clang" not in repo.state  # a delta's record is its own
    await _run_tier(module, repo, not_due)
    assert tier_health.parse("clang_full", repo.state["tier_health:clang_full"]).failing
    await _run_tier(module, repo, ran)
    assert not tier_health.parse("clang_full", repo.state["tier_health:clang_full"]).failing


@pytest.mark.asyncio
async def test_a_checkout_gc_failure_is_not_the_tiers_failure(tmp_path: Path) -> None:
    repo = _StateRepo()
    (tmp_path / ".git").mkdir()
    calls: list[str] = []

    async def not_due(*_: Any) -> bool:
        return False

    async def denied(git_dir: str) -> None:
        calls.append(git_dir)
        raise PermissionError(13, "Permission denied", f"{git_dir}/loom-gc")

    with (
        patch.object(compiler_tier, "checkout_for", return_value=str(tmp_path)),
        patch("server.code.app.index.mirrors.gc_if_due", denied),
    ):
        result = await _run_tier("server.code.workers.scip_full", repo, not_due)
    assert calls == [str(tmp_path / ".git")]
    assert "tier_health:clang_full" not in repo.state
    assert result.errors == []


def test_an_ingest_failure_is_never_overwritten_by_a_later_success_of_the_same_tier() -> None:
    from server.code.app.index.pass_budget import CodeIngestResult

    result = CodeIngestResult()
    cfg = _Cfg()
    result.tier_ok(cfg, "fetch")
    result.tier_failed(cfg, "ingest", "walk: RuntimeError: boom")
    result.tier_ok(cfg, "ingest")
    result.tier_failed(cfg, "ingest", "a second error")
    assert result.tier_outcomes == {
        ("o", "code-x", "fetch"): "",
        ("o", "code-x", "ingest"): "walk: RuntimeError: boom",
    }


@pytest.mark.asyncio
async def test_a_fetch_records_its_outcome(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from server.code.app.index import gitrepo, pass_fetch
    from server.code.app.index.pass_budget import CodeIngestResult

    async def nothing(*_: Any, **__: Any) -> int:
        return 0

    for name in ("fetch_extra_branches", "fetch_missing_merges", "fetch_pr_heads"):
        monkeypatch.setattr(pass_fetch, name, nothing)
    monkeypatch.setattr(pass_fetch, "mark_fetched", lambda _d: None)

    async def refused(*_: Any, **__: Any) -> None:
        raise gitrepo.GitError("fatal: Authentication failed for 'https://github.com/o/r.git/'")

    result = CodeIngestResult()
    monkeypatch.setattr(gitrepo, "fetch", refused)
    assert not await pass_fetch.fetch_entry(_Cfg(), str(tmp_path / "x.git"), None, result)

    async def fetched(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(gitrepo, "fetch", fetched)
    other = type("Other", (_Cfg,), {"namespace": "code-y"})()
    assert await pass_fetch.fetch_entry(other, str(tmp_path / "y.git"), None, result)
    assert result.tier_outcomes[("o", "code-y", "fetch")] == ""
    error = result.tier_outcomes[("o", "code-x", "fetch")]
    assert error.startswith("fetch /nonexistent/mirror.git: ") and "Authentication failed" in error


@pytest.mark.asyncio
async def test_a_failing_leg_records_the_ingest_tier() -> None:
    from server.code.app.index import ingest_pass
    from server.code.app.index.pass_budget import CodeIngestResult, PassBudget

    async def boom(*_: Any) -> None:
        raise RuntimeError("parser crashed")

    plan = type("Plan", (), {"cfg": _Cfg()})()
    result = CodeIngestResult()
    await ingest_pass._run_leg(
        result,
        PassBudget(started=time.monotonic(), stop_after=time.monotonic() + 600),
        [plan],
        "walk",
        boom,
    )
    # `code.index_status` shows the record to a viewer: the kind, not the text.
    assert result.tier_outcomes[("o", "code-x", "ingest")] == "walk: RuntimeError"


@pytest.mark.asyncio
async def test_the_ingest_pass_writes_its_outcomes_at_its_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest_pass

    repo = _StateRepo()

    async def run(result: Any, *_: Any) -> None:
        result.tier_failed(_Cfg(), "fetch", "fetch x: Could not resolve host: x")
        result.tier_ok(_Cfg(), "ingest")

    monkeypatch.setattr(ingest_pass, "_run", run)
    with patch("server.code.store.index.CodeRepo", return_value=repo):
        await ingest_pass.run_pass()
    assert tier_health.parse("fetch", repo.state["tier_health:fetch"]).kind == "network"
    assert "tier_health:ingest" not in repo.state  # ok over no record: nothing to write


def test_as_of_counts_a_compiler_tiers_lag_from_the_published_status() -> None:
    from server.code.app.common import failing_tiers
    from server.code.app.index_status import STATUS_SNAPSHOT_KEY

    tip, mark = "c" * 40, "a" * 40
    snap = {
        "clone_head": tip,
        "compiler_tiers": [{"watermark_key": "scip_commit", "commit": mark, "lag": 413}],
    }
    state = {
        "tier_health:clang": {
            "value": tier_health.failed(None, "clang", NO_TOKEN_SHORT, T0).to_json()
        },
        "scip_commit": {"value": mark},
        "clone_head": {"value": tip, "detail": "main"},
        STATUS_SNAPSHOT_KEY: {"value": json.dumps(snap)},
    }
    cfg = type("Cfg", (), {"git_branch": "main"})()
    (row,) = failing_tiers(state, cfg)
    assert row["lag_commits"] == 413


def test_a_remote_url_credential_is_redacted_from_a_fetch_error() -> None:
    health = tier_health.failed(
        None, "fetch", "fetch https://bot:s3cretvalue@git.example.com/o/r.git: refused", T0
    )
    assert "s3cretvalue" not in health.error
    assert "https://***@git.example.com/o/r.git" in health.error


@pytest.mark.asyncio
async def test_entry_lock_records_nothing_for_a_vanished_base() -> None:
    from server.cli import code_scip
    from server.code.app.index.compiler_tier import BASE_UNKNOWN_EXIT

    repo = _StateRepo()
    with patch("server.code.store.index.CodeRepo", return_value=repo):
        await code_scip._record_tier("o", "ns", "python", BASE_UNKNOWN_EXIT, b"base unknown")
    assert repo.writes == []


@pytest.mark.asyncio
async def test_an_entry_the_budget_cut_is_not_recorded_healthy() -> None:
    from server.code.app.index import ingest_pass
    from server.code.app.index.pass_budget import CodeIngestResult, PassBudget

    served = type("Plan", (), {"cfg": _Cfg()})()
    cut_cfg = type("Cut", (_Cfg,), {"namespace": "code-y", "label": "o/code-y"})()
    cut = type("Plan", (), {"cfg": cut_cfg})()
    result = CodeIngestResult()
    spent = PassBudget(started=time.monotonic() - 10, stop_after=time.monotonic() - 1)

    async def never(*_: Any) -> None:
        raise AssertionError("a cut leg runs nothing")

    await ingest_pass._run_leg(result, spent, [served, cut], "walk", never)
    assert result.unserved == {("o", "code-x"), ("o", "code-y")}
    assert result.resume == "code-x:walk"  # the next pass starts at the first


@pytest.mark.asyncio
async def test_an_entry_a_held_call_stopped_is_not_recorded_healthy() -> None:
    from server.code.app.index import ingest_pass
    from server.code.app.index.pass_budget import CodeIngestResult, PassBudget
    from server.llm import customer

    plan = type("Plan", (), {"cfg": _Cfg()})()
    result = CodeIngestResult()

    async def held(*_: Any) -> None:
        raise customer.HOLDS[0]("held")

    fresh = PassBudget(started=time.monotonic(), stop_after=time.monotonic() + 600)
    await ingest_pass._run_leg(result, fresh, [plan], "walk", held)
    ingest_pass._ingest_served(plan.cfg, result)
    assert result.unserved == {("o", "code-x")}
    assert result.tier_outcomes == {}
    assert not result.partial


@pytest.mark.asyncio
async def test_a_held_commit_is_not_recorded_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import ingest_pass, pass_stream
    from server.code.app.index.pass_budget import CodeIngestResult
    from server.llm import customer

    async def held(*_: Any, **__: Any) -> int:
        raise customer.HOLDS[0]("held")

    async def nothing(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(pass_stream, "_ingest_commit", held)
    monkeypatch.setattr(pass_stream, "_slice_done", nothing)
    result = CodeIngestResult()
    stream = pass_stream.CommitStream(_Cfg(), _StateRepo(), "b" * 40, "a" * 40, ["c" * 40], result)
    stream._ctx = {}
    assert await stream.step(None) is False
    ingest_pass._ingest_served(_Cfg(), result)
    assert result.tier_outcomes == {}


@pytest.mark.asyncio
async def test_a_failed_commit_records_the_ingest_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import pass_stream
    from server.code.app.index.pass_budget import CodeIngestResult

    async def boom(*_: Any, **__: Any) -> int:
        raise RuntimeError("parser crashed")

    async def nothing(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(pass_stream, "_ingest_commit", boom)
    monkeypatch.setattr(pass_stream, "_slice_done", nothing)
    result = CodeIngestResult()
    stream = pass_stream.CommitStream(_Cfg(), _StateRepo(), "b" * 40, "a" * 40, ["c" * 40], result)
    stream._ctx = {}
    assert await stream.step(None) is False
    error = result.tier_outcomes[("o", "code-x", "ingest")]
    assert error == f"commit {'c' * 12}: RuntimeError"


class _FullArgs:
    checkout = ""
    apply_delta = ""
    keep_delta = ""
    jobs = 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["harness", "error", "lease_lost", "ok"])
async def test_scip_full_records_the_full_run(
    outcome: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from server.cli import code_scip
    from server.code.app.index import compiler_tier, scip_full
    from server.code.app.index.entry_lock import EntryLockLost

    for d in ("checkout", "repo.git"):
        (tmp_path / d).mkdir()
    clang = tmp_path / "scip-clang"
    clang.write_text("")
    monkeypatch.setattr(compiler_tier, "resolve_binary", lambda _p: clang)
    monkeypatch.setattr(
        compiler_tier,
        "resolve_checkout",
        lambda _c, _p: (tmp_path / "checkout", tmp_path / "repo.git", None),
    )
    monkeypatch.setattr(compiler_tier, "unsandboxed_skip", lambda *_a, **_k: "")
    raised = {
        "harness": scip_full.FullIndexFailed(1, "cmake failed"),
        "error": recorder.ToolOutputError("git submodule failed: Could not resolve host: h"),
        "lease_lost": EntryLockLost("entry lease lost"),
    }.get(outcome)

    async def full_index(*_: Any, **__: Any) -> Any:
        if raised is not None:
            raise raised
        return type("Stats", (), {"model_dump": lambda self: {}})(), ""

    monkeypatch.setattr(scip_full, "full_index", full_index)
    repo = _StateRepo({"git_head": "b" * 40, "tier_health:clang_full": FAILING_FULL})
    with patch("server.code.store.index.CodeRepo", return_value=repo):
        if outcome in ("error", "lease_lost"):
            with pytest.raises(type(raised)):
                await code_scip._scip_full_locked(_FullArgs(), "o", "ns", _Cfg())
        else:
            await code_scip._scip_full_locked(_FullArgs(), "o", "ns", _Cfg())
    health = tier_health.parse("clang_full", repo.state["tier_health:clang_full"])
    assert health is not None
    if outcome == "lease_lost":
        assert repo.state["tier_health:clang_full"] == FAILING_FULL  # no write
    elif outcome == "ok":
        assert not health.failing
    else:
        assert health.failed_passes == 2
        assert health.kind == {"harness": "harness", "error": "network"}[outcome]


@pytest.mark.parametrize(
    ("text", "fix"),
    [
        (
            "fatal: clone of 'https://github.com/o/p.git' into submodule path "
            "'/srv/c/contrib/deep/p' failed\nFailed to clone 'contrib/deep/p' a second time",
            "submodule contrib/deep/p",
        ),
        ("fetch https://github.com/o/r.git: fatal: could not read Username", "(github.com/o/r)"),
        ("git@github.com: Permission denied (publickey).", "ssh key was refused"),
        (
            "fetch https://git.example.com:8443/o/r: could not read Username",
            "(git.example.com/o/r)",
        ),
        (
            "fetch ssh://git@git.example.com:2222/o/r.git: Permission denied (publickey).",
            "(git.example.com/o/r)",
        ),
    ],
)
def test_a_fix_names_what_the_error_names(text: str, fix: str) -> None:
    assert fix in tier_health.fix_for("credential", text)


def test_a_full_disk_is_disk_even_when_the_transfer_hung_up() -> None:
    text = "error: No space left on device\nfatal: the remote end hung up unexpectedly"
    assert tier_health.classify(text) == "disk"


def test_an_ingest_pass_records_the_parser_tier_ok_only_for_an_entry_served_in_full() -> None:
    from server.code.app.index import ingest_pass
    from server.code.app.index.pass_budget import CodeIngestResult

    cut_cfg = type("Cut", (_Cfg,), {"namespace": "code-y"})()
    other_org = type("Other", (_Cfg,), {"org": "p", "namespace": "code-y"})()
    result = CodeIngestResult()
    result.cut("code-y:walk", cfg=cut_cfg)
    for cfg in (_Cfg(), cut_cfg, other_org):
        ingest_pass._ingest_served(cfg, result)
    # another org's entry of the same namespace was served: the cut is not its
    assert result.tier_outcomes == {("o", "code-x", "ingest"): "", ("p", "code-y", "ingest"): ""}


@pytest.mark.asyncio
async def test_a_failed_clone_records_the_fetch_tier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from server.code.app.index import bootstrap, ingest_pass
    from server.code.app.index.pass_budget import CodeIngestResult

    async def failed_clone(*_: Any) -> tuple[bool, str]:
        return False, "fatal: could not read Username for 'https://github.com'"

    monkeypatch.setattr(bootstrap, "clone_in_background", failed_clone)
    cfg = type(
        "Remote",
        (_Cfg,),
        {"git_dir": str(tmp_path / "none.git"), "git_remote": "https://github.com/o/r.git"},
    )()
    result = CodeIngestResult()
    assert await ingest_pass._run_repo(cfg, result) is None
    error = result.tier_outcomes[("o", "code-x", "fetch")]
    assert error.startswith("clone https://github.com/o/r.git: fatal: could not read Username")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["missing", "bad", "boom", "ok"])
async def test_apply_delta_records_the_apply_not_the_callers_input(
    outcome: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import argparse

    from server.cli import code_scip
    from server.code.app.index import entry_lock, scip_delta, scip_full

    path = tmp_path / "delta.json"
    if outcome != "missing":
        path.write_text("{" if outcome == "bad" else json.dumps({"commit": "a" * 40}))

    async def apply(*_: Any) -> Any:
        if outcome == "boom":
            raise RuntimeError("clickhouse down")
        return scip_delta.ScipApplyStats()

    monkeypatch.setattr(scip_delta, "apply_delta", apply)
    monkeypatch.setattr(scip_full, "mark_done", AsyncMock())
    monkeypatch.setattr(entry_lock, "check_held", lambda: None)
    monkeypatch.setattr(code_scip, "_note", lambda **_: None)
    failing = tier_health.failed(None, "clang_full", "harness exit 1: x", T0).to_json()
    repo = _StateRepo({"tier_health:clang_full": failing})
    args = argparse.Namespace(apply_delta=str(path))
    if outcome == "ok":
        assert await code_scip._scip_apply_recorded(args, repo, "o", "ns", "a" * 40) == 0
    else:
        with pytest.raises((OSError, ValueError, RuntimeError)):
            await code_scip._scip_apply_recorded(args, repo, "o", "ns", "a" * 40)
    record = tier_health.parse("clang_full", repo.state["tier_health:clang_full"])
    if outcome in ("missing", "bad"):
        assert repo.writes == []  # the caller's input: the tier did not run
    elif outcome == "boom":
        assert record.failed_passes == 2
    else:
        assert not record.failing


def test_a_credential_in_a_url_query_reaches_neither_the_error_nor_the_fix() -> None:
    text = (
        "fatal: unable to access 'https://dl.example.com/pkg.tgz?token=abcd1234&v=2': "
        "The requested URL returned error: 403"
    )
    record = tier_health.failed(None, "clang", text, T0)
    assert record.kind == "credential"
    assert "abcd1234" not in record.error and "token=***&v=2" in record.error
    assert "dl.example.com/pkg.tgz)" in record.fix and "abcd1234" not in record.fix


@pytest.mark.parametrize(
    "param", ["private_token", "api_key", "apikey", "client_secret", "password", "X-Goog-Signature"]
)
def test_a_query_credential_is_redacted_by_its_name_ending(param: str) -> None:
    assert tier_health.redact(f"GET https://h/x?{param}=s3cr3t&v=2") == (
        f"GET https://h/x?{param}=***&v=2"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["reconcile", "rechunk"])
async def test_a_walk_step_that_fails_in_its_own_handler_records_the_ingest_tier(
    step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.app.index import ingest_pass
    from server.code.app.index.pass_budget import CodeIngestResult
    from server.settings import settings

    monkeypatch.setattr(settings, "code_extractor_rechunk_enabled", True)
    monkeypatch.setattr(settings, "code_reconcile_enabled", True)
    repo = AsyncMock()
    repo.all_state.side_effect = RuntimeError("boom")  # the step's first store read
    result = CodeIngestResult()
    run = ingest_pass._maybe_reconcile if step == "reconcile" else ingest_pass._maybe_rechunk
    await run(repo, _Cfg(), "a" * 40, result)  # logged, never raised
    ingest_pass._ingest_served(_Cfg(), result)
    assert result.tier_outcomes[("o", "code-x", "ingest")] == f"{step}: RuntimeError"


def test_an_ssh_remote_credential_fix_names_the_host_and_repository() -> None:
    text = (
        "fetch git@github.com:org/repo.git: git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository."
    )
    record = tier_health.failed(None, "fetch", text, T0)
    assert record.kind == "credential"
    assert "(github.com/org/repo)" in record.fix and "ssh key" in record.fix


@pytest.mark.asyncio
async def test_entry_lock_records_the_go_modules_that_would_not_build() -> None:
    from server.cli import code_scip

    repo = _StateRepo()
    skipped = b"done svc\nskip m: go mod download failed: dial tcp: connection refused\n"
    with patch("server.code.store.index.CodeRepo", return_value=repo):
        await code_scip._record_tier("o", "ns", "go", 0, skipped)
        record = tier_health.parse("go", repo.state["tier_health:go"])
        assert (record.kind, record.error) == (
            "module",
            "m: go mod download failed: dial tcp: connection refused",
        )
        # a run that failed outright is a harness failure, whatever modules it skipped
        await code_scip._record_tier("o", "ns", "go", 1, skipped + b"panic: boom\n")
        assert tier_health.parse("go", repo.state["tier_health:go"]).kind == "harness"
        await code_scip._record_tier("o", "ns", "go", 0, b"done svc\n")
    assert not tier_health.parse("go", repo.state["tier_health:go"]).failing


def test_a_credential_fix_says_the_token_goes_only_under_the_remotes_owner() -> None:
    text = (
        "fatal: clone of 'https://github.com/other/dep.git' into submodule path "
        "'/c/contrib/dep' failed\nfatal: could not read Username for 'https://github.com'"
    )
    record = tier_health.failed(None, "clang", text, T0)
    assert record.kind == "credential"
    assert "(github.com/other/dep, submodule /c/contrib/dep)" in record.fix
    assert "only under the remote's owner" in record.fix


def test_a_relative_submodule_url_names_its_own_fix() -> None:
    text = (
        "fatal: transport 'file' not allowed\n"
        "fatal: clone of '/c/../dep' into submodule path '/c/contrib/dep' failed"
    )
    record = tier_health.failed(None, "clang", text, T0)
    assert record.kind == "submodule_url"
    assert "absolute https URL" in record.fix


def test_a_recorded_error_keeps_tool_output_and_cuts_any_other_text() -> None:
    """`code.index_status` shows the record to a viewer: a harness's or git's output
    about the entry is kept for its fix, a driver's text is cut to the kind."""
    from server.code.app.index import compiler_tier
    from server.code.app.index import tier_health as health_app
    from server.code.app.index.gitrepo import GitError

    driver = RuntimeError("Code: 210. Connection refused (ch-internal-7:9440) INSERT INTO x")
    assert health_app.error_text(driver) == "RuntimeError"
    harness = compiler_tier.harness_failure(2, SUBMODULE_NO_TOKEN)
    assert SUBMODULE_NO_TOKEN in health_app.error_text(harness)
    assert SUBMODULE_NO_TOKEN in health_app.error_text(GitError(SUBMODULE_NO_TOKEN))


@pytest.mark.asyncio
async def test_a_failed_git_step_keeps_its_output_for_the_record(tmp_path: Path) -> None:
    with pytest.raises(Exception) as ei:
        await compiler_tier.git(tmp_path, "rev-parse", "HEAD")
    assert "rev-parse" in recorder.error_text(ei.value)
