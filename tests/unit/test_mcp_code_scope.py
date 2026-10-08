"""The MCP connection names the checkout's code namespace."""

from __future__ import annotations

import inspect
import re

from server.code.domain.scope import (
    apply_code_scope,
    current_code_scope,
    parse_code_scope,
    reset_code_scope,
    set_code_scope,
)
from server.transport import mcp_auth, mcp_call


def test_parse_accepts_org_slash_namespace_and_nothing_else() -> None:
    assert parse_code_scope("clickhouse/code-example-private") == (
        "clickhouse",
        "code-example-private",
    )
    assert parse_code_scope("  acme/code.x_y-1  ") == ("acme", "code.x_y-1")
    for bad in (
        "",
        None,
        "no-slash",
        "/ns",
        "org/",
        "org/ns/extra",
        "or g/ns",
        "org/ns;drop",
        "-org/ns",
    ):
        assert parse_code_scope(bad) is None, bad


def test_scope_fills_only_namespace_less_code_calls() -> None:
    scope = ("clickhouse", "code-example-private")
    args: dict = {"name": "StorageSharedMergeTree::mergeSelectingTask"}
    assert apply_code_scope("code.symbol", args, scope) is True
    assert args["namespace"] == "code-example-private" and args["org"] == "clickhouse"
    # explicit namespace wins; an explicit org is kept
    args = {"namespace": "code-clickhouse", "org": "other"}
    assert apply_code_scope("code.search", args, scope) is False
    assert args == {"namespace": "code-clickhouse", "org": "other"}
    args = {"org": "acme"}
    assert apply_code_scope("code.grep", args, scope) is True
    assert args == {"org": "acme", "namespace": "code-example-private"}
    # memory ops and an absent scope are untouched
    args = {"query": "x"}
    assert apply_code_scope("memory.search", args, scope) is False and args == {"query": "x"}
    assert apply_code_scope("code.symbol", args, None) is False and args == {"query": "x"}


def test_contextvar_round_trip() -> None:
    assert current_code_scope() is None
    tok = set_code_scope("clickhouse/code-loom")
    try:
        assert current_code_scope() == ("clickhouse", "code-loom")
    finally:
        reset_code_scope(tok)
    assert current_code_scope() is None
    tok = set_code_scope("garbage")
    try:
        assert current_code_scope() is None
    finally:
        reset_code_scope(tok)


def test_transport_reads_the_header_and_applies_it_after_the_defaults() -> None:
    assert 'request.headers.get("x-loom-code-scope")' in inspect.getsource(mcp_auth)
    src = inspect.getsource(mcp_call)
    body = src.split("async def _call_tool(", 1)[1]
    defaults_at = body.index("defaults = await _caller_defaults(auth, client_name)")
    scope_at = body.index("_scope_arguments(op.name, arguments, defaults, explicit_agent)")
    assert defaults_at < scope_at, "scope is applied after the identity defaults"
    helper = src.split("def _scope_arguments(", 1)[1].split("async def _call_tool(", 1)[0]
    assert "apply_code_scope(op_name, arguments, current_code_scope())" in helper
    assert 'arguments["agent"] = ""' in helper, "memory.update rule travels with it"
    from server.transport import mcp_primer

    assert re.search(r"Code index scope for this connection", inspect.getsource(mcp_primer))
