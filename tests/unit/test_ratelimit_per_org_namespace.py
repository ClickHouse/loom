"""A namespace's rate-limit budget belongs to its own org.

Keyed on the name alone, a namespace bucket would let any verified token from
any org name `namespace="prod"` and drain another tenant's bucket — every one of
its requests refused 403, the tenant's own callers answered 429. Buckets key on
(verified token org, namespace): another org's requests spend that org's
bucket, never the target's.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import server.auth.bearer as bearer_mod
from server.auth.jwt import AuthInfo, TokenError
from server.core.ratelimit import Limit, Limiter
from server.operations.base import Role

BODY = {"org": "o", "namespace": "prod", "agent": "a", "memory_key": "k", "reason": "t"}


def _principal(subject: str, org: str) -> AuthInfo:
    return AuthInfo(
        subject=subject,
        org=org,
        namespaces=("prod",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Limiter, list[str]]:
    from server.operations.registry import ALL_OPERATIONS
    from server.settings import settings
    from server.transport import http as http_mod

    base = next(o for o in ALL_OPERATIONS if o.name == "memory.unlock")

    async def _handler(inp: Any) -> Any:
        return base.output_model.model_construct(unlocked=True)

    tokens = {"alice": _principal("alice", "o"), "eve": _principal("eve", "p")}

    def _verify(token: str) -> AuthInfo:
        if token not in tokens:
            raise TokenError("bad token")
        return tokens[token]

    authorized: list[str] = []

    async def _authorize(op: Any, response: Any, call: Any, payload: Any) -> Any:
        # The gate: a token reaches only its own org.
        from server.transport import gates

        authorized.append(call.auth.subject)
        if call.auth.org != call.identity["org"]:
            raise HTTPException(status_code=403, detail={"code": "AUTH_INSUFFICIENT"})
        return gates.authz_target(
            op, org=call.identity["org"], namespace=call.identity["namespace"], auth=call.auth
        )

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    limiter = Limiter()
    op = dataclasses.replace(base, handler=_handler)
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", [op])
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(bearer_mod, "verify_token", _verify)
    monkeypatch.setattr(http_mod, "_authorize", _authorize)
    import server.core.ratelimit as ratelimit_mod

    monkeypatch.setattr(ratelimit_mod, "get_limiter", lambda: limiter)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr("server.transport._common.emit_op_audit", _noop)
    monkeypatch.setattr("server.transport._common.emit_budget_violation", _noop)
    fastapi_app = FastAPI()
    fastapi_app.include_router(http_mod.build_router())
    return TestClient(fastapi_app), limiter, authorized


def _post(c: TestClient, token: str) -> Any:
    return c.post("/v1/memory.unlock", json=BODY, headers={"Authorization": f"Bearer {token}"})


def test_another_orgs_requests_never_spend_the_target_namespace_bucket(app) -> None:
    c, limiter, _ = app
    limiter.set_namespace_limit("prod", "memory", Limit(capacity=2, rate=0))
    for _ in range(10):  # another org's token names o/prod: refused, from its own bucket
        assert _post(c, "eve").status_code in (403, 429)
    for _ in range(2):  # o/prod's own caller still has its whole budget
        r = _post(c, "alice")
        assert r.status_code == 200, r.text
    r = _post(c, "alice")
    assert r.status_code == 429 and r.json()["detail"]["code"] == "RATE_LIMITED"
    assert int(r.headers["Retry-After"]) >= 1


def test_two_orgs_same_namespace_name_have_separate_buckets() -> None:
    lim = Limiter()
    lim.set_namespace_limit("prod", "memory", Limit(capacity=1, rate=0))
    assert lim.check("prod", "memory.set", org="a").allowed
    assert not lim.check("prod", "memory.set", org="a").allowed
    assert lim.check("prod", "memory.set", org="b").allowed


def test_two_orgs_same_namespace_name_have_separate_shared_buckets(
    settings_override, tmp_path: Path
) -> None:
    path = str(tmp_path / "rl.buckets")
    settings_override(ratelimit_shared="on", ratelimit_shared_path=path, ratelimit_shared_slots=256)
    lim = Limiter()
    assert lim.shared_active
    lim.set_namespace_limit("prod", "memory", Limit(capacity=1, rate=0))
    assert lim.check("prod", "memory.set", org="a").allowed
    r = lim.check("prod", "memory.set", org="a")
    assert not r.allowed and r.limit == 1  # the namespace tier refused
    assert lim.check("prod", "memory.set", org="b").allowed
    lim._store().close()  # type: ignore[union-attr]
