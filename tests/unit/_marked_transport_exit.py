"""Isolated transport calls with a real ledger, accumulator and required writer."""

from __future__ import annotations

import asyncio
import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import Request, Response
from pydantic import BaseModel

from server.auth import tenancy
from server.auth.jwt import AuthInfo
from server.core import request_tail, request_work
from server.core.diagnostics import private_work
from server.observability import cost_accumulator, events, ledger
from server.observability.run_context import set_ledger_run
from server.operations.base import Operation, Role
from server.transport import gates, http, mcp_call, post_rows


class Input(BaseModel):
    org: str = ""
    namespace: str = ""
    agent: str = "reader"
    user_id: str = ""
    session_id: str = "session-a"
    run_id: str = "explicit-run"
    query: str = "private query nonce"


class Output(BaseModel):
    answer: str = "private answer nonce"


class Stopped(BaseException):
    pass


def prepare(monkeypatch, transport):
    world = SimpleNamespace(
        rows=[],
        queued=[],
        held=[],
        messages=[],
        ready=asyncio.Event(),
        release=asyncio.Event(),
        insert_started=asyncio.Event(),
        insert_release=asyncio.Event(),
        stage="handler",
        private=False,
        marked=True,
        join_child=False,
        pause_insert=False,
        control=None,
        primary=None,
        accumulator=None,
        tail=None,
        child=None,
    )
    auth = AuthInfo(
        subject="verified-reader",
        org="org-a",
        namespaces=("team-a",),
        roles=(Role.VIEWER,),
        is_service_account=True,
        read_scope="own_session",
        read_user_id="reader-user",
    )
    target = gates.Target("org-a", "team-a", None, "team-a")

    async def insert(table, data, *, settings, required_columns):
        assert table == "loom.agent_events"
        assert settings["async_insert"] == settings["wait_for_async_insert"] == 1
        assert required_columns == frozenset({"mirror_marked"})
        world.insert_started.set()
        if world.pause_insert:
            await world.insert_release.wait()
        world.rows.extend(copy.deepcopy(data))

    client = SimpleNamespace(insert=AsyncMock(side_effect=insert))
    world.client = client
    emitter = SimpleNamespace(emit=world.queued.append)
    monkeypatch.setattr(tenancy, "attributable", lambda org: True)
    monkeypatch.setattr(events, "get_client", lambda: client)
    monkeypatch.setattr(events, "get_emitter", lambda: emitter)
    monkeypatch.setattr(events, "stamp_row", lambda row: row)
    monkeypatch.setattr(events, "for_this_cell", lambda event: event)
    monkeypatch.setattr(ledger, "get_emitter", lambda: emitter)
    monkeypatch.setattr(ledger, "_event_hold", lambda event: world.held.append(event) or False)

    async def joined_provider():
        try:
            await world.release.wait()
        finally:
            cost_accumulator.report_charge("fake-echo-1", 0.25)

    async def handler(payload):
        world.tail = request_tail._tail.get()
        world.accumulator = cost_accumulator.current()
        if world.marked:
            assert request_work.mark_mirrored()
        cost_accumulator.report_embed("fake-ngram-1024", 13)
        cost_accumulator.report_llm("fake-echo-1", 17, 19)
        cost_accumulator.report_charge("fake-echo-1", 0.125)
        if world.join_child:
            world.child = asyncio.create_task(joined_provider())
            await asyncio.sleep(0)
        if world.stage == "post_rows":
            return Output()
        world.ready.set()
        try:
            if world.control is not None:
                raise world.control
            await world.release.wait()
        finally:
            if world.child is not None:
                world.child.cancel()
                await asyncio.gather(world.child, return_exceptions=True)
        return Output()

    op = Operation(
        name="memory.get",
        input_model=Input,
        output_model=Output,
        handler=handler,
        idempotent=False,
        required_role=Role.VIEWER,
    )

    async def none(*args, **kwargs):
        return None

    async def admitted(*args, **kwargs):
        request_work._current.get().admitted = ("org-a", "team-a")
        return target

    async def traffic(*args, **kwargs):
        return "interactive", "default"

    async def post(*args, **kwargs):
        if world.stage == "post_rows":
            world.ready.set()
            await world.release.wait()

    async def consent(*args, **kwargs):
        return "off"

    monkeypatch.setattr(gates, "policy", none)
    monkeypatch.setattr(gates, "traffic_class", traffic)
    monkeypatch.setattr(gates, "rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(gates, "tenant_slot", lambda *a, **k: nullcontext())
    monkeypatch.setattr("server.observability.capture_consent.resolve", consent)
    monkeypatch.setattr("server.observability.otel.set_loom_attributes", lambda **kwargs: None)
    monkeypatch.setattr(http, "_extract_auth", lambda request: auth)
    monkeypatch.setattr(http, "_authorize", admitted)
    monkeypatch.setattr(http, "post_handler_rows", post)
    monkeypatch.setattr(post_rows, "post_handler_rows", post)
    monkeypatch.setattr(mcp_call, "_caller_defaults", none)
    monkeypatch.setattr(mcp_call, "_another_cells_org", none)
    monkeypatch.setattr(mcp_call, "_authorize_call", admitted)
    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, op.mcp_tool_name, op)

    async def invoke():
        # The exception crosses this observer before any test-owned final settlement.
        try:
            if transport == "http":
                body = json.dumps(
                    {
                        "agent": "reader",
                        "session_id": "session-a",
                        "run_id": "explicit-run",
                        "query": "private query nonce",
                    }
                ).encode()
                scope = {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/memory.get",
                    "headers": [(b"x-loom-run-id", b"ambient-run")],
                    "query_string": b"",
                }

                async def receive():
                    return {"type": "http.request", "body": body, "more_body": False}

                with private_work() if world.private else nullcontext():
                    return await http._make_endpoint(op)(Request(scope, receive), Response())

            # begin_call resets the ambient run; bind its ordinary admitted run leaf.
            async def bind_run(*args, **kwargs):
                set_ledger_run("ambient-run")

            monkeypatch.setattr(mcp_call, "_bind_session_run", bind_run)
            with private_work() if world.private else nullcontext():
                return await mcp_call._call_tool(
                    1,
                    {
                        "name": op.mcp_tool_name,
                        "arguments": {
                            "agent": "reader",
                            "session_id": "session-a",
                            "run_id": "explicit-run",
                            "query": "private query nonce",
                        },
                    },
                    auth=auth,
                    mcp_session_id="transport-session",
                )
        except BaseException as error:
            world.primary = error
            raise

    world.invoke = invoke
    return world


