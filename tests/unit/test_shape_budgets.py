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
    assert declared_budget_ms("memory.search", 400) == 400
    assert declared_budget_ms("no.such.op", 400) == 400


def test_note_is_bounded_and_clears():
    note_budget_shape("s" * 100)
    assert len(current_budget_shape()) == 32
    note_budget_shape(None)
    assert current_budget_shape() == ""


def test_code_symbol_prices_the_ranked_and_brief_shapes():
    op = next(o for o in ALL_OPERATIONS if o.name == "code.symbol")
    assert op.shape_budgets_p99_ms == {"ranked": 3000, "uses_brief": 1500}


@pytest.mark.parametrize(
    ("op_name", "shape"),
    [
        ("memory.get", "audited"),
        ("memory.get_by_id", "audited"),
        ("memory.bulk_get", "audited"),
        ("memory.history", "audited"),
        ("memory.evidence", "audited"),
        ("memory.delete", "webhook"),
        ("memory.delete_version", "webhook"),
        ("memory.update", "webhook"),
        ("memory.update_by_id", "webhook"),
    ],
)
def test_a_memory_op_prices_its_conditional_insert_wait_as_a_shape(op_name, shape):
    # one waited insert (an audit row of a high read, an outbox row of a webhook
    # namespace) adds ch_batched_insert_wait_max_ms to the calls that make it
    from server.settings import Settings

    op = next(o for o in ALL_OPERATIONS if o.name == op_name)
    assert list(op.shape_budgets_p99_ms) == [shape]
    wait_ms = Settings.model_fields["ch_batched_insert_wait_max_ms"].default
    assert op.shape_budgets_p99_ms[shape] == op.budget_p99_ms + wait_ms
    note_budget_shape(shape)
    assert declared_budget_ms(op_name, op.budget_p99_ms) == op.budget_p99_ms + wait_ms


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
    assert rows["memory.search"]["shape_budgets_p99_ms"] == {}


@pytest.mark.asyncio
async def test_operations_rows_are_the_catalogue_entries_in_registry_order():
    from server.main import list_operations

    rows = await list_operations()
    assert [r["name"] for r in rows] == [op.name for op in ALL_OPERATIONS]
    for row, op in zip(rows, ALL_OPERATIONS, strict=True):
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
