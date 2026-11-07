"""code_scip_delta worker: watermark guards must hold before any subprocess.

The heavy path (git + scip-clang + apply) only runs when the fast tier's
git_head has outrun scip_commit AND the host has the checkout + binary.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.code.workers.scip_delta import run_pass
from tests.unit.code_fixtures import repo_config


@pytest.fixture(autouse=True)
def _lock_dir(monkeypatch, tmp_path):
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_lock_dir", str(tmp_path / "locks"))
    # The deployment-wide entry lease is ClickHouse's; tests hold a fake one.
    from tests.unit.claims_fake import held_leases

    held_leases(monkeypatch)


def _Cfg(**overrides: Any) -> Any:
    return repo_config(
        org="clickhouse", namespace="code-clickhouse", scip_producer="clang", **overrides
    )


def _repo(git_head: str, scip_commit: str) -> AsyncMock:
    repo = AsyncMock()

    async def get_state(org, ns, key):
        return {"git_head": git_head, "scip_commit": scip_commit}.get(key, "")

    repo.get_state.side_effect = get_state
    return repo


@pytest.mark.asyncio
async def test_current_watermarks_noop():
    repo = _repo("a" * 40, "a" * 40)
    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_Cfg()],
        ),
    ):
        res = await run_pass()
    assert res.repos_checked == 1
    assert res.deltas_applied == 0 and not res.errors and not res.skipped


@pytest.mark.asyncio
async def test_unseeded_watermarks_skip():
    repo = _repo("a" * 40, "")
    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_Cfg()],
        ),
    ):
        res = await run_pass()
    assert res.skipped and "watermarks not seeded" in res.skipped[0]


@pytest.mark.asyncio
async def test_fetch_does_not_recurse_submodules(monkeypatch, tmp_path):
    """git's default on-demand submodule recursion fetches every pin that
    changed anywhere in the fetched range — one pin the upstream submodule has
    since rewritten away fatals the whole fetch (contrib/sql-console stalled
    the compiler tier for 5 days this way). Only `submodule update` needs
    pins, and only the ones at git_head."""
    from server.code.workers import scip_delta as w
    from server.settings import settings

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    bare = tmp_path / "bare.git"
    bare.mkdir()
    clang = tmp_path / "scip-clang"
    clang.touch()
    monkeypatch.setattr(settings, "code_scip_checkout_dir", str(checkout))
    monkeypatch.setattr(settings, "code_git_dir", str(bare))
    monkeypatch.setattr(settings, "code_scip_clang_path", str(clang))

    git_calls: list[tuple[str, ...]] = []
    git_kwargs: list[dict[str, Any]] = []

    async def fake_git(cwd, *args, **kw):
        git_calls.append(args)
        git_kwargs.append(kw)

    monkeypatch.setattr(w, "git", fake_git)

    from server.code.app.index.sandbox import JobResult

    harness = AsyncMock(return_value=JobResult(0, b"", b""))

    repo = _repo("a" * 40, "b" * 40)
    cfg = _Cfg(github_token="ghs_test", git_remote="https://github.com/o/private")
    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[cfg],
        ),
        patch("server.code.app.index.compiler_tier.run_job", harness),
    ):
        res = await run_pass()

    assert not res.errors
    fetches = [c for c in git_calls if c[0] == "fetch"]
    assert fetches, "the delta path never fetched"
    assert "--recurse-submodules=no" in fetches[0]
    assert "token" not in git_kwargs[git_calls.index(fetches[0])]
    # the submodule pins come with the entry's token, so a private submodule is fetched
    sub = git_kwargs[next(i for i, c in enumerate(git_calls) if c[0] == "submodule")]
    assert sub["token"] == "ghs_test" and sub["remote"] == "https://github.com/o/private"


_STALE_OUT = (
    b">> stale compile database: cmake reconfigure failed\n"
    b">> cmake: CMake Error: a package is missing\n"
)


async def _stale_pass(
    monkeypatch, tmp_path, *, out: bytes, scip_commit: str = "b" * 40, marker: bool = False
) -> tuple[Any, AsyncMock]:
    """One delta pass whose harness exits 0 with `out`."""
    from server.code.app.index.compiler_tier import STALE_COMPDB_MARKER
    from server.code.app.index.sandbox import JobResult
    from server.code.workers import scip_delta as w
    from server.settings import settings

    for name in ("checkout/build", "bare.git"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "scip-clang").touch()
    if marker:
        (tmp_path / "checkout" / STALE_COMPDB_MARKER).touch()
    else:
        (tmp_path / "checkout" / STALE_COMPDB_MARKER).unlink(missing_ok=True)
    monkeypatch.setattr(settings, "code_scip_checkout_dir", str(tmp_path / "checkout"))
    monkeypatch.setattr(settings, "code_git_dir", str(tmp_path / "bare.git"))
    monkeypatch.setattr(settings, "code_scip_clang_path", str(tmp_path / "scip-clang"))
    monkeypatch.setattr(w, "git", AsyncMock())
    record = AsyncMock()
    with (
        patch("server.code.store.index.CodeRepo", return_value=_repo("a" * 40, scip_commit)),
        patch("server.code.domain.index.repos.configured_repos", return_value=[_Cfg()]),
        patch(
            "server.code.app.index.compiler_tier.run_job",
            AsyncMock(return_value=JobResult(0, out, b"")),
        ),
        patch("server.code.app.index.tier_health.record", record),
    ):
        res = await run_pass()
    return res, record


@pytest.mark.asyncio
async def test_a_delta_on_a_stale_compile_database_fails_the_tier_record(monkeypatch, tmp_path):
    """A harness that exits 0 closing with the stale compile database lines: the
    watermark moves, the pass is no error, and the clang record reads failing with
    CMake's lines; a later run without them records ok."""
    from server.code.app.index.tier_health import error_text
    from server.code.domain.index.tier_health import classify

    res, record = await _stale_pass(monkeypatch, tmp_path, out=b">> 3 TUs to index\n" + _STALE_OUT)
    assert not res.errors
    (_, _, ns, tier, err) = record.await_args.args
    assert (ns, tier) == ("code-clickhouse", "clang")
    assert "a package is missing" in str(err) and classify(error_text(err)) == "cmake"
    res, record = await _stale_pass(monkeypatch, tmp_path, out=b">> 3 TUs to index\n")
    assert not res.errors and record.await_args.args[3:] == ("clang",)


