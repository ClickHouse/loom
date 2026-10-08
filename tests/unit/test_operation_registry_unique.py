"""The op registry refuses a duplicate op name or MCP tool name at import.

`OPERATIONS_BY_NAME` (and MCP's tool map) is a dict over the registry, so a
second declaration under the same name — or two names that derive the same
`loom_<name>` tool — would silently shadow the first on every transport.
"""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import BaseModel

from server.operations.base import Operation, Role
from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME, index_by_name


class _M(BaseModel):
    pass


async def _h(inp: _M) -> _M:
    return inp


def _op(name: str) -> Operation:
    return Operation(
        name=name,
        input_model=_M,
        output_model=_M,
        handler=_h,
        idempotent=True,
        budget_p99_ms=10,
        required_role=Role.VIEWER,
    )


def test_the_registry_indexes_every_op_once() -> None:
    assert len(OPERATIONS_BY_NAME) == len(ALL_OPERATIONS)
    assert len({op.mcp_tool_name for op in ALL_OPERATIONS}) == len(ALL_OPERATIONS)


def test_a_duplicate_op_name_is_refused() -> None:
    with pytest.raises(ValueError, match="declared twice"):
        index_by_name([_op("x.get"), _op("x.get")])


def test_two_names_deriving_one_tool_name_are_refused() -> None:
    with pytest.raises(ValueError, match="share the MCP tool name 'loom_a_b_c'"):
        index_by_name([_op("a.b_c"), _op("a_b.c")])


def test_an_explicit_tool_name_counts() -> None:
    renamed = dataclasses.replace(_op("q.find"), mcp_tool="loom_x_get")
    with pytest.raises(ValueError, match="share the MCP tool name"):
        index_by_name([_op("x.get"), renamed])
