"""Each `loom code` maintenance command parses its flags and runs its own coroutine
under its own name: `_run(<name>, ...)` is the CLI dispatcher's query tag
(`cli.<name>`), so a command that dispatched under another's name would attribute its
ClickHouse statements to it."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from server.cli import app
from server.cli import code as code_cli

ID = ["--org", "o", "--namespace", "code-x"]
COMMANDS: list[list[str]] = [
    ["backfill-assignees", *ID],
    ["backfill-items", *ID],
    ["backfill-prs", *ID, "--months", "1"],
    ["backfill-reviews", *ID],
    ["export-matrix", *ID, "--prs", "50", "--json"],
    ["export-pairs", *ID, "--out", "pairs.jsonl"],
    ["lint-instructions", *ID, "--file", "CLAUDE.md"],
    ["repack", *ID, "--dry-run"],
    ["scip-retry", *ID, "batch-1"],
    ["vector-twin", *ID],
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for add in app._SUBCOMMAND_BUILDERS:
        add(sub)
    return parser


@pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: a[0])
def test_the_command_runs_its_own_coroutine_under_its_name(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[tuple[str, str]] = []

    def _run(name: str, coro: Any) -> int:
        ran.append((name, coro.__qualname__))
        coro.close()  # never awaited: the command's I/O is not this test's subject
        return 0

    monkeypatch.setattr(code_cli, "_run", _run)
    args = _parser().parse_args(["code", *argv])
    assert args.func(args) == 0
    ((name, coro_name),) = ran
    assert name == argv[0]
    assert coro_name.lstrip("_").replace("_", "-") == argv[0]


def test_scip_lock_path_prints_the_entry_lock_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.code.app.index.entry_lock import entry_lock_path
    from server.settings import settings

    monkeypatch.setattr(settings, "code_scip_lock_dir", str(tmp_path))
    args = _parser().parse_args(["code", "scip-lock-path", *ID])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == str(entry_lock_path("o", "code-x").resolve())


def test_entry_lock_runs_its_command_under_its_name(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def _run(name: str, coro: Any) -> int:
        ran.append(name)
        coro.close()
        return 0

    monkeypatch.setattr(code_cli, "_run", _run)
    args = _parser().parse_args(["code", "entry-lock", *ID, "--", "true"])
    assert args.func(args) == 0 and ran == ["entry-lock"]
