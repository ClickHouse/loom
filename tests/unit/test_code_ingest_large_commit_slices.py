"""A commit too big for one pass is ingested in round slices.

The pass always starts its first pending commit, so a commit of hundreds of
files at 6–17 s a file could outlast the pass deadline with no cursor, and
every later pass would restart the same commit while every other entry
starved.

The files of a commit land a round (25) at a time, each round flushed; a
round starts only if it can finish before the deadline; the slice cursor says
how far the commit got, and the watermark moves only with its last file.
"""

from __future__ import annotations

import asyncio
import math
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import gitrepo
from server.code.app.index import ingest_pass as ingest
from server.code.app.index import pass_budget as ingest_budget
from server.code.app.index import pass_commit as ingest_commit
from server.code.app.index import pass_fetch as ingest_fetch
from server.code.app.index import pass_stream as ingest_stream
from server.settings import settings
from tests.unit.test_code_ingest_pass_budget import _cfg, _Clock, _StateRepo

PER_FILE_S = 17.0  # a slow per-file ingest


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    files: dict[str, int],
    done: list[tuple[str, str]],
    kill_at: float | None = None,
) -> None:
    """Real `_ingest_commit` / `_ingest_commit_files`; the git reads, the
    per-round models and the per-file ingest are fakes that advance `clock`."""
    import server.code.app.index.denylist as dl
    import server.code.app.index.round_reads as rr
    import server.embedding.embedder as emb
    import server.workers.cost_event as cs

    async def diff_files(git_dir: str, base: str, sha: str) -> list[Any]:
        return [
            SimpleNamespace(path=f"src/{sha}/f{i:03d}.py", status="M") for i in range(files[sha])
        ]

    async def names(git_dir: str, base: str, sha: str, limit: int = 2000) -> list[str]:
        return ["x"] * files[sha]

    async def meta(git_dir: str, shas: list[str]) -> dict[str, tuple[str, int]]:
        return {s: ("", int(clock.time()) - 3600 + i) for i, s in enumerate(shas)}

    async def show(git_dir: str, sha: str, path: str, max_bytes: int = 0) -> bytes:
        return b"x"

    async def subject(git_dir: str, sha: str) -> str:
        return ""

    class _Reads:
        hits = misses = 0

        def __init__(self, *a: Any, **k: Any) -> None:
            self.parsed: dict[str, Any] = {}

        async def prefetch(self, *a: Any, **k: Any) -> None:
            return None

        async def flush(self) -> None:
            return None

    async def round_files(
        reads: Any,
        org: str,
        ns: str,
        sha: str,
        round_entries: list[Any],
        sources: dict[str, bytes],
        **kw: Any,
    ) -> None:
        for e in round_entries:
            if kill_at is not None and clock.now + PER_FILE_S > kill_at:
                raise asyncio.CancelledError  # the supervisor's deadline kill
            clock.now += PER_FILE_S
            done.append((sha, e.path))
            kw["changed_paths"].append(e.path)

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    async def load_denylist(cfg: Any, head: str, show_: Any) -> None:
        return None

    monkeypatch.setattr(gitrepo, "diff_files", diff_files)
    monkeypatch.setattr(gitrepo, "diff_name_only", names)
    monkeypatch.setattr(gitrepo, "commit_meta_many", meta)
    monkeypatch.setattr(gitrepo, "show_file", show)
    monkeypatch.setattr(gitrepo, "commit_subject", subject)
    monkeypatch.setattr(rr, "RoundReads", _Reads)
    monkeypatch.setattr(ingest_commit, "_ingest_round_files", round_files)
    monkeypatch.setattr(ingest_commit, "_prefetch_round_models", nothing)
    monkeypatch.setattr(ingest_commit, "_resolve_dependents", nothing)
    monkeypatch.setattr(ingest_commit, "_refresh_unit_risk", nothing)
    monkeypatch.setattr(ingest_commit, "time", clock)
    monkeypatch.setattr(ingest, "time", clock)
    monkeypatch.setattr(ingest_budget, "time", clock)
    monkeypatch.setattr(ingest_stream, "time", clock)
    monkeypatch.setattr(ingest_fetch, "time", clock)
    monkeypatch.setattr(emb, "get_embedder", lambda: SimpleNamespace(model_name="m"))
    monkeypatch.setattr(dl, "load_denylist", load_denylist)
    monkeypatch.setattr(cs, "emit_worker_cost_event_for", lambda **kw: None)
    monkeypatch.setattr(settings, "code_context_description_enabled", False)
    monkeypatch.setattr(settings, "code_ingest_round_files", 25)
    monkeypatch.setattr(settings, "code_ingest_max_files_per_commit", 500)


