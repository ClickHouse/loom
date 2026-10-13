"""The host-shared override stores (budgets, rate limits): concurrent writers in
separate processes never drop each other's update, and an update that would
bind only the worker that took it is refused and rolled back."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from server.core import shared_document
from server.core.errors import BackendUnavailableError
from server.core.ratelimit import Limit, Limiter
from server.observability.budgets import BudgetOverrides

ROOT = Path(__file__).resolve().parents[2]
WRITERS, UPDATES = 4, 10

_BUDGET_WRITER = """
import sys
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
    env = {"LOOM_RATELIMIT_SHARED": "on", "LOOM_RATELIMIT_SHARED_PATH": table}
    _race(_LIMIT_WRITER, table, env)
    ns = json.loads(Path(table + ".overrides.json").read_text())["ns"]
    assert len(ns) == WRITERS * UPDATES


def _no_space(*_a: object) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


def _deny_reads_of(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    real = shared_document.open_private

    def open_private(p: str, flags: int) -> int:
        if p == path:
            raise PermissionError(errno.EACCES, "denied", p)
        return real(p, flags)

    monkeypatch.setattr(shared_document, "open_private", open_private)


def _budgets(tmp_path: Path) -> tuple[BudgetOverrides, BudgetOverrides, Path]:
    path = tmp_path / "budgets.json"
    a, b = BudgetOverrides(), BudgetOverrides()
    a.path = b.path = str(path)
    return a, b, path


@pytest.fixture
def limiters(settings_override, tmp_path: Path) -> tuple[Limiter, Limiter, Path]:
    table = tmp_path / "rl.buckets"
    settings_override(ratelimit_shared="on", ratelimit_shared_path=str(table))
    return Limiter(), Limiter(), Path(f"{table}.overrides.json")


def test_an_unpublished_budget_update_is_refused_and_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, _ = _budgets(tmp_path)
    a.set("memory.set", 30)
    monkeypatch.setattr(shared_document.os, "replace", _no_space)
    with pytest.raises(BackendUnavailableError, match="not applied"):
        a.set("memory.set", 0)
    with pytest.raises(BackendUnavailableError):
        a.clear("memory.set")
    assert a.effective("memory.set", 99) == 30
    assert b.effective("memory.set", 99) == 30


def test_an_unpublished_rate_limit_update_is_refused_and_rolled_back(
    limiters: tuple[Limiter, Limiter, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, _, _ = limiters
    monkeypatch.setattr(shared_document.os, "replace", _no_space)
    with pytest.raises(BackendUnavailableError, match="not applied"):
        a.set_namespace_limit("ns1", "memory", Limit(capacity=1, rate=0.0))
    assert ("ns1", "memory") not in a._ns_overrides


def test_a_budget_update_over_an_unreadable_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, path = _budgets(tmp_path)
    b.set("memory.search", 30)
    a.set("memory.set", 0)
    before = path.read_bytes()
    _deny_reads_of(monkeypatch, str(path))
    with pytest.raises(BackendUnavailableError, match="not applied"):
        b.set("memory.get", 5)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert b.effective("memory.set", 99) == 0


def test_a_rate_limit_update_over_an_unreadable_file_is_refused(
    limiters: tuple[Limiter, Limiter, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b, overrides = limiters
    b.set_namespace_limit("ns_b", "memory", Limit(capacity=30, rate=1.0))
    a.set_namespace_limit("ns_a", "memory", Limit(capacity=1, rate=0.0))
    before = overrides.read_bytes()
    _deny_reads_of(monkeypatch, str(overrides))
    with pytest.raises(BackendUnavailableError, match="not applied"):
        b.set_namespace_limit("ns_b", "memory", Limit(capacity=2, rate=1.0))
    monkeypatch.undo()
    assert overrides.read_bytes() == before
    a.reset_all()
    b.reset_all()


def test_an_update_repairs_a_corrupt_override_file(
    limiters: tuple[Limiter, Limiter, Path],
) -> None:
    a, b, overrides = limiters
    a.set_namespace_limit("ns1", "memory", Limit(capacity=1, rate=0.0))
    overrides.write_text("{not json")
    b.set_namespace_limit("ns2", "memory", Limit(capacity=2, rate=0.0))
    a.check("ns0", "memory.set", org="o")
    assert a._ns_overrides == {("ns2", "memory"): Limit(capacity=2, rate=0.0)}
    a.reset_all()
    b.reset_all()


@pytest.mark.parametrize(
    "shared",
    [
        {"ratelimit_shared": "on", "ratelimit_shared_path": "/proc/loom-test.buckets"},
        {"ratelimit_shared": "off", "web_workers": 3},
    ],
    ids=["table-did-not-open", "sharing-off"],
)
def test_an_override_that_would_bind_one_worker_is_refused(settings_override, shared) -> None:
    settings_override(**shared)
    lim = Limiter()
    assert not lim.shared_active
    with pytest.raises(BackendUnavailableError, match="this worker only"):
        lim.set_namespace_limit("ns1", "memory", Limit(capacity=3, rate=0.0))
    assert ("ns1", "memory") not in lim._ns_overrides


def test_one_worker_without_the_shared_table_keeps_its_overrides(settings_override) -> None:
    settings_override(ratelimit_shared="auto", web_workers=1)
    lim = Limiter()
    lim.set_namespace_limit("ns1", "memory", Limit(capacity=3, rate=0.0))
    assert lim._ns_overrides[("ns1", "memory")] == Limit(capacity=3, rate=0.0)
    lim.reset_all()


@pytest.mark.asyncio
async def test_admin_ratelimit_set_cannot_acknowledge_a_worker_local_change(
    settings_override, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import ratelimit
    from server.insights.app.admin_models import RatelimitSetInput
    from server.insights.app.admin_routes import admin_ratelimit_set

    settings_override(ratelimit_shared="off", web_workers=3)
    lim = Limiter()
    monkeypatch.setattr(ratelimit, "get_limiter", lambda: lim)
    with pytest.raises(BackendUnavailableError) as refused:
        await admin_ratelimit_set(
            RatelimitSetInput(namespace="ns1", op_class="memory", capacity=3, rate=0.0)
        )
    assert refused.value.status_code == 503
    assert ("ns1", "memory") not in lim._ns_overrides
