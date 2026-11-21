"""An answer cannot discover private tools outside its pre-output admission."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import jwt
import pytest

from server.auth.answer_tokens import ANSWER_JTI_PREFIX
from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo, TokenError, mint_token, verify_token
from server.core import request_work
from server.knowledge.app import source_access
from server.operations.base import Role
from server.settings import settings

_SECRET = "assistant-mirror-admission-unit-key"


def _caller(**kwargs: Any) -> AuthInfo:
    return AuthInfo(
        **{
            "subject": "reader",
            "org": "example",
            "namespaces": ("notes",),
            "roles": (Role.VIEWER,),
            "is_service_account": False,
            "token_id": f"{ANSWER_JTI_PREFIX}answer",
            **kwargs,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        {"consumer": "loom-assistant", "token_id": "legacy"},
        {},
        {"token_id": "child", "parents": (f"{ANSWER_JTI_PREFIX}answer",)},
    ],
)
@pytest.mark.parametrize("protected", [False, True])
async def test_an_answer_without_admission_never_discovers_a_mirror(
    monkeypatch: pytest.MonkeyPatch, identity: dict[str, Any], protected: bool
) -> None:
    seen: list[str] = []

    async def authorized(*args: Any) -> None:
        seen.append("authorized")

    async def active(*args: Any) -> list[dict[str, Any]]:
        seen.append("discovered")
        return [{"namespace": "notes", "source_id": "source"}]

    async def prepare(*args: Any) -> None:
        seen.append("private")

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(source_access, "check_authorized", authorized)
    monkeypatch.setattr(source_access.store, "active_sources", active)
    monkeypatch.setattr(source_access, "_prepare", prepare)
    auth = _caller(**identity, session_protection=protected)
    token = current_auth_var.set(auth)
    try:
        request_work.begin()
        plan = await source_access.prepare("example", ["notes"])
    finally:
        current_auth_var.reset(token)
    assert not plan.marked
    assert not plan.sources
    assert seen == []


@pytest.mark.parametrize("admitted", [False, True])
def test_answer_admission_survives_signature_verification(admitted: bool) -> None:
    token = mint_token(
        "reader",
        "example",
        ["notes"],
        ["viewer"],
        secret=_SECRET,
        answer_mirrors=admitted,
    )
    assert verify_token(token, secret=_SECRET).answer_mirrors is admitted


@pytest.mark.asyncio
async def test_positive_admission_still_requires_the_fixed_caller_promise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected(*args: Any) -> Any:
        pytest.fail("an undeclared answer queried mirrored registrations")

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(source_access, "check_authorized", unexpected)
    monkeypatch.setattr(source_access.store, "active_sources", unexpected)
    token = current_auth_var.set(replace(_caller(), answer_mirrors=True))
    try:
        request_work.begin()
        assert not (await source_access.prepare("example", ["notes"])).marked
    finally:
        current_auth_var.reset(token)


@pytest.mark.parametrize("claim", ["true", "false", 0, 1, None, [], {}])
def test_non_boolean_answer_admission_is_refused(claim: Any) -> None:
    with pytest.raises(TokenError, match="must be a boolean"):
        mint_token("reader", "example", ["notes"], ["viewer"], secret=_SECRET, answer_mirrors=claim)
    signed = mint_token("reader", "example", ["notes"], ["viewer"], secret=_SECRET)
    payload = jwt.decode(signed, options={"verify_signature": False})
    payload["answer_mirrors"] = claim
    signed = jwt.encode(payload, _SECRET, algorithm="HS256", headers={"kid": "override"})
    with pytest.raises(TokenError, match="must be a boolean"):
        verify_token(signed, secret=_SECRET)


@pytest.mark.asyncio
async def test_an_admitted_protected_answer_keeps_the_checked_mirror_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    async def authorized(*args: Any) -> None:
        seen.append("authorized")

    async def active(*args: Any) -> list[dict[str, str]]:
        seen.append("discovered")
        return [{"namespace": "notes", "source_id": "source"}]

    async def prepare(*args: Any) -> None:
        assert request_work.mirror_marked()
        seen.append("checked")

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(source_access, "check_authorized", authorized)
    monkeypatch.setattr(source_access.store, "active_sources", active)
    monkeypatch.setattr(source_access, "_prepare", prepare)
    token = current_auth_var.set(_caller(session_protection=True, answer_mirrors=True))
    try:
        bag = request_work.begin()
        plan = await source_access.prepare("example", ["notes"])
    finally:
        current_auth_var.reset(token)
    assert plan.marked and bag.mirror_marked
    assert seen == ["authorized", "discovered", "checked"]


@pytest.mark.parametrize("status", [403, 503, 500])
def test_mcp_failures_keep_the_mark_after_protocol_unwrapping(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    import json

    from server.core.diagnostics import private_work
    from server.core.errors import AuthInsufficientError, BackendUnavailableError
    from server.transport import mcp_outcome

    error = {
        403: AuthInsufficientError("refused"),
        503: BackendUnavailableError("unavailable"),
        500: RuntimeError("private detail"),
    }[status]
    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", lambda *a, **kw: None)
    with request_work.bind(request_work.RequestWork()):
        request_work.mark_mirrored()
        with private_work():
            result = mcp_outcome._handler_failed(
                error,
                1,
                "loom_assistant_ask",
                SimpleNamespace(name="assistant.ask", idempotent=False),
                {},
                None,
                _caller(),
                "request",
                "session",
                rl_ns="notes",
                rl_org="example",
                elapsed_ms=1,
            )
    assert next(iter(result)) == "mirrored" and result["mirrored"] is True
    if status == 403:
        body = result["result"]
        assert next(iter(body["structuredContent"])) == "mirrored"
        assert json.loads(body["content"][0]["text"])["mirrored"] is True
    else:
        assert next(iter(result["error"]["data"])) == "mirrored"


@pytest.mark.parametrize("name", ["knowledge.get", "knowledge.search"])
@pytest.mark.parametrize("marked", [False, True])
def test_http_success_serialization_retains_the_server_owned_mark(name, marked):
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import arg_check

    op = OPERATIONS_BY_NAME[name]
    result = (
        op.output_model(found=True, source_uri="example", content="private")
        if name == "knowledge.get"
        else op.output_model(results=[], embedding_model="fake")
    )
    with request_work.bind(request_work.RequestWork()):
        if marked:
            request_work.mark_mirrored()
        body = arg_check.response_body(op, {}, result)
    if marked:
        assert next(iter(body)) == "mirrored" and body["mirrored"] is True
        assert {k: v for k, v in body.items() if k != "mirrored"} == result.model_dump(mode="json")
    else:
        assert body == result.model_dump(mode="json")


@pytest.mark.parametrize("name", ["knowledge.get", "search.query"])
@pytest.mark.parametrize("marked", [False, True])
async def test_mcp_success_unwrapping_keeps_the_mark(monkeypatch, name, marked):
    import json

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_outcome, post_rows

    op = OPERATIONS_BY_NAME[name]
    result = (
        op.output_model(found=True, source_uri="example", content="private")
        if name == "knowledge.get"
        else op.output_model()
    )
    rows = []

    async def record_rows(op, **kwargs):
        rows.append(kwargs["response_body"])

    monkeypatch.setattr(post_rows, "post_handler_rows", record_rows)
    call = SimpleNamespace(
        req_id=1,
        op=op,
        input_obj=SimpleNamespace(agent="example"),
        arguments={},
        auth=None,
        request_id="request",
        rl_ns="notes",
        rl_org="example",
        mcp_session_id="session",
    )
    with request_work.bind(request_work.RequestWork()):
        if marked:
            request_work.mark_mirrored()
        reply = await mcp_outcome._success_response(call, result, 1, (None, None), None)
    body = reply["result"]
    if marked:
        for envelope in [reply, body, body["structuredContent"], rows[0]]:
            assert next(iter(envelope)) == "mirrored" and envelope["mirrored"] is True
        assert json.loads(body["content"][0]["text"])["mirrored"] is True
    else:
        assert all(
            "mirrored" not in envelope
            for envelope in [reply, body, body["structuredContent"], rows[0]]
        )
    if name == "knowledge.get":
        assert body["structuredContent"]["content"] == "private"
        assert json.loads(body["content"][0]["text"])["content"] == "private"
    else:
        assert "loom_search" in body["content"][-1]["text"]
