"""MCP keeps index status brief and shares code search's deployment defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.code.api.models_status import CodeIndexStatusInput
from server.transport.mcp_defaults import mcp_default_arguments

ROOT = Path(__file__).resolve().parents[2]


def test_index_status_is_brief_over_mcp_unless_asked_otherwise() -> None:
    assert mcp_default_arguments("code.index_status", {})["brief"] is True
    assert mcp_default_arguments("code.index_status", {"brief": False})["brief"] is False
    assert (
        CodeIndexStatusInput.model_validate(mcp_default_arguments("code.index_status", {})).brief
        is True
    )


def test_code_search_uses_the_deployment_token_budget_over_mcp() -> None:
    from server.code.api.models_search import CodeSearchInput

    assert "token_budget" not in mcp_default_arguments("code.search", {"query": "x"})
    assert (
        mcp_default_arguments("code.search", {"query": "x", "token_budget": 4000})["token_budget"]
        == 4000
    )
    assert CodeSearchInput(query="x").token_budget is None  # HTTP keeps the handler's default
    # code.symbol keeps its own default: a definition cut short sends the agent to the file
    assert "token_budget" not in mcp_default_arguments("code.symbol", {"name": "X"})


@pytest.mark.parametrize("default", ["auto", "none"])
def test_code_search_uses_the_deployment_body_mode_over_mcp(
    monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    from server.code.api.models_search import CodeSearchInput
    from server.code.app.search.shared import page_plan
    from server.settings import settings

    monkeypatch.setattr(settings, "code_search_bodies_default", default)
    args = mcp_default_arguments("code.search", {"query": "make_url"})
    assert "bodies" not in args
    assert page_plan(CodeSearchInput.model_validate(args)).bodies == default
    explicit = "none" if default == "auto" else "auto"
    args = mcp_default_arguments("code.search", {"query": "make_url", "bodies": explicit})
    assert page_plan(CodeSearchInput.model_validate(args)).bodies == explicit


def test_symbol_history_and_grep_page_defaults_remain_explicitly_overridable() -> None:
    from server.code.api.models_symbol import CodeSymbolInput

    args = mcp_default_arguments("code.symbol", {"name": "X"})
    # the linked memories stay; the recent PRs serve a review, not a lookup
    assert "include_lessons" not in args and args["include_history"] is False
    asked = mcp_default_arguments("code.symbol", {"name": "X", "include_history": True})
    assert asked["include_history"] is True
    assert CodeSymbolInput(name="X").include_history is True  # HTTP keeps the full answer
    assert mcp_default_arguments("code.grep", {"pattern": "x"})["limit"] == 10
    assert mcp_default_arguments("code.grep", {"pattern": "x", "limit": 50})["limit"] == 50


def test_other_ops_and_non_dict_arguments_pass_through() -> None:
    args = {"query": "x"}
    assert mcp_default_arguments("code.callers", args) is args
    assert mcp_default_arguments("code.index_status", None) is None


@pytest.mark.parametrize("option", ["related_on_miss", "related_on_prefix"])
def test_coding_grep_suggests_related_source_with_explicit_opt_out(option: str) -> None:
    assert mcp_default_arguments("code.grep", {}, toolset="coding")[option]
    assert option not in mcp_default_arguments("code.grep", {}, toolset="core")
    assert mcp_default_arguments("code.grep", {option: False}, toolset="coding")[option] is False


def test_coding_symbol_calls_include_test_source_by_default() -> None:
    args = mcp_default_arguments("code.symbol", {"name": "X"}, toolset="coding")
    assert args["test_examples_only"] is True
    assert args["example_context_lines"] == 10
    choices = {"test_examples_only": False, "example_context_lines": 0, "include_examples": False}
    assert mcp_default_arguments("code.symbol", choices, toolset="coding") == {
        "include_history": False,
        **choices,
    }
    assert "test_examples_only" not in mcp_default_arguments("code.symbol", {}, toolset="core")


def test_http_keeps_the_full_default() -> None:
    # the op's own default is None (= brief only with `commit`); only MCP flips it
    assert CodeIndexStatusInput().brief is None


def test_coding_symbol_metadata_omits_example_source_unless_requested() -> None:
    from server.code.api.models_symbol import CodeSymbolInput
    from server.operations.registry import ALL_OPERATIONS
    from server.transport.mcp_call import _validated_input

    op = next(op for op in ALL_OPERATIONS if op.name == "code.symbol")
    metadata = {"name": "X", "include_body": "false"}
    result = _validated_input(1, op, metadata, "", toolset="coding")
    assert isinstance(result, CodeSymbolInput) and result.example_context_lines == 0
    result = _validated_input(1, op, {**metadata, "example_context_lines": 2}, "", toolset="coding")
    assert isinstance(result, CodeSymbolInput) and result.example_context_lines == 2


def test_the_mcp_validator_applies_the_defaults() -> None:
    src = (ROOT / "server/transport/mcp_call.py").read_text()
    assert "mcp_default_arguments(op.name, arguments, toolset=toolset)" in src