def _plan(ns: str, repo: _StateRepo, pending: list[str]) -> ingest_budget.RepoPass:
    watermark = repo.state.get("git_head") or "w0"
    left = pending[pending.index(watermark) + 1 :] if watermark in pending else list(pending)
    plan = ingest_budget.RepoPass(
        _cfg(ns, path_prefixes=("src/",)), repo, pending[-1], watermark, False, walk=True
    )
    plan.pending = left
    return plan


def _pass(
    clock: _Clock, plans: list[ingest_budget.RepoPass]
) -> tuple[float, ingest_budget.CodeIngestResult]:
    started = clock.now
    result = ingest_budget.CodeIngestResult()
    budget = ingest_budget.PassBudget(started=started, stop_after=started + 675.0)
    asyncio.run(ingest_stream.ingest_oldest_first(plans, result, budget))
    return clock.now - started, result


def test_a_315_file_commit_lands_across_passes_and_no_pass_overruns(monkeypatch) -> None:
    clock, done = _Clock(), []
    _wire(monkeypatch, clock, {"big": 315}, done)
    repo = _StateRepo()
    passes, longest = 0, 0.0
    while repo.state.get("git_head") != "big":
        passes += 1
        assert passes <= 20, "livelock: the commit never lands"
        took, _ = _pass(clock, [_plan("code-loom", repo, ["big"])])
        longest = max(longest, took)
    assert longest <= 900 * 0.95, f"a pass ran {longest:.0f} s, past its deadline"
    # two 25-file rounds fit a pass at 17 s/file (425 s each, the second ends
    # at 850 s < 855 s): 50 files a pass
    assert passes == math.ceil(315 / 50)
    assert len(done) == 315 and len(set(done)) == 315, "every file once, none repeated"
    assert repo.state["commit_slice"] == "", "the cursor clears when the commit lands"


def test_a_killed_pass_loses_only_its_round_in_flight(monkeypatch) -> None:
    clock, done = _Clock(), []
    _wire(monkeypatch, clock, {"big": 315}, done, kill_at=clock.now + 600.0)
    repo = _StateRepo()
    with pytest.raises(asyncio.CancelledError):
        _pass(clock, [_plan("code-loom", repo, ["big"])])
    # round one (25 files) landed and is recorded; round two died mid-way
    assert repo.state["commit_slice"] == "w0..big:25"
    assert "git_head" not in repo.state, "the watermark waits for the last file"
    _wire(monkeypatch, clock, {"big": 315}, done)
    _pass(clock, [_plan("code-loom", repo, ["big"])])
    resumed = [p for s, p in done[25:]]
    assert resumed[0].endswith("f025.py"), "the next pass resumes at file 25"


def test_a_big_commit_shares_the_phase_with_waiting_entries(monkeypatch) -> None:
    clock, done = _Clock(), []
    _wire(monkeypatch, clock, {"big": 315, "s1": 3, "s2": 4}, done)
    big, small = _StateRepo(), _StateRepo()
    _pass(clock, [_plan("code-loom", big, ["big"]), _plan("code-weave", small, ["s1", "s2"])])
    assert small.state.get("git_head") == "s2", "the waiting entry lands in the same pass"
    assert big.state["commit_slice"].startswith("w0..big:")
    assert int(big.state["commit_slice"].rsplit(":", 1)[1]) >= 25, "and the big commit progressed"


def test_a_cursor_for_another_base_is_ignored(monkeypatch) -> None:
    repo = _StateRepo()
    repo.state["commit_slice"] = "old..big:100"
    assert asyncio.run(ingest_stream._slice_done(repo, "o", "n", "w0..big")) == 0
    repo.state["commit_slice"] = "w0..big:100"
    assert asyncio.run(ingest_stream._slice_done(repo, "o", "n", "w0..big")) == 100
