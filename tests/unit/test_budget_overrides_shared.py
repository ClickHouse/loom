"""Runtime budget overrides bind every web process of one server."""

from __future__ import annotations

from pathlib import Path

from server.observability.budgets import BudgetOverrides


def _pair(tmp_path: Path) -> tuple[BudgetOverrides, BudgetOverrides]:
    a, b = BudgetOverrides(), BudgetOverrides()
    a.path = b.path = str(tmp_path / "budgets.json")
    return a, b


def test_a_set_through_one_process_binds_the_other(tmp_path: Path) -> None:
    a, b = _pair(tmp_path)
    a.set("memory.set", 0)
    assert b.effective("memory.set", 30) == 0
    a.set_for_consumer("bench", "*", "3x")
    assert b.effective("memory.search", 100, consumer="bench") == 300
    a.clear("memory.set")
    a.clear_for_consumer("bench")
    assert b.effective("memory.set", 30) == 30
    assert b.effective("memory.search", 100, consumer="bench") == 100


def test_updates_from_two_processes_both_survive(tmp_path: Path) -> None:
    a, b = _pair(tmp_path)
    b.get("x")
    a.set("memory.set", 5)
    b.set("memory.search", 7)
    c = BudgetOverrides()
    c.path = a.path
    assert c.snapshot() == {"memory.set": 5, "memory.search": 7}


def test_one_web_process_keeps_them_in_memory(tmp_path: Path, monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "web_workers", 1)
    a = BudgetOverrides()
    a.set("memory.set", 1)
    assert a.get("memory.set") == 1
    assert a._document() is None
