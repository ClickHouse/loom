"""The server's side of a document the converter skipped: every tier's harness run logs
its warning once and carries the stop switch, and the apply marks the file stale until a
later delta applies or deletes it."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.code.app.index import compiler_tier, sandbox
from server.code.app.index.scip_delta import ScipDelta, ScipFile

_WARNING = "scip_to_delta: warning: skipped document 'pkg/bad.py': DecodeError()"


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
async def test_a_harness_run_logs_each_skipped_document_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Every compiler tier (C++, its full run, Python, TypeScript, Go) runs its harness
    through `run_harness`, so this is where the converter's warnings reach the log."""
    out = f"{_WARNING}\nother line\n{_WARNING}\ndelta: 3 files, ...\n"
    monkeypatch.setattr(compiler_tier, "run_job", _job([], out))
    with caplog.at_level(logging.WARNING, logger=compiler_tier.logger.name):
        assert await _harness(tmp_path) == (0, out)
    assert [r.getMessage() for r in caplog.records] == [f"run_python_delta.sh: {_WARNING}"]


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
async def test_a_delta_marks_a_skipped_file_and_clears_the_mark_of_one_it_applies_or_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import scip_delta

    monkeypatch.setattr(scip_delta, "_apply_files", AsyncMock())
    repo = AsyncMock()
    repo.scip_stale_marks.return_value = {"src/a.cpp", "src/gone.cpp", "src/other.cpp"}
    delta = ScipDelta(
        commit="c" * 40,
        files=[ScipFile(path="src/a.cpp")],
        deleted_files=["src/gone.cpp"],
        skipped_files=["src/bad.cpp"],
    )
    stats = await scip_delta.apply_delta(repo, "o", "n", delta)
    assert [c.args for c in repo.set_state.await_args_list] == [
        ("o", "n", "scip_stale:src/bad.cpp", "c" * 40),
        ("o", "n", "scip_stale:src/a.cpp", ""),
        ("o", "n", "scip_stale:src/gone.cpp", ""),
    ]
    assert stats.files_skipped_failed == 1