@pytest.mark.asyncio
async def test_an_applied_run_removes_the_marker_unless_it_was_stale(monkeypatch, tmp_path):
    """The marker holds what a reconfigure owes until its run is applied."""
    from server.code.app.index.compiler_tier import STALE_COMPDB_MARKER

    marker = tmp_path / "checkout" / STALE_COMPDB_MARKER
    await _stale_pass(monkeypatch, tmp_path, out=_STALE_OUT, marker=True)
    assert marker.exists()
    await _stale_pass(monkeypatch, tmp_path, out=b">> 3 TUs to index\n", marker=True)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_a_current_pass_leaves_a_stale_record_while_the_marker_is_there(
    monkeypatch, tmp_path
):
    """No new merge: the pass runs no harness, so it does not clear a record failing on
    a stale compile database while the checkout still marks the failed configure."""
    _, record = await _stale_pass(monkeypatch, tmp_path, out=b"", scip_commit="a" * 40, marker=True)
    record.assert_not_awaited()
    _, record = await _stale_pass(monkeypatch, tmp_path, out=b"", scip_commit="a" * 40)
    assert record.await_args.args[3:] == ("clang",)


@pytest.mark.asyncio
async def test_missing_checkout_skips(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_checkout_dir", "/nonexistent/checkout")
    repo = _repo("a" * 40, "b" * 40)
    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_Cfg()],
        ),
    ):
        res = await run_pass()
    assert res.skipped and "missing" in res.skipped[0]
    assert res.deltas_applied == 0


@pytest.mark.asyncio
async def test_pass_is_recorded_in_the_worker_status_registry() -> None:
    """The pass calls `record_pass`, so /v1/admin/workers.status shows its
    last_run_at/outcome and a stalled compiler tier is visible there, not
    only in a log."""
    from server.workers.status import get_registry

    get_registry().reset()
    repo = _repo("a" * 40, "")
    with (
        patch("server.code.store.index.CodeRepo", return_value=repo),
        patch(
            "server.code.domain.index.repos.configured_repos",
            return_value=[_Cfg()],
        ),
    ):
        await run_pass()
    snap = get_registry().snapshot()["code_scip_delta"]
    assert snap["last_run_at"] and snap["last_error"] is None
    assert snap["last_outcome"]["repos_checked"] == 1
    assert snap["last_outcome"]["deltas_applied"] == 0
    assert snap["last_outcome"]["errors"] == 0
    assert "watermarks not seeded" in snap["last_outcome"]["reason"]


@pytest.mark.asyncio
async def test_git_timeout_names_the_command_and_retries(monkeypatch, tmp_path):
    from server.code.app.index import compiler_tier as w
    from server.code.app.index.sandbox import JobResult

    calls = {"n": 0}

    async def _job(argv, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError  # first attempt hangs past its timeout
        return JobResult(0, b"", b"")

    monkeypatch.setattr(w, "run_job", _job)
    await w.git(tmp_path, "fetch", "origin", timeout_s=0.05, retries=1)
    assert calls["n"] == 2
    calls["n"] = 0
    with pytest.raises(TimeoutError, match=r"git fetch origin in .* exceeded 0s"):
        await w.git(tmp_path, "fetch", "origin", timeout_s=0.05, retries=0)
