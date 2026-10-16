"""MCP `tools/call` idempotency: replay, conflict and fail-open, end to end.

MCP has no Idempotency-Key header, so a caller passes `_idempotency_key` in
the tool arguments. These tests drive `_call_tool` with the idempotency store
stubbed by an in-memory dict that keeps `server.core.idempotency`'s check/store
contract, and pin the three outcomes a caller relies on:

  (a) the same key + same arguments replays the stored response and the
      handler does NOT run again;
  (b) the same key + different arguments answers IDEMPOTENT_CONFLICT and the
      handler does not run;
  (c) a failed cache read fails OPEN — the call runs uncached;
  (d) the stored answer belongs to its caller: another
      principal with the same key and arguments runs the handler, and a
      caller the role gate refuses gets AUTH_INSUFFICIENT, never the replay.

The early returns of the idempotency gate are what (a) and (b) exercise; a
refactor that loses one (or returns a malformed tuple) makes them fail.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

import server.core.idempotency as idempotency
import server.transport.mcp_call as mcp_call
from server.auth.jwt import AuthInfo
from server.operations.base import Operation, Role

TOOL = "loom_test_idempotency_probe"
# The module's own read, which the fixture swaps for the in-memory store.
_REAL_CHECK = idempotency.check


class _In(BaseModel):
    text: str = ""


class _Out(BaseModel):
    echoed: str = ""
    calls: int = 0


class _Store:
    """In-memory stand-in for loom.idempotency with the module's semantics:
    a stored key with a different body hash is a conflict, the same hash a
    hit carrying the stored body."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, Any]] = {}

    async def check(self, idempotency_key: str, body_hash: str) -> idempotency.CacheResult:
        row = self.rows.get(idempotency_key)
        if row is None:
            return idempotency.CacheResult(hit=None, conflict=False)
        stored_hash, body = row
        if stored_hash != body_hash:
            return idempotency.CacheResult(hit=None, conflict=True)
        hit = idempotency.CacheHit(status_code=200, body=body, headers={})
        return idempotency.CacheResult(hit=hit, conflict=False)

    async def store(
        self,
        idempotency_key: str,
        body_hash: str,
        operation: str,
        status_code: int,
        response_body: Any,
        response_headers: dict[str, str] | None = None,
        *,
        org: str,
        subject: str,
    ) -> None:
        self.rows[idempotency_key] = (body_hash, response_body)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> tuple[_Store, list[str]]:
    ran: list[str] = []

    async def _handler(inp: Any) -> _Out:
        ran.append(inp.text)
        return _Out(echoed=inp.text, calls=len(ran))

    op = Operation(
        name="test.idempotency_probe",
        input_model=_In,
        output_model=_Out,
        handler=_handler,
        idempotent=True,
        budget_p99_ms=10_000,
        required_role=Role.VIEWER,
        writes=True,  # a read-only op keeps no replay
    )
    monkeypatch.setitem(mcp_call._TOOLS_BY_NAME, TOOL, op)

    async def _no_defaults(auth: Any, client_name: str | None = None) -> None:
        return None

    monkeypatch.setattr(mcp_call, "_caller_defaults", _no_defaults)
    store = _Store()
    monkeypatch.setattr(idempotency, "check", store.check)
    monkeypatch.setattr(idempotency, "store", store.store)
    return store, ran


def _call(arguments: dict[str, Any], auth: Any = None) -> dict[str, Any]:
    params = {"name": TOOL, "arguments": dict(arguments)}
    return asyncio.run(mcp_call._call_tool(1, params, auth=auth))


def _principal(subject: str, org: str = "o") -> AuthInfo:
    return AuthInfo(
        subject=subject, org=org, namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )


@pytest.fixture
def gated(monkeypatch: pytest.MonkeyPatch, probe: tuple[_Store, list[str]]):
    """The role gate admits every principal but "intruder"; no admission I/O."""
    import server.code.app.member_writes as member_writes

    async def _authorize_op(auth: Any, op: Any, **_k: Any) -> None:
        if auth.subject == "intruder":
            raise PermissionError("not a member")

    async def _admitted(*_a: Any, **_k: Any) -> None:
        return None

    async def _no_refusal(call: Any) -> None:
        return None

    monkeypatch.setattr(member_writes, "authorize_op", _authorize_op)
    monkeypatch.setattr("server.transport.gates._admitted", _admitted)
    monkeypatch.setattr(mcp_call, "_admission_refusal", _no_refusal)
    return probe


