"""An argument the op does not read is named, a write's
misspelled argument is refused, and a ClickHouse zero date answers null.

A task completed with ``result: {"text": "hi"}`` (the field is ``outcome``)
must not answer 200 ``done`` with an empty outcome, and `memory.get` must not
answer ``last_recalled_at: "1970-01-01T00:00:00"`` for a row never recalled.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.transport import arg_check
from server.transport.epoch_nulls import is_epoch, scrub

# ---------------------------------------------------------------- the rule


class _M(BaseModel):
    org: str
    namespace: str
    outcome: dict[str, Any] = Field(default_factory=dict)
    top_k: int = Field(default=5, validation_alias=AliasChoices("top_k", "k"))
    tags: list[str] = Field(default_factory=list)


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")
    a: int = 0


def test_unknown_args_names_what_the_model_drops_and_nothing_else() -> None:
    args = {
        "org": "o",
        "namespace": "n",
        "outcome": {},
        "k": 3,  # fields and an alias
        "consumer": "x",
        "traffic_class": "bench",
        "explain": True,  # the transport's own
        "_idempotency_key": "i",
        "agent": "a",
        "session_id": "s",
        "user_id": "-",
        "run_id": "r",
        "result": {"text": "hi"},
        "tag": "x",
    }
    assert arg_check.unknown_args(_M, args) == ["result", "tag"]
    assert arg_check.did_you_mean(_M, ["result", "tag"]) == {"tag": "tags"}
    assert arg_check.unknown_args(_Open, {"anything": 1}) == []  # the model keeps extras itself
    assert arg_check.unknown_args(_M, None) == [] and arg_check.unknown_args(_M, {}) == []


def test_a_write_refuses_a_close_match_and_serves_everything_else() -> None:
    complete = OPERATIONS_BY_NAME["task.complete"]
    search = OPERATIONS_BY_NAME["memory.search"]
    typo = {"org": "o", "namespace": "n", "task_id": "t", "fence": 1, "outcom": {"text": "hi"}}
    block = arg_check.ignored_block(complete, typo)
    assert block == {"ignored_args": ["outcom"], "did_you_mean": {"outcom": "outcome"}}
    detail = arg_check.refusal(complete, block)
    assert detail is not None and detail["code"] == "UNKNOWN_ARGUMENT"
    assert "Nothing was written" in detail["message"] and detail["did_you_mean"] == {
        "outcom": "outcome"
    }
    # No close field: served, and the answer names the key.
    far = {k: v for k, v in typo.items() if k != "outcom"} | {"result": {"text": "hi"}}
    assert arg_check.refusal(complete, arg_check.ignored_block(complete, far)) is None
    # A read op never refuses, even a close match.
    rblock = arg_check.ignored_block(
        search, {"org": "o", "namespace": "n", "query": "q", "top_kk": 3}
    )
    assert (
        rblock["did_you_mean"] == {"top_kk": "top_k"} and arg_check.refusal(search, rblock) is None
    )


def test_a_viewer_op_that_changes_state_refuses_a_close_match_too() -> None:
    """The write test is `Operation.read_only`, not the role: a VIEWER-floor
    op that registers, restores or records state (audited, or `writes`)
    would otherwise drop `rerank_endpiont` and make the write without it."""
    register = OPERATIONS_BY_NAME["namespace.register"]
    assert register.required_role.name == "VIEWER" and not register.read_only
    typo = {"org": "o", "namespace": "n", "rerank_endpiont": "https://r"}
    block = arg_check.ignored_block(register, typo)
    assert block["did_you_mean"] == {"rerank_endpiont": "rerank_endpoint"}
    refused = arg_check.named_argument_error(register, typo)
    assert refused is not None and refused.error_detail()["code"] == "UNKNOWN_ARGUMENT"


# ---------------------------------------------------------------- HTTP


@pytest.fixture()
def _http(monkeypatch: pytest.MonkeyPatch):
    from server.transport import http as http_mod

    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: emitted.append(k))
    calls: list[Any] = []

    def mount(op_name: str, result: Any) -> TestClient:
        async def _handler(inp: Any) -> Any:
            calls.append(inp)
            return result

        op = dataclasses.replace(OPERATIONS_BY_NAME[op_name], handler=_handler)
        app = FastAPI()
        app.add_api_route(f"/v1/{op_name}", http_mod._make_endpoint(op), methods=["POST"])
        return TestClient(app)

    return mount, calls, emitted


def _task_out() -> Any:
    from server.tasks.api.models_task import TaskPayload

    out = OPERATIONS_BY_NAME["task.complete"].output_model
    return out.model_construct(task=TaskPayload.model_construct(task_id="t", status="done"))


def test_http_names_the_dropped_key_and_refuses_the_misspelled_one(_http) -> None:
    mount, calls, emitted = _http
    c = mount("task.complete", _task_out())
    body = {"org": "o", "namespace": "n", "agent": "a", "task_id": "t", "fence": 1}
    r = c.post("/v1/task.complete", json={**body, "result": {"text": "hi"}})
    assert r.status_code == 200, r.text
    assert r.json()["ignored_args"] == ["result"] and "did_you_mean" not in r.json()
    assert r.headers["X-Loom-Ignored-Args"] == "result"
    assert len(calls) == 1
    r = c.post("/v1/task.complete", json={**body, "outcom": {"text": "hi"}})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "UNKNOWN_ARGUMENT" and detail["did_you_mean"] == {"outcom": "outcome"}
    assert len(calls) == 1, "the handler never ran"
    assert emitted[-1]["error_code"] == "UNKNOWN_ARGUMENT"
    r = c.post("/v1/task.complete", json={**body, "outcome": {"text": "hi"}})
    assert r.status_code == 200 and "ignored_args" not in r.json()
    assert "X-Loom-Ignored-Args" not in r.headers
    assert calls[-1].outcome == {"text": "hi"}


def test_http_memory_get_answers_null_for_a_zero_date_and_keeps_the_stored_value(_http) -> None:
    mount, _calls, _ = _http
    out = OPERATIONS_BY_NAME["memory.get"].output_model
    epoch = datetime(1970, 1, 1)
    res = out.model_construct(
        found=True,
        value={"due_at": "1970-01-01T00:00:00"},  # the caller's own payload: returned as stored
        last_recalled_at=epoch,
        created_at=datetime(2026, 9, 24, 14, 51, 24),
    )
    c = mount("memory.get", res)
    r = c.post(
        "/v1/memory.get", json={"org": "o", "namespace": "n", "agent": "a", "memory_key": "k"}
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["last_recalled_at"] is None
    assert j["created_at"].startswith("2026-09-24")
    assert j["value"] == {"due_at": "1970-01-01T00:00:00"}


# ---------------------------------------------------------------- MCP


def test_mcp_structured_content_names_the_dropped_key_and_nulls_the_zero_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.transport.mcp_call as mcp_call

    out_model = OPERATIONS_BY_NAME["memory.get"].output_model

    async def _handler(inp: Any) -> Any:
        return out_model.model_construct(
            found=True, value="v", last_recalled_at=datetime(1970, 1, 1)
        )

    tool = "loom_memory_get"
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME,
        tool,
        dataclasses.replace(mcp_call.TOOLS_BY_NAME[tool], handler=_handler),
    )

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "n", "agent": "claude-code"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    resp = asyncio.run(
        mcp_call._call_tool(
            1,
            {"name": tool, "arguments": {"memory_key": "k", "feilds": ["value"]}},
            auth=None,
        )
    )
    sc = resp["result"]["structuredContent"]
    assert sc["ignored_args"] == ["feilds"] and sc["did_you_mean"] == {"feilds": "fields"}
    assert sc.get("last_recalled_at") is None


def test_mcp_refuses_a_writes_misspelled_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.transport.mcp_call as mcp_call

    ran: list[Any] = []

    async def _handler(inp: Any) -> Any:
        ran.append(inp)
        return _task_out()

    tool = "loom_task_complete"
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME,
        tool,
        dataclasses.replace(mcp_call.TOOLS_BY_NAME[tool], handler=_handler),
    )

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "n", "agent": "claude-code"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    resp = asyncio.run(
        mcp_call._call_tool(
            1,
            {"name": tool, "arguments": {"task_id": "t", "fence": 1, "outcom": {"x": 1}}},
            auth=None,
        )
    )
    # a tool result with isError (the model reads its text), not a
    # JSON-RPC error frame whose data never reached it.
    result = resp["result"]
    assert result["isError"] and result["structuredContent"]["code"] == "UNKNOWN_ARGUMENT", resp
    assert "'outcom' → 'outcome'" in result["content"][0]["text"]
    assert not ran


# ---------------------------------------------------------------- the zero date


class _Row(BaseModel):
    opened_at: Any = None
    created_at: datetime


class _Out(BaseModel):
    last_recalled_at: datetime | None = None
    ts: datetime
    rows: list[_Row] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    value: Any = None


def test_scrub_nulls_only_where_the_model_admits_null() -> None:
    assert is_epoch("1970-01-01 00:00:00") and is_epoch("1970-01-01T00:00:00Z")
    assert is_epoch("1970-01-01T00:00:00.000+00:00") and is_epoch(datetime(1970, 1, 1))
    assert not is_epoch("1970-01-01T00:00:01") and not is_epoch("2026-09-24 00:00:00")
    dumped = {
        "last_recalled_at": "1970-01-01T00:00:00",
        "ts": "1970-01-01T00:00:00",  # required: a bug to see, never hidden
        "rows": [{"opened_at": "1970-01-01 00:00:00", "created_at": "1970-01-01T00:00:00"}],
        "items": [
            {"opened_at": "1970-01-01 00:00:00", "title": "1970-01-01", "merged_at": "2025-10-02"}
        ],
        "value": {"due_at": "1970-01-01T00:00:00"},
    }
    out = scrub(_Out, dumped)
    assert out["last_recalled_at"] is None and out["ts"] == "1970-01-01T00:00:00"
    assert out["rows"] == [{"opened_at": None, "created_at": "1970-01-01T00:00:00"}]
    assert out["items"] == [{"opened_at": None, "title": "1970-01-01", "merged_at": "2025-10-02"}]
    assert out["value"] == {"due_at": "1970-01-01T00:00:00"}
    assert scrub(_Out, ["not", "a", "dict"]) == ["not", "a", "dict"]


def test_code_pr_check_items_lose_the_zero_open_date() -> None:
    op = OPERATIONS_BY_NAME["code.pr_check"]
    dumped = {
        "verdict": "related",
        "items": [
            {
                "pr_number": 88001,
                "opened_at": "1970-01-01 00:00:00",
                "merged_at": "2025-10-02 12:26:23",
            }
        ],
    }
    out = scrub(op.output_model, dumped)
    assert (
        out["items"][0]["opened_at"] is None
        and out["items"][0]["merged_at"] == "2025-10-02 12:26:23"
    )
