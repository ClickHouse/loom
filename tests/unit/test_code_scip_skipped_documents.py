"""The server's side of a document the converter skipped: every tier's harness run logs
the first warnings and the count and carries the stop switch, and the apply marks the
files it skipped stale under its tier, until a later delta of that tier applies or
deletes them."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.code.app.index import compiler_tier, sandbox, scip_delta
from server.code.app.index.scip_delta import ScipDelta, ScipFile


def _warning(path: str) -> str:
    return f"scip_to_delta: warning: skipped document {path!r}: DecodeError()"


def _job(envs: list[dict[str, str]], out: str):
    async def run_job(argv: list[str], **kw: object) -> sandbox.JobResult:
        envs.append(kw["env"])  # type: ignore[arg-type]
        return sandbox.JobResult(0, out.encode(), b"")

    return run_job


async def _harness(tmp_path: Path) -> tuple[int, str]:
    return await compiler_tier.run_harness(
        Path("run_python_delta.sh"), tmp_path, [], {"A": "1"}, out_dir=tmp_path, timeout_s=5
    )


@pytest.mark.asyncio
async def test_a_harness_run_logs_the_first_skipped_documents_and_the_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every compiler tier (C++, its full run, Python, TypeScript, Go) runs its harness
    through `run_harness`, so this is where the converter's warnings reach the log."""
    lines = [_warning(f"pkg/bad{i}.py") for i in range(7)]
    out = "\n".join([lines[0], "other line", *lines, "delta: 3 files, ..."]) + "\n"
    monkeypatch.setattr(compiler_tier, "run_job", _job([], out))
    with caplog.at_level(logging.WARNING, logger=compiler_tier.logger.name):
        assert await _harness(tmp_path) == (0, out)
    assert [r.getMessage() for r in caplog.records] == [
        *(f"run_python_delta.sh: {line}" for line in lines[:5]),
        "run_python_delta.sh: 7 documents skipped in all",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", [False, True])
async def test_a_harness_run_carries_the_stop_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: bool
) -> None:
    envs: list[dict[str, str]] = []
    monkeypatch.setattr(compiler_tier, "run_job", _job(envs, ""))
    monkeypatch.setattr(compiler_tier.settings, "code_scip_stop_on_failed_document", stop)
    await _harness(tmp_path)
    assert envs == [{"A": "1", "SCIP_STOP_ON_FAILED_DOCUMENT": "1" if stop else "0"}]


@pytest.mark.asyncio
async def test_one_apply_marks_and_clears_its_tiers_files_in_one_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each skipped file the entry keeps is marked under the applying tier, whatever its
    suffix, and the mark of each file this delta applies or deletes is emptied. An
    out-of-scope or refused path is never marked: no delta would clear it."""
    monkeypatch.setattr(scip_delta, "_apply_files", AsyncMock())
    monkeypatch.setattr(scip_delta, "_indexable_prefixes", lambda *a: ("src/",))
    monkeypatch.setattr(
        scip_delta, "_path_refusal", lambda *a: lambda p: "denied" if p.endswith(".env.h") else ""
    )
    repo = AsyncMock()
    repo.scip_stale_marks.return_value = {"src/a.cpp", "src/gone.cpp", "src/other.cpp"}
    delta = ScipDelta(
        commit="c" * 40,
        files=[ScipFile(path="src/a.cpp")],
        deleted_files=["src/gone.cpp"],
        skipped_files=["src/bad.cpp", "src/k.cu", "contrib/x.cpp", "src/conf.env.h"],
    )
    await scip_delta.apply_delta(repo, "o", "n", delta, tier="clang")
    repo.scip_stale_marks.assert_awaited_once_with("o", "n", "clang")
    repo.set_state.assert_not_awaited()
    repo.set_states.assert_awaited_once_with(
        "o",
        "n",
        {
            "scip_stale:clang:src/bad.cpp": "c" * 40,
            "scip_stale:clang:src/k.cu": "c" * 40,
            "scip_stale:clang:src/a.cpp": "",
            "scip_stale:clang:src/gone.cpp": "",
        },
    )


@pytest.mark.asyncio
async def test_an_apply_that_marks_and_clears_nothing_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scip_delta, "_apply_files", AsyncMock())
    repo = AsyncMock()
    repo.scip_stale_marks.return_value = {"src/other.cpp"}
    delta = ScipDelta(commit="c" * 40, files=[ScipFile(path="src/a.cpp")])
    await scip_delta.apply_delta(repo, "o", "n", delta, tier="clang")
    repo.set_states.assert_not_awaited()


class _State:
    """`code_index_state` as last writer wins, both applies' reads landing before
    either one's write."""

    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = rows
        self.read = asyncio.Barrier(2)

    async def scip_stale_marks(self, org: str, ns: str, tier: str) -> set[str]:
        prefix = f"scip_stale:{tier}:"
        marked = {k[len(prefix) :] for k, v in self.rows.items() if k.startswith(prefix) and v}
        await self.read.wait()
        return marked

    async def set_states(self, org: str, ns: str, rows: dict[str, str]) -> None:
        self.rows.update(rows)


@pytest.mark.asyncio
async def test_a_cpp_delta_beside_a_python_one_keeps_the_python_marks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A C++ window lists every file it deleted, a `.py` one too; the Python tier's marks
    are not its own, so it neither reads nor writes them."""
    monkeypatch.setattr(scip_delta, "_apply_files", AsyncMock())
    state = _State({"scip_stale:python:pkg/a.py": "a" * 40})
    cpp = ScipDelta(commit="b" * 40, files=[ScipFile(path="src/x.cpp")], deleted_files=["pkg/a.py"])
    py = ScipDelta(commit="c" * 40, files=[ScipFile(path="pkg/c.py")], skipped_files=["pkg/b.py"])
    await asyncio.gather(
        scip_delta.apply_delta(state, "o", "n", cpp, tier="clang"),  # type: ignore[arg-type]
        scip_delta.apply_delta(state, "o", "n", py, tier="python"),  # type: ignore[arg-type]
    )
    assert state.rows == {
        "scip_stale:python:pkg/a.py": "a" * 40,
        "scip_stale:python:pkg/b.py": "c" * 40,
    }


def test_an_upload_applies_for_the_tier_whose_watermark_it_moves() -> None:
    from server.code.workers.scip_apply import tier_for

    assert [tier_for(p) for p in ("scip_harness@ci", "scip-python", "scip-typescript", "go")] == [
        "clang",
        "python",
        "typescript",
        "go",
    ]
