"""An op whose input declares `idempotency_key` keeps it over MCP: the
transport reads the key for its replay cache but must not strip the field
the handler dedupes on (task.create's durable dedupe reads it)."""

from __future__ import annotations

from server.operations.registry import ALL_OPERATIONS
from server.transport.mcp_call import _pop_idempotency_key

_OPS = {op.name: op for op in ALL_OPERATIONS}


def test_a_declared_idempotency_key_reaches_the_handler() -> None:
    args = {"inbox": "i", "kind": "k", "title": "t", "idempotency_key": "abc"}
    assert _pop_idempotency_key(_OPS["task.create"], args) == "abc"
    assert args["idempotency_key"] == "abc"


def test_an_undeclared_idempotency_key_is_still_stripped() -> None:
    op = next(
        o
        for o in ALL_OPERATIONS
        if o.idempotent and "idempotency_key" not in o.input_model.model_fields
    )
    args = {"idempotency_key": "abc"}
    assert _pop_idempotency_key(op, args) == "abc"
    assert "idempotency_key" not in args