def assert_row(world, transport, *, success=False, joined=False):
    assert len(world.rows) == 1 and world.client.insert.await_count == 1
    assert world.queued == world.held == []
    row = world.rows[0]
    assert "ts" not in row and row["mirror_marked"] == 1
    assert {k: row[k] for k in ("org", "namespace", "agent", "user_id", "session_id")} == {
        "org": "org-a",
        "namespace": "team-a",
        "agent": "reader",
        "user_id": "reader-user",
        "session_id": "session-a",
    }
    assert row["subject"] == "verified-reader" and row["run_id"] == "explicit-run"
    assert row["operation"] == "memory.get" and row["transport"] == transport
    assert row["request_id"] and row["latency_ms"] >= 0
    assert row["success"] is success
    assert row["error_code"] == ("" if success else "REQUEST_INTERRUPTED")
    assert row["upstream_cost_usd"] == world.accumulator.cost_usd == (0.375 if joined else 0.125)
    assert (
        row["embedding_tokens"],
        row["input_tokens"],
        row["output_tokens"],
        row["llm_calls"],
    ) == (13, 17, 19, 3 if joined else 2)
    assert row["model"] == "fake-echo-1"
    assert "private query nonce" not in json.dumps(
        row
    ) and "private answer nonce" not in json.dumps(row)
    if not success:
        assert "error" not in row["args"] and "error_class" not in row["args"]