def test_same_key_same_arguments_replays_without_rerunning_the_handler(
    probe: tuple[_Store, list[str]],
) -> None:
    store, ran = probe
    first = _call({"text": "hello", "_idempotency_key": "k-1"})
    assert first["result"]["isError"] is False
    assert first["result"]["structuredContent"]["echoed"] == "hello"
    scoped = idempotency.scoped_key("k-1", org="", subject="", operation="test.idempotency_probe")
    assert ran == ["hello"] and list(store.rows) == [scoped], "stored under the caller's scope"

    again = _call({"text": "hello", "_idempotency_key": "k-1"})
    result = again["result"]
    assert ran == ["hello"], "a replay never calls the handler a second time"
    assert result["isError"] is False
    assert result["_meta"] == {"idempotentReplay": True}
    assert result["structuredContent"]["echoed"] == "hello"
    assert result["structuredContent"]["calls"] == 1, "the stored response, not a new one"


def test_the_legacy_arg_name_replays_too(probe: tuple[_Store, list[str]]) -> None:
    _, ran = probe
    _call({"text": "x", "idempotency_key": "k-legacy"})
    again = _call({"text": "x", "idempotency_key": "k-legacy"})
    assert ran == ["x"] and again["result"]["_meta"] == {"idempotentReplay": True}


def test_same_key_different_arguments_is_a_conflict(probe: tuple[_Store, list[str]]) -> None:
    _, ran = probe
    _call({"text": "hello", "_idempotency_key": "k-2"})
    clash = _call({"text": "goodbye", "_idempotency_key": "k-2"})
    msg = "Idempotency key reused with different tool arguments."
    assert clash["result"] == {
        "content": [{"type": "text", "text": msg}],
        "isError": True,
        "structuredContent": {"error": msg, "code": "IDEMPOTENT_CONFLICT"},
    }
    assert ran == ["hello"], "a conflict never runs the handler"


def test_a_failed_cache_read_fails_open_and_runs_the_handler(
    probe: tuple[_Store, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, ran = probe

    class _Down:
        async def query_one(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("idempotency store unreachable")

    monkeypatch.setattr(idempotency, "check", _REAL_CHECK)
    monkeypatch.setattr(idempotency, "get_client", lambda: _Down())
    out = _call({"text": "uncached", "_idempotency_key": "k-3"})
    assert ran == ["uncached"], "the idempotency cache failing never blocks the call"
    assert out["result"]["isError"] is False
    assert "_meta" not in out["result"]
    assert out["result"]["structuredContent"]["echoed"] == "uncached"


def test_another_principal_with_the_same_key_gets_no_replay(gated) -> None:
    store, ran = gated
    _call({"text": "hello", "_idempotency_key": "k-4"}, auth=_principal("alice"))
    theirs = _call({"text": "hello", "_idempotency_key": "k-4"}, auth=_principal("bob"))
    other_org = _call({"text": "hello", "_idempotency_key": "k-4"}, auth=_principal("alice", "p"))
    assert ran == ["hello"] * 3, "each caller's call runs its own handler"
    assert "_meta" not in theirs["result"] and "_meta" not in other_org["result"]
    assert len(store.rows) == 3
    mine = _call({"text": "hello", "_idempotency_key": "k-4"}, auth=_principal("alice"))
    assert mine["result"]["_meta"] == {"idempotentReplay": True} and len(ran) == 3


def test_a_refused_caller_gets_forbidden_not_the_replay(gated) -> None:
    _, ran = gated
    _call({"text": "secret", "_idempotency_key": "k-5"}, auth=_principal("alice"))
    out = _call({"text": "secret", "_idempotency_key": "k-5"}, auth=_principal("intruder"))
    assert ran == ["secret"]
    assert out["result"]["isError"] is True and "_meta" not in out["result"]
    assert "secret" not in str(out["result"]).replace("k-5", "")
