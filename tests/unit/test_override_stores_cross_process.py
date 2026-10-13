"""The host-shared override stores (budgets, rate limits): concurrent writers in
separate processes never drop each other's update, and an update that cannot be
published is refused and rolled back."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from server.core import ratelimit as ratelimit_mod
from server.core import ratelimit_shared
from server.core.errors import LoomServerError
from server.core.ratelimit import Limit, Limiter
from server.observability import budgets as budgets_mod
from server.observability.budgets import BudgetOverrides

ROOT = Path(__file__).resolve().parents[2]
WRITERS, UPDATES = 4, 10

_BUDGET_WRITER = """
import sys
from server.observability import budgets as budgets_mod
from server.observability.budgets import BudgetOverrides
o = BudgetOverrides()
o.path = sys.argv[1]
for i in range(int(sys.argv[3])):
    o.set(f"op.{sys.argv[2]}.{i}", i)
"""

_LIMIT_WRITER = """
import sys
from server.core.ratelimit import Limit, Limiter
lim = Limiter()
for i in range(int(sys.argv[3])):
    lim.set_namespace_limit(f"ns{sys.argv[2]}_{i}", "memory", Limit(capacity=i + 1, rate=1.0))
"""


def _race(code: str, path: str, env: dict[str, str]) -> None:
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(code), path, str(w), str(UPDATES)],
            cwd=ROOT,
            env={**os.environ, **env},
        )
        for w in range(WRITERS)
    ]
    assert [p.wait(timeout=120) for p in procs] == [0] * WRITERS


def test_concurrent_budget_writers_all_survive(tmp_path: Path) -> None:
    path = str(tmp_path / "budgets.json")
    _race(_BUDGET_WRITER, path, {})
    ops = json.loads(Path(path).read_text())["ops"]
    assert len(ops) == WRITERS * UPDATES


def test_concurrent_rate_limit_writers_all_survive(tmp_path: Path) -> None:
    table = str(tmp_path / "rl.buckets")
    _race(
        _LIMIT_WRITER, table, {"LOOM_RATELIMIT_SHARED": "on", "LOOM_RATELIMIT_SHARED_PATH": table}
    )
    ns = json.loads(Path(table + ".overrides.json").read_text())["ns"]
    assert len(ns) == WRITERS * UPDATES


def _no_space(*_a: object, **_k: object) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


def test_an_unpublished_budget_update_is_refused_and_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = BudgetOverrides(), BudgetOverrides()
    a.path = b.path = str(tmp_path / "budgets.json")
    a.set("memory.set", 30)
    monkeypatch.setattr(ratelimit_shared, "publish_json", _no_space)
    with pytest.raises(LoomServerError, match="not applied"):
        a.set("memory.set", 0)
    with pytest.raises(LoomServerError):
        a.clear("memory.set")
    assert a.effective("memory.set", 99) == 30
    assert b.effective("memory.set", 99) == 30


def test_an_unpublished_rate_limit_update_is_refused_and_rolled_back(
    settings_override, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_override(ratelimit_shared="on", ratelimit_shared_path=str(tmp_path / "rl.buckets"))
    lim = Limiter()
    monkeypatch.setattr(ratelimit_shared, "publish_json", _no_space)
    with pytest.raises(LoomServerError, match="not applied"):
        lim.set_namespace_limit("ns1", "memory", Limit(capacity=1, rate=0.0))
    assert ("ns1", "memory") not in lim._ns_overrides
    lim.reset_all()


def test_a_failed_replace_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit_shared.os, "replace", _no_space)
    with pytest.raises(OSError):
        ratelimit_shared.publish_json(str(tmp_path / "x.json"), {"a": 1})
    assert list(tmp_path.iterdir()) == []


def _unreadable(*_a: object, **_k: object) -> None:
    raise ValueError("injected read failure")


def test_a_budget_update_over_an_unreadable_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = BudgetOverrides(), BudgetOverrides()
    a.path = b.path = str(tmp_path / "budgets.json")
    b.set("memory.search", 30)
    a.set("memory.set", 0)
    before = Path(a.path).read_bytes()
    monkeypatch.setattr(budgets_mod.json, "load", _unreadable)
    with pytest.raises(LoomServerError, match="unreadable"):
        b.set("memory.get", 5)
    monkeypatch.undo()
    assert Path(a.path).read_bytes() == before
    assert b.effective("memory.set", 99) == 0


def test_a_rate_limit_update_over_an_unreadable_file_is_refused(
    settings_override, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = str(tmp_path / "rl.buckets")
    settings_override(ratelimit_shared="on", ratelimit_shared_path=table)
    a, b = Limiter(), Limiter()
    b.set_namespace_limit("ns_b", "memory", Limit(capacity=30, rate=1.0))
    a.set_namespace_limit("ns_a", "memory", Limit(capacity=1, rate=0.0))
    before = Path(table + ".overrides.json").read_bytes()
    monkeypatch.setattr(ratelimit_mod.json, "load", _unreadable)
    with pytest.raises(LoomServerError, match="unreadable"):
        b.set_namespace_limit("ns_b", "memory", Limit(capacity=2, rate=1.0))
    monkeypatch.undo()
    assert Path(table + ".overrides.json").read_bytes() == before
    a.reset_all()
    b.reset_all()


def test_a_committed_budget_update_is_never_reported_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once the rename commits the file, nothing left can fail the update."""
    a, b = BudgetOverrides(), BudgetOverrides()
    a.path = b.path = str(tmp_path / "budgets.json")
    a.set("memory.set", 30)
    real_replace, real_stat = os.replace, os.stat
    committed = {"done": False}

    def replace(src: str, dst: str) -> None:
        real_replace(src, dst)
        committed["done"] = True

    def stat(*args: object, **kwargs: object) -> os.stat_result:
        if committed["done"]:
            raise OSError(errno.EIO, "injected stat failure")
        return real_stat(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ratelimit_shared.os, "replace", replace)
    monkeypatch.setattr(ratelimit_shared.os, "stat", stat)
    a.set("memory.set", 0)
    monkeypatch.undo()
    assert a.effective("memory.set", 99) == 0
    assert b.effective("memory.set", 99) == 0
