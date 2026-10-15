"""`loom code entries`: scripts read one line per configured repository
(org namespace checkout lock-file) and split it on whitespace."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.cli import code_scip
from server.code.domain.index import repos
from server.settings import settings


def test_one_line_per_entry_with_the_resolved_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    entries = [
        SimpleNamespace(org="acme", namespace="code-a", scip_checkout_dir="var/checkout"),
        SimpleNamespace(org="acme", namespace="code-b", scip_checkout_dir="/srv/b"),
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    monkeypatch.setattr(settings, "code_scip_lock_dir", str(tmp_path))

    assert asyncio.run(code_scip._entries(argparse.Namespace())) == 0

    lines = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        ["acme", "code-a", "var/checkout", str(tmp_path / "scip.acme.code-a.lock")],
        ["acme", "code-b", "/srv/b", str(tmp_path / "scip.acme.code-b.lock")],
    ]
