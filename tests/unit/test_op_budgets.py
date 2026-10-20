"""Every operation's latency budget is one entry of the table in server/settings/catalog.py,
and a deployment tunes an entry without editing it."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from server.observability.budgets import BudgetOverrides
from server.operations.registry import ALL_OPERATIONS
from server.settings import Settings, settings
from server.settings.catalog import OP_BUDGETS_P99_MS, OP_SHAPE_BUDGETS_P99_MS
from server.tasks.api.models_task import MAX_WAIT_SECONDS


def test_every_registered_op_has_a_budget_and_every_budget_names_an_op() -> None:
    names = {op.name for op in ALL_OPERATIONS}
    assert sorted(names - set(OP_BUDGETS_P99_MS)) == [], "ops with no budget"
    assert sorted(set(OP_BUDGETS_P99_MS) - names) == [], "budgets for no op"
    assert sorted(set(OP_SHAPE_BUDGETS_P99_MS) - names) == [], "shape budgets for no op"
    assert all(isinstance(ms, int) and ms > 0 for ms in OP_BUDGETS_P99_MS.values())


def test_an_op_reads_its_budget_from_the_table() -> None:
    by_name = {op.name: op for op in ALL_OPERATIONS}
    assert by_name["memory.search"].budget_p99_ms == OP_BUDGETS_P99_MS["memory.search"]
    assert by_name["code.symbol"].shape_budgets_p99_ms == OP_SHAPE_BUDGETS_P99_MS["code.symbol"]
    assert by_name["memory.get"].shape_budgets_p99_ms == {}


def test_a_long_poll_budget_is_its_wait_plus_three_seconds() -> None:
    for name in ("task.pull", "task.updates"):
        assert OP_BUDGETS_P99_MS[name] == (MAX_WAIT_SECONDS + 3) * 1000


@pytest.mark.parametrize(
    ("raw", "budget"),
    [
        ('{"memory.search": 900}', 900),  # ms replaces the declared budget
        ('{"memory.search": "2x"}', 600),  # a multiplier of it
        ('{"memory.get": 900}', 300),  # another op's entry leaves this one alone
        ("", 300),
    ],
)
def test_the_deployment_overrides_one_op_by_setting(
    monkeypatch: pytest.MonkeyPatch, raw: str, budget: int
) -> None:
    monkeypatch.setattr(settings, "latency_budget_overrides", raw)
    assert BudgetOverrides().effective("memory.search", 300) == budget


def test_a_runtime_override_wins_over_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    overrides = BudgetOverrides()
    monkeypatch.setattr(settings, "latency_budget_overrides", '{"memory.search": 900}')
    overrides.set("memory.search", 50)
    assert overrides.effective("memory.search", 300) == 50


@pytest.mark.parametrize("raw", ['{"memory.search": "lots"}', '{"memory.search": -5}'])
def test_an_unusable_budget_stops_startup(raw: str) -> None:
    """The refusal names the op, never the value. The field is not live, so the settings
    file and the environment are the only ways in."""
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, latency_budget_overrides=raw)  # type: ignore[call-arg]
    assert "memory.search" in str(caught.value)
    assert "lots" not in str(caught.value) and "-5" not in str(caught.value)


def test_an_op_this_build_lacks_is_logged_and_ignored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Builds N-1 and N read one settings file: an op one of them lacks must not stop it."""
    raw = '{"memory.search": 900, "memory.a_later_op": 50}'
    Settings(_env_file=None, latency_budget_overrides=raw)  # type: ignore[call-arg]
    monkeypatch.setattr(settings, "latency_budget_overrides", raw)
    with caplog.at_level(logging.WARNING):
        assert BudgetOverrides().effective("memory.search", 300) == 900
    assert "memory.a_later_op" in caplog.text
