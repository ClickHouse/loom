"""The server's side of a document the converter skipped: every tier's harness run logs
the first warnings and the count and carries the stop switch, and the apply keeps each
tier's stale row: the files it skipped, until a later delta applies or deletes them."""

from __future__ import annotations

import json
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
async def test_one_apply_rewrites_each_tiers_stale_row_in_one_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row keeps what it held, gains the skipped files the entry keeps, and drops
    the files this delta applies or deletes. An out-of-scope or refused path is never
    marked: no delta would clear it."""
    monkeypatch.setattr(scip_delta, "_apply_files", AsyncMock())
    monkeypatch.setattr(scip_delta, "_indexable_prefixes", lambda *a: ("src/",))
    monkeypatch.setattr(
        scip_delta, "_path_refusal", lambda *a: lambda p: "denied" if p.endswith(".env.ts") else ""
    )
    repo = AsyncMock()
    repo.scip_stale_marks.return_value = {
        "scip_stale:clang": {"src/a.cpp", "src/gone.cpp", "src/other.cpp"},
        "scip_stale:python": {"src/kept.py"},
    }
    delta = ScipDelta(
        commit="c" * 40,
        files=[ScipFile(path="src/a.cpp")],
        deleted_files=["src/gone.cpp"],
        skipped_files=["src/bad.cpp", "src/web.ts", "contrib/x.cpp", "src/conf.env.ts"],
    )
    await scip_delta.apply_delta(repo, "o", "n", delta)
    repo.set_state.assert_not_awaited()
    repo.set_states.assert_awaited_once()
    org, ns, rows = repo.set_states.await_args.args
    assert (org, ns) == ("o", "n")
    assert {k: json.loads(v) for k, v in rows.items()} == {
        "scip_stale:clang": ["src/bad.cpp", "src/other.cpp"],
        "scip_stale:typescript": ["src/web.ts"],
    }


@pytest.mark.asyncio
async def test_an_apply_that_changes_no_stale_row_writes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scip_delta, "_apply_files", AsyncMock())
    repo = AsyncMock()
    repo.scip_stale_marks.return_value = {"scip_stale:clang": {"src/other.cpp"}}
    await scip_delta.apply_delta(
        repo, "o", "n", ScipDelta(commit="c" * 40, files=[ScipFile(path="src/a.cpp")])
    )
    repo.set_states.assert_not_awaited()
