"""A call with a wrong argument says how to make it right.

A bare `-32602 "Input validation failed"` gives the model nothing to act on,
since Claude Code shows it only the message. So a validation failure names the
argument and a suggestion (synonyms and containment as well as difflib, which
alone misses confusions like key_prefix, block, memory_keys, query, text),
`help` lists optional args too, and an empty `inbox.list` says it is empty
rather than answering `{}`.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

import server.transport.mcp_call as mcp_call
from server.memory.api.models_apply import ApplyTarget
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import arg_check, mcp_op
from server.transport._common import validation_errors
from server.transport.arg_feedback import feedback, guesses, optional_args, required_args
from server.transport.lean_results import lean_result


def _model(name: str) -> Any:
    return OPERATIONS_BY_NAME[name].input_model


# ------------------------------------------------ did_you_mean beyond difflib


@pytest.mark.parametrize(
    ("op", "key", "meant"),
    [
        ("memory.list", "key_prefix", "prefix"),  # synonym / stem
        ("working_memory.delete", "block", "block_key"),  # contained in
        ("working_memory.append", "text", "addition"),  # synonym
        ("tracker.similar", "query", "text"),  # synonym where `text` exists
        ("tracker.similar", "limit", "top_k"),  # a common ignored arg
        ("task.complete", "outcom", "outcome"),  # difflib still first
    ],
)
def test_the_confusions_real_sessions_made_have_a_guess(op: str, key: str, meant: str) -> None:
    assert arg_check.did_you_mean(_model(op), [key]) == {key: meant}


def test_a_nested_model_key_has_a_guess() -> None:
    assert arg_check.did_you_mean(ApplyTarget, ["memory_keys"]) == {"memory_keys": "keys"}


@pytest.mark.parametrize(
    ("op", "keys"),
    [
        # unknown keys shipped clients are known to send (from the `ignored_args`
        # log): a guess here would make a write refuse plugin traffic
        ("observe.record", ["memory_type"]),
        ("observe.crystallize", ["reason", "tags"]),
        ("task.heartbeat", ["note"]),
    ],
)
def test_client_traffic_keys_get_no_guess_so_writes_keep_serving(op: str, keys: list[str]) -> None:
    assert arg_check.did_you_mean(_model(op), keys) == {}


def test_a_synonym_counts_only_where_the_op_has_the_field() -> None:
    assert arg_check.did_you_mean(_model("memory.list"), ["query"]) == {}
    assert arg_check.closest_field("query", ["issue_number", "frames"]) is None


def test_containment_needs_a_single_candidate() -> None:
    assert arg_check.closest_field("id", ["task_id", "run_id_x"]) is None
    assert arg_check.closest_field("block", ["block_key", "separator"]) == "block_key"


def test_the_silent_working_memory_delete_is_now_refused() -> None:
    op = OPERATIONS_BY_NAME["working_memory.delete"]
    refused = arg_check.named_argument_error(
        op, {"org": "o", "namespace": "n", "agent": "a", "block": "loomcheck"}
    )
    assert refused is not None
    detail = refused.error_detail()
    assert detail["code"] == "UNKNOWN_ARGUMENT" and detail["did_you_mean"] == {"block": "block_key"}


# ----------------------------------------------------- the feedback block


def _fail(op_name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    op = OPERATIONS_BY_NAME[op_name]
    full = {"org": "o", "namespace": "n", "agent": "a", **args}
    with pytest.raises(Exception) as ei:
        op.input_model.model_validate(full)
    return feedback(op, full, validation_errors(ei.value))


def test_memory_apply_names_the_nested_key_and_shows_the_corrected_call() -> None:
    msg, data = _fail(
        "memory.apply",
        {"target": {"memory_keys": ["test/x/a", "test/x/b"]}, "action": "expire", "reason": "r"},
    )
    assert "target must name exactly one of keys | filter | search" in msg
    assert "'target.memory_keys' — did you mean 'target.keys'?" in msg
    assert "required: target, action, reason" in msg and "optional: " in msg
    assert data["example"]["target"] == {"keys": ["test/x/a", "test/x/b"]}
    assert "org" not in data["example"]  # identity stays with the server's defaults
    assert data["errors"] and data["unknown"] == {"target.memory_keys": "target.keys"}
    assert len(msg) < 600  # ~150 tokens


def test_working_memory_append_names_addition() -> None:
    msg, data = _fail("working_memory.append", {"text": "hello"})
    assert "addition: Field required [missing]" in msg
    assert "'text' — did you mean 'addition'?" in msg
    assert data["example"] == {"addition": "hello"}


def test_a_missing_required_arg_gets_a_placeholder() -> None:
    _, data = _fail("tracker.flaky", {})
    assert data["example"] == {"test_name": "…"}


def test_hidden_and_identity_fields_are_never_named() -> None:
    for op in OPERATIONS_BY_NAME.values():
        names = set(required_args(op.input_model)) | set(optional_args(op.input_model))
        assert not names & arg_check.IDENTITY_KEYS, op.name
        for n in names:
            extra = op.input_model.model_fields[n].json_schema_extra
            assert not (isinstance(extra, dict) and extra.get("x-mcp-hidden")), (op.name, n)


def test_guesses_skip_a_nested_value_that_is_not_an_object() -> None:
    assert guesses(_model("memory.apply"), {"target": "k"}) == {}


# ---------------------------------------------------- over the MCP transport


def _patched(monkeypatch: pytest.MonkeyPatch, tool: str, ran: list[Any]) -> None:
    async def _handler(inp: Any) -> Any:
        ran.append(inp)
        return {}

    monkeypatch.setitem(
        mcp_call._TOOLS_BY_NAME,
        tool,
        dataclasses.replace(mcp_call._TOOLS_BY_NAME[tool], handler=_handler),
    )

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "n", "agent": "claude-code"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)


def test_validation_failure_is_a_tool_result_the_model_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = "loom_working_memory_append"
    ran: list[Any] = []
    _patched(monkeypatch, tool, ran)
    resp = asyncio.run(
        mcp_call._call_tool(1, {"name": tool, "arguments": {"separator": 3}}, auth=None)
    )
    result = resp["result"]
    assert result["isError"] is True and not ran
    text = result["content"][0]["text"]
    assert (
        "addition: Field required" in text and 'example: {"separator": 3, "addition": "…"}' in text
    )
    assert result["structuredContent"]["errors"]


def test_a_refused_write_names_its_args(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = "loom_working_memory_delete"
    ran: list[Any] = []
    _patched(monkeypatch, tool, ran)
    resp = asyncio.run(
        mcp_call._call_tool(1, {"name": tool, "arguments": {"block": "loomcheck"}}, auth=None)
    )
    result = resp["result"]
    assert result["isError"] is True and not ran
    text = result["content"][0]["text"]
    assert "'block' → 'block_key'" in text and "optional: block_key" in text


# ------------------------------------------------------------ loom_op help


def test_help_rows_carry_optional_args() -> None:
    row = next(
        r
        for r in mcp_op.catalog("working_memory.append")["ops"]
        if r["op"] == "working_memory.append"
    )
    assert row["required"] == ["addition"]
    assert row["optional"] == ["block_key", "separator"]


def test_a_help_row_shows_a_nested_args_shape() -> None:
    row = next(r for r in mcp_op.catalog("memory.apply")["ops"] if r["op"] == "memory.apply")
    assert row["required"][0] == "target{keys|filter|search}"


def test_an_unknown_op_answers_the_near_ops_with_their_args() -> None:
    req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "loom_op", "arguments": {"op": "tracker.similiar"}},
    }
    _, resp = mcp_op.route(req, toolset=None)
    assert resp is not None
    body = resp["result"]["structuredContent"]
    assert body["code"] == "UNKNOWN_OP" and body["did_you_mean"][0] == "tracker.similar"
    first = body["ops"][0]
    assert first["op"] == "tracker.similar" and "text" in first["optional"]


# ------------------------------------------------------------ lean results


def test_an_empty_top_level_list_stays_a_list() -> None:
    assert lean_result({"inboxes": []}, {}) == {"inboxes": []}
    # nested empties are still dropped
    assert lean_result({"items": [{"a": 1, "tags": []}]}, {}) == {"items": [{"a": 1}]}
    assert lean_result({"note": "", "meta": {}}, {}) == {}
