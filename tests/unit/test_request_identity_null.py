"""A null identity field in a request body is absent, never the text "None".

Reading the body's identity with `str(body.get(k, "-"))` would turn a JSON
`null` — which the ops whose `namespace` is optional (cost.*, audit.list,
agents.connected, ops.*, …) and every optional `user_id` / `session_id`
validate — into the string "None": the authorization target, the rate
limiter's namespace bucket and the ledger row would all name a namespace
called "None", while MCP (and the handler) read it as absent.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.transport._common import request_identity


def test_null_and_absent_fields_are_the_sentinel() -> None:
    got = request_identity({"org": "o", "namespace": None, "user_id": "", "agent": 3})
    assert got == {"org": "o", "namespace": "-", "agent": "3", "user_id": "", "session_id": "-"}


class _In(BaseModel):
    org: str
    namespace: str | None = None


class _Out(BaseModel):
    ok: bool = True


def test_http_authorizes_and_records_a_null_namespace_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.transport.http as http_mod
    from server.transport import gates

    async def _handler(inp: Any) -> _Out:
        return _Out()

    op = dataclasses.replace(
        OPERATIONS_BY_NAME["cost.summary"], input_model=_In, output_model=_Out, handler=_handler
    )
    targets: list[Any] = []
    rows: list[dict[str, str]] = []

    def _rate_limit(op: Any, auth: Any, target: Any, traffic: str) -> None:
        targets.append(target)

    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    monkeypatch.setattr(gates, "rate_limit", _rate_limit)
    monkeypatch.setattr(http_mod, "emit_agent_event", lambda **kw: rows.append(kw["identity"]))
    app = FastAPI()
    app.include_router(http_mod.build_router())
    r = TestClient(app).post("/v1/cost.summary", json={"org": "o", "namespace": None})
    assert r.status_code == 200, r.text
    [target] = targets
    assert target.named_namespace is None and target.namespace is None
    assert rows and all(row["namespace"] == "-" for row in rows)
