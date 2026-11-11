"""Write commands that rewrite stored rows are dry runs unless given --apply."""

from __future__ import annotations

import argparse

import pytest

from server.cli import app
from server.cli._apply import APPLY_HINT, add_apply_flags, dry_run_hint

# (argv without the write switch) for every --apply-gated command
GATED: list[list[str]] = [
    ["reconcile-status", "--org", "o", "--namespace", "n"],
    ["period-digest", "--org", "o", "--namespace", "n"],
    ["rechunk", "--org", "o", "--namespace", "n"],
    ["tracker-embed-reset", "--org", "o", "--namespace", "n"],
    ["code", "compact"],
    ["code", "answers-purge", "--all"],
    ["integrity-repair"],
    ["sensitivity-sweep", "--org", "o"],
    ["demote-activity-narration", "--org", "o", "--namespace", "n"],
    ["namespaces-adopt", "--org", "o", "--owner", "u"],
]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loom")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for add in app._SUBCOMMAND_BUILDERS:
        add(sub)
    return parser


@pytest.mark.parametrize("argv", GATED, ids=lambda a: " ".join(a[:2]))
def test_default_is_a_dry_run_and_apply_writes(argv: list[str]) -> None:
    p = _parser()
    assert p.parse_args(argv).dry_run is True
    assert p.parse_args([*argv, "--dry-run"]).dry_run is True
    assert p.parse_args([*argv, "--apply"]).dry_run is False
    with pytest.raises(SystemExit):
        p.parse_args([*argv, "--apply", "--dry-run"])


def test_a_successful_dry_run_ends_with_the_apply_hint() -> None:
    p = argparse.ArgumentParser()
    add_apply_flags(p, writes="write")
    assert dry_run_hint(p.parse_args([]), 0) == APPLY_HINT
    assert dry_run_hint(p.parse_args([]), 1) is None
    assert dry_run_hint(p.parse_args(["--apply"]), 0) is None
    # a command without the switch never gets the hint
    assert dry_run_hint(argparse.Namespace(dry_run=True), 0) is None


def test_dispatch_prints_the_hint_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    p = argparse.ArgumentParser()
    add_apply_flags(p, writes="write")
    args = p.parse_args([])
    args.cmd = "demo-cmd"
    args.func = lambda a: print('{"ok": true}') or 0
    assert app._dispatch(args) == 0
    out = capsys.readouterr()
    assert out.out == '{"ok": true}\n' and APPLY_HINT in out.err


def test_answers_purge_dry_run_samples_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    from server.cli import code_maintenance

    rows = [{"llm_model": "m", "question": f"q{i}"} for i in range(12)]
    written: list[object] = []

    class _CH:
        connected = False

        async def connect(self) -> None: ...

        async def disconnect(self) -> None: ...

        async def query_all(self, sql: str, params: object = None) -> list[dict[str, str]]:
            return rows

    class _Repo:
        _ch = _CH()

        async def insert_rows(self, table: str, out: list[object]) -> None:
            written.extend(out)

        @staticmethod
        def tombstone_of(table: str, r: dict[str, str]) -> dict[str, str]:
            return r

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr("server.code.store.index.CodeRepo", _Repo)
    args = _parser().parse_args(
        ["code", "answers-purge", "--all", "--org", "o", "--namespace", "n"]
    )
    assert asyncio.run(code_maintenance._answers_purge(args)) == 0
    out = capsys.readouterr().out
    assert "would tombstone 12" in out and "q9" in out and "q10" not in out and "2 more" in out
    assert written == []
    args = _parser().parse_args(
        ["code", "answers-purge", "--all", "--org", "o", "--namespace", "n", "--apply"]
    )
    assert asyncio.run(code_maintenance._answers_purge(args)) == 0
    assert len(written) == 12
