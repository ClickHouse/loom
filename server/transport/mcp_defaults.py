"""Argument defaults that apply only when a model calls an op over MCP,
and the options `loom_search` clamps instead of refusing.

HTTP callers keep an op's own defaults: the SDK's capability discovery reads
`ops` / `request_fields` from `code.index_status`, the plugins' session start
reads it, and outside consumers may read its `state`. A model asking "is the
index fresh?" needs none of that. The full answer runs to 15–30k tokens on
a large repository, where the brief one carries the
watermarks, lags and tiers. So over MCP the op defaults to `brief: true`,
and a model that wants the rest passes `brief: false`.

`code.search` uses the same body mode and token budget over MCP and HTTP:
automatic bodies by default, with no token cap unless configured or supplied by the caller.

`code.symbol` over MCP leaves out the recent PRs (about a third of a definition's
tokens): they serve a review, not a where/how lookup, and a model that wants them
passes `include_history`. The lessons stay: up to three complete memories linked to that
symbol within `code_result_lessons_tokens` (unlimited by default). `code.search`, `code.callers`,
`code.callees`, `code.impact`, `code.outline` and a task's `code.context_pack` carry
the memories of their top files (`server.code.app.result_lessons`): as many as fit in
`code_result_lessons_tokens` (unlimited by default), each whole or left out.
Coding connections also default to indexed tests with source excerpts at the definition.

`code.grep` over MCP answers ten matches a page (each with its enclosing
function), the cursor for the rest: in paired runs an agent's grep through the
index cost about three times the shell grep it stood in for.
"""

from __future__ import annotations

from typing import Any

from server.code.api.models_symbol import EDIT_TEST_CONTEXT_LINES
from server.operations.toolsets import resolve_toolset, toolset_parts

MCP_DEFAULT_ARGUMENTS: dict[str, dict[str, Any]] = {
    "code.index_status": {"brief": True},
    "code.symbol": {"include_history": False},
    "code.grep": {"limit": 10},
}

_CODING_SYMBOL_EXAMPLE_DEFAULTS = {
    "test_examples_only": True,
    "example_context_lines": EDIT_TEST_CONTEXT_LINES,
}


def mcp_argument_defaults(op_name: str, toolset: str | None = None) -> dict[str, Any]:
    defaults = MCP_DEFAULT_ARGUMENTS.get(op_name, {})
    if op_name == "code.symbol":
        from server.settings import settings

        if "coding" in toolset_parts(resolve_toolset(toolset, settings.mcp_toolset)):
            return {**defaults, **_CODING_SYMBOL_EXAMPLE_DEFAULTS}
    return defaults


def mcp_default_arguments(op_name: str, arguments: Any, *, toolset: str | None = None) -> Any:
    """`arguments` with the op's MCP defaults filled in where the caller gave
    none; anything that is not a dict is returned untouched for the
    validator to reject."""
    defaults = mcp_argument_defaults(op_name, toolset)
    if not defaults or not isinstance(arguments, dict):
        return arguments
    return {**defaults, **arguments}


def coerce_search_arguments(arguments: dict[str, Any]) -> None:
    """`loom_search`'s options clamped in place to values `search.query`
    accepts, where HTTP would refuse them: a model's `top_k: 500` reads 100,
    an unknown `recipe` the server's default, an unknown `bodies` an excerpt,
    any scope but `this` the fan-out, a `tags` that is not a list none, and a
    `page` handle trimmed to its bound. Bodies default to a 200-character
    excerpt over MCP, a decision or constraint as stored (`memory.get` returns
    a full body on demand)."""
    from typing import get_args

    from server.memory.api.models_loom_search import SearchInput

    fields = SearchInput.model_fields
    try:
        top_k = int(arguments.get("top_k", 5))
    except (TypeError, ValueError):
        top_k = 5
    arguments["top_k"] = max(1, min(100, top_k))
    arguments["namespace_scope"] = (
        "this" if arguments.get("namespace_scope", "all_mine") == "this" else "all_mine"
    )
    raw_tags = arguments.get("tags") or []
    arguments["tags"] = [str(t) for t in raw_tags if t] if isinstance(raw_tags, list) else []
    recipe = str(arguments.get("recipe") or "")
    arguments["recipe"] = recipe if recipe in get_args(fields["recipe"].annotation) else ""
    bodies = str(arguments.get("bodies") or "excerpt")
    arguments["bodies"] = bodies if bodies in get_args(fields["bodies"].annotation) else "excerpt"
    arguments["explain"] = bool(arguments.get("explain") or False)
    arguments["page"] = str(arguments.get("page") or "").strip()[:120]
