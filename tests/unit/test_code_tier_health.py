"""A tier that fails on an entry is recorded per entry with its kind and the one fix,
counts its failed passes in a row, and clears on the next pass that succeeds; the
record reaches `as_of`, code.index_status and code.repos."""

from __future__ import annotations

import importlib
import json
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.app.index import compiler_tier
from server.code.app.index import tier_health as recorder
from server.code.domain.index import tier_health
from server.workers import pass_context

# The error the C++ tier gave on every pass for an entry with a private submodule.
SUBMODULE_NO_TOKEN = (
    "RuntimeError: git submodule failed: fatal: could not read Username for "
    "'https://github.com': terminal prompts disabled\n"
    "fatal: Fetched in submodule path 'contrib/amber', but it did not contain "
    "2d70290e18f53e91cd093db820370d4ba571b366. Direct fetching of that commit failed.\n"
)
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


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
        ("fatal: unable to access 'https://h/': Could not resolve host: h", "network"),
        ("RuntimeError: harness timed out after 600.0s", "timeout"),
        ("TimeoutError: git fetch origin in /c exceeded 600s", "timeout"),
        ("write error: No space left on device", "disk"),
        ("RuntimeError: harness exit 2: cmake: error in CMakeLists.txt", "harness"),
        ("ValueError: something else", "unknown"),
    ],
)
def test_an_error_is_classified_into_its_kind(text: str, kind: str) -> None:
    assert tier_health.classify(text) == kind
    assert kind in tier_health.KINDS


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
    await recorder.record(repo, "o", "ns", "clang", RuntimeError(SUBMODULE_NO_TOKEN))
    await recorder.record(repo, "o", "ns", "clang", RuntimeError(SUBMODULE_NO_TOKEN))
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


async def _run_tier(module: str, repo: _StateRepo, run_repo: Any) -> Any:
    w = importlib.import_module(module)

    @asynccontextmanager
    async def _lock(*_: Any, **__: Any) -> Any:
        yield

    pass_context.begin("w")
    try:
        with (
            patch("server.code.store.index.CodeRepo", return_value=repo),
            patch("server.code.domain.index.repos.configured_repos", return_value=[_Cfg()]),
            patch.object(compiler_tier, "worker_owns", AsyncMock(return_value=True)),
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
        raise RuntimeError(SUBMODULE_NO_TOKEN)

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
        assert health.error.startswith("exit 1:")
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
    assert rows[0]["since"] == T0.isoformat(timespec="seconds")
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


def test_a_fix_names_the_repository_url_over_a_bare_host_and_a_short_submodule_path() -> None:
    text = (
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"
        "fatal: clone of 'https://github.com/o/private.git' into submodule path "
        "'/srv/loom/checkout/contrib/private' failed"
    )
    fix = tier_health.fix_for("credential", text)
    assert "(github.com/o/private, submodule contrib/private)" in fix
