"""An HTTP idempotency replay is served only to the caller
that stored it, and only after the token and authorization gates.

A replay that ran ahead of `_extract_auth`, keyed by the client's
Idempotency-Key alone, would hand the stored response to anyone who knew or
guessed a key — without a token, or as another tenant.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.auth.bearer as bearer_mod
from server.auth.jwt import AuthInfo, TokenError
from server.core import idempotency
from server.operations.base import Role
from server.transport import gates

BODY = {"org": "o", "namespace": "n", "agent": "a", "memory_key": "k", "reason": "t"}


def _principal(subject: str, org: str = "o") -> AuthInfo:
    return AuthInfo(
        subject=subject, org=org, namespaces=("n",), roles=(Role.WRITER,), is_service_account=False
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, Any], list[str]]:
    from server.operations.registry import ALL_OPERATIONS
    from server.settings import settings
    from server.transport import http as http_mod

    base = next(o for o in ALL_OPERATIONS if o.name == "memory.unlock")
    ran: list[str] = []

    async def _handler(inp: Any) -> Any:
        ran.append(inp.memory_key)
        return base.output_model.model_construct(unlocked=True)

    tokens = {"alice": _principal("alice"), "bob": _principal("bob"), "eve": _principal("eve", "p")}

    def _verify(token: str) -> AuthInfo:
        if token not in tokens:
            raise TokenError("bad token")
        return tokens[token]

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    rows: dict[str, tuple[str, Any]] = {}

    async def _check(key: str, body_hash: str) -> idempotency.CacheResult:
        row = rows.get(key)
        if row is None:
            return idempotency.CacheResult(hit=None, conflict=False)
        if row[0] != body_hash:
            return idempotency.CacheResult(hit=None, conflict=True)
        hit = idempotency.CacheHit(status_code=200, body=row[1], headers={})
        return idempotency.CacheResult(hit=hit, conflict=False)

    async def _store(*, idempotency_key: str, body_hash: str, response_body: Any, **_: Any) -> None:
        rows[idempotency_key] = (body_hash, response_body)

    op = dataclasses.replace(base, handler=_handler)
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(bearer_mod, "verify_token", _verify)

    async def _admit(*_a: Any, **_k: Any) -> gates.Target:
        return gates.Target(org="o", namespace="n", named_namespace="n", register_namespace="n")

    monkeypatch.setattr(http_mod, "_authorize", _admit)
    monkeypatch.setattr(http_mod, "_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr("server.transport._common.emit_op_audit", _noop)
    monkeypatch.setattr("server.transport._common.emit_budget_violation", _noop)
    monkeypatch.setattr(idempotency, "check", _check)
    monkeypatch.setattr(idempotency, "store", _store)
    app = FastAPI()
    app.include_router(http_mod.build_router())
    return TestClient(app), rows, ran


def _post(c: TestClient, token: str | None, key: str = "idem-1") -> Any:
    headers = {"Idempotency-Key": key}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return c.post("/v1/memory.unlock", json=BODY, headers=headers)


def test_a_replay_goes_only_to_the_caller_that_stored_it(client) -> None:
    c, rows, ran = client
    first = _post(c, "alice")
    assert first.status_code == 200, first.text
    assert list(rows) == [
        idempotency.scoped_key("idem-1", org="o", subject="alice", operation="memory.unlock")
    ]
    again = _post(c, "alice")
    assert again.headers.get("X-Idempotent-Replay") == "true" and ran == ["k"]


def test_an_unauthenticated_request_with_a_known_key_gets_401(client) -> None:
    c, _, ran = client
    _post(c, "alice")
    for token in (None, "forged"):
        r = _post(c, token)
        assert r.status_code == 401, r.text
        assert "X-Idempotent-Replay" not in r.headers
        assert "unlocked" not in r.text
    assert ran == ["k"]


def test_another_principal_or_tenant_with_the_same_key_gets_no_replay(client) -> None:
    c, rows, ran = client
    _post(c, "alice")
    for token in ("bob", "eve"):
        r = _post(c, token)
        assert r.status_code == 200 and "X-Idempotent-Replay" not in r.headers
    assert ran == ["k", "k", "k"] and len(rows) == 3


def test_scoped_keys_differ_by_every_part() -> None:
    k = idempotency.scoped_key("c", org="o", subject="s", operation="op")
    assert len(k) == 64
    assert k != idempotency.scoped_key("c", org="p", subject="s", operation="op")
    assert k != idempotency.scoped_key("c", org="o", subject="t", operation="op")
    assert k != idempotency.scoped_key("c", org="o", subject="s", operation="op2")
    assert k != idempotency.scoped_key("d", org="o", subject="s", operation="op")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # Each pair joins to the same bytes on U+001F: one per boundary.
        (("o\x1fa", "b", "op", "k"), ("o", "a\x1fb", "op", "k")),
        (("o", "s\x1fop", "x", "k"), ("o", "s", "op\x1fx", "k")),
        (("o", "s", "op", "x\x1fk"), ("o", "s", "op\x1fx", "k")),
    ],
)
def test_scopes_that_join_to_the_same_bytes_get_distinct_keys(left, right) -> None:
    """A caller controls its client key (and a service account its
    subject): parts joined on a separator it can send would let two scopes
    share one row."""
    assert "\x1f".join(left) == "\x1f".join(right)
    keys = [
        idempotency.scoped_key(key, org=org, subject=subject, operation=op)
        for org, subject, op, key in (left, right)
    ]
    assert keys[0] != keys[1]
