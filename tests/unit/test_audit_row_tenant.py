"""An op's audit row names the tenant the gate authorized, on both transports.

code.* inputs default `org` / `namespace` to "" and the transports authorize
them against the deployment's code plane (`plane_org_for` /
`plane_namespace_for`), which is also what the handler writes. The audit row
names that pair, not the payload's empty strings (HTTP: "", MCP: "-"): an
admin's `code.reindex` or `code.refresh` under no org would be a row no
org-scoped audit read returns.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.transport import gates


@pytest.fixture
def code_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_org", "acme")
    monkeypatch.setattr(settings, "code_namespace", "code-acme")


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def _audit(**kw: Any) -> None:
        rows.append(kw)

    monkeypatch.setattr("server.transport._common.emit_op_audit", _audit)
    return rows


def _http_rows(op: Any, payload: Any, identity: dict[str, str]) -> None:
    """The rows `http._finish_success` writes for an admitted call whose
    `call.identity` is `identity` (what `_authorize` leaves)."""
    from server.transport import post_rows

    asyncio.run(
        post_rows.post_handler_rows(
            op,
            transport="http",
            request_id="r",
            org=identity["org"],
            namespace=identity["namespace"],
            agent=identity["agent"],
            actor="anonymous",
            input_obj=payload,
            elapsed_ms=0,
            response_body={},
            idem=(None, None),
            replay_headers={},
        )
    )


def test_http_audit_row_takes_the_plane_the_gate_authorized(
    code_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _capture(monkeypatch)
    op = OPERATIONS_BY_NAME["code.reindex"]
    payload = op.input_model(git_head="a" * 40)  # org / namespace omitted: ""
    target = gates.authz_target(op, org=payload.org, namespace=payload.namespace, auth=None)
    identity = {"org": target.org or "-", "namespace": target.register_namespace or "-"}
    _http_rows(op, payload, {**identity, "agent": "-"})
    assert [(r["org"], r["namespace"]) for r in rows] == [("acme", "code-acme")]


def test_http_audit_row_keeps_the_payload_tenant_when_named(
    code_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _capture(monkeypatch)
    op = OPERATIONS_BY_NAME["memory.delete"]
    payload = op.input_model(org="o", namespace="n", agent="a", memory_key="k")
    _http_rows(op, payload, {"org": "o", "namespace": "n", "agent": "a"})
    assert [(r["org"], r["namespace"]) for r in rows] == [("o", "n")]


def test_mcp_audit_row_takes_the_plane_the_gate_authorized(
    code_plane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.transport import gates, mcp_call, mcp_outcome, post_rows

    seen: list[tuple[str, str]] = []

    async def _rows(op: Any, **kw: Any) -> None:
        seen.append((kw["org"], kw["namespace"]))

    monkeypatch.setattr(post_rows, "post_handler_rows", _rows)
    op = OPERATIONS_BY_NAME["code.reindex"]
    inp = op.input_model(git_head="a" * 40)
    target = gates.authz_target(op, org=inp.org, namespace=inp.namespace, auth=None)
    call = mcp_call._AdmittedCall(
        1, op, inp, {}, None, "r", "", target.register_namespace or "", target.org or "", target
    )
    out = op.output_model(previous_head="", new_head="a" * 40)
    asyncio.run(mcp_outcome._success_response(call, out, 0, ("", None), None))
    assert seen == [("acme", "code-acme")]
