"""An operation prices the request shapes its handler
declares; the catalogue lists them; the transport holds a request to the
shape it served."""

from __future__ import annotations

import pytest

from server.observability.budget_shape import (
    budget_shape_var,
    current_budget_shape,
    note_budget_shape,
)
from server.observability.budgets import declared_budget_ms, effective_budget_ms
from server.operations.registry import ALL_OPERATIONS


@pytest.fixture(autouse=True)
def _clear_shape():
    token = budget_shape_var.set("")
    yield
    budget_shape_var.reset(token)


def test_no_shape_is_the_base_budget():
    assert declared_budget_ms("code.symbol", 250) == 250


def test_a_priced_shape_is_held_to_its_own_budget():
    note_budget_shape("ranked")
    assert current_budget_shape() == "ranked"
    assert declared_budget_ms("code.symbol", 250) == 3000
    assert effective_budget_ms("code.symbol", 250) >= 3000


def test_an_unpriced_shape_falls_back_to_the_base_budget():
    note_budget_shape("something-else")
    assert declared_budget_ms("code.symbol", 250) == 250


def test_an_operation_without_shapes_is_unaffected():
    note_budget_shape("ranked")
    assert declared_budget_ms("memory.get", 400) == 400
    assert declared_budget_ms("no.such.op", 400) == 400


def test_note_is_bounded_and_clears():
    note_budget_shape("s" * 100)
    assert len(current_budget_shape()) == 32
    note_budget_shape(None)
    assert current_budget_shape() == ""


def test_code_symbol_prices_the_ranked_and_brief_shapes():
    op = next(o for o in ALL_OPERATIONS if o.name == "code.symbol")
    assert op.shape_budgets_p99_ms == {"ranked": 3000, "uses_brief": 1500}


def test_every_shape_budget_is_at_least_the_base_budget():
    # a shape is more work under the same name, never less; a shape priced
    # below the base would hide a slow default call behind a note
    for op in ALL_OPERATIONS:
        for shape, ms in op.shape_budgets_p99_ms.items():
            assert ms >= op.budget_p99_ms, (op.name, shape, ms)


@pytest.mark.asyncio
async def test_catalogue_lists_shape_budgets():
    from server.main import list_operations

    rows = {r["name"]: r for r in await list_operations()}
    assert rows["code.symbol"]["shape_budgets_p99_ms"] == {"ranked": 3000, "uses_brief": 1500}
    assert rows["memory.get"]["shape_budgets_p99_ms"] == {}


@pytest.mark.asyncio
async def test_operations_rows_are_the_catalogue_entries_in_registry_order():
    from server.main import list_operations

    rows = await list_operations()
    served = [op for op in ALL_OPERATIONS if op.available]  # a feature off lists none of its ops
    assert [r["name"] for r in rows] == [op.name for op in served]
    for row, op in zip(rows, served, strict=True):
        assert row == {
            "name": op.name,
            "path": op.http_path,
            "mcp_tool": op.mcp_tool_name,
            "idempotent": op.idempotent,
            "budget_p99_ms": op.budget_p99_ms,
            "shape_budgets_p99_ms": dict(op.shape_budgets_p99_ms),
            "required_role": op.required_role.name,
        }
        assert list(row) == [
            "name",
            "path",
            "mcp_tool",
            "idempotent",
            "budget_p99_ms",
            "shape_budgets_p99_ms",
            "required_role",
        ]


def test_capabilities_catalogue_lists_shape_budgets():
    from server.operations.capabilities import capability_snapshot

    ops = capability_snapshot()["ops"]
    assert ops["code.symbol"]["shape_budgets_p99_ms"] == {"ranked": 3000, "uses_brief": 1500}
