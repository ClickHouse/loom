"""Transport promises stay request-local and cannot upgrade an assistant caller."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import jwt
import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from server.assistant.app import bridge
from server.auth.answer_tokens import ANSWER_JTI_PREFIX
from server.auth.delegation import Delegated
from server.auth.jwt import AuthInfo, TokenError, mint_token, verify_token
from server.auth.session_protection import protected_for, request_declaration
from server.operations.base import Role
from server.transport.request_scope import HTTPApplication

_SECRET = "session-protection-unit-signing-key-unchanged"
_HEADER = (b"x-loom-session-protection", b"v1")


def _auth(**kwargs: Any) -> AuthInfo:
    return AuthInfo(
        **{
            "subject": "reader",
            "org": "example",
            "namespaces": ("notes",),
            "roles": (Role.VIEWER,),
            "is_service_account": False,
            "token_id": "caller",
            **kwargs,
        }
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([], False),
        ([_HEADER], True),
        ([(b"X-Loom-Session-Protection", b"v1")], True),
        ([(b"x-loom-session-protection", b"true")], False),
        ([(b"x-loom-session-protection", b"v2")], False),
        ([(b"x-loom-session-protection", b"v1, v1")], False),
        ([_HEADER, _HEADER], False),
        ([_HEADER, (b"x-loom-session-protection", b"false")], False),
        ([(b"user-agent", b"loom-protected-v1")], False),
    ],
)
def test_only_one_versioned_transport_promise_is_accepted(
    headers: list[tuple[bytes, bytes]], expected: bool
) -> None:
    assert not protected_for(None)
    with request_declaration({"headers": headers}):
        assert protected_for(_auth()) is expected
    assert not protected_for(None)


@pytest.mark.parametrize(
    "identity",
    [
        {"consumer": "loom-assistant"},
        {"token_id": f"{ANSWER_JTI_PREFIX}bounded"},
        {"parents": ("root", f"{ANSWER_JTI_PREFIX}bounded")},
    ],
)
@pytest.mark.parametrize("caller_protected", [False, True])
def test_an_answer_and_its_descendants_use_only_the_fixed_claim(
    identity: dict[str, Any], caller_protected: bool
) -> None:
    auth = _auth(**identity, session_protection=caller_protected)
    for headers in ([], [_HEADER]):
        with request_declaration({"headers": headers}):
            assert protected_for(auth) is caller_protected


@pytest.mark.asyncio
async def test_concurrent_requests_and_child_tasks_keep_their_own_promise() -> None:
    arrived = asyncio.Event()
    release = asyncio.Event()

    async def declared() -> bool:
        with request_declaration({"headers": [_HEADER]}):
            arrived.set()
            await release.wait()
            return await asyncio.create_task(_read())

    async def _read() -> bool:
        await asyncio.sleep(0)
        return protected_for(None)

    task = asyncio.create_task(declared())
    await arrived.wait()
    with request_declaration({"headers": []}):
        assert not await asyncio.create_task(_read())
        release.set()
        assert await task
        assert not protected_for(None)
    assert not protected_for(None)


def test_nested_or_failed_requests_restore_the_previous_promise() -> None:
    with request_declaration({"headers": [_HEADER]}):
        with pytest.raises(RuntimeError), request_declaration({"headers": []}):
            assert not protected_for(None)
            raise RuntimeError("interrupted")
        assert protected_for(None)
    assert not protected_for(None)


def test_the_http_owner_keeps_the_promise_through_middleware_and_streaming() -> None:
    app = HTTPApplication()

    @app.middleware("http")
    async def middleware(request: Request, call_next: Any) -> Any:
        return await call_next(request)

    @app.get("/promise")
    async def promise() -> StreamingResponse:
        async def chunks() -> Any:
            await asyncio.sleep(0)
            yield b"protected" if protected_for(None) else b"unknown"

        return StreamingResponse(chunks())

    with TestClient(app) as client:
        assert (
            client.get("/promise", headers={"X-Loom-Session-Protection": "v1"}).text == "protected"
        )
        assert client.get("/promise").text == "unknown"
    assert not protected_for(None)


@pytest.mark.parametrize("declared", [False, True])
def test_the_signed_promise_survives_token_verification(declared: bool) -> None:
    token = mint_token(
        "reader",
        "example",
        ["notes"],
        ["viewer"],
        secret=_SECRET,
        session_protection=declared,
    )
    assert verify_token(token, secret=_SECRET).session_protection is declared


@pytest.mark.parametrize("claim", ["false", "true", 0, 1, None, [], {}])
def test_a_signed_non_boolean_promise_is_refused(claim: Any) -> None:
    with pytest.raises(TokenError, match="must be a boolean"):
        mint_token(
            "reader", "example", ["notes"], ["viewer"], secret=_SECRET, session_protection=claim
        )
    token = mint_token("reader", "example", ["notes"], ["viewer"], secret=_SECRET)
    payload = jwt.decode(token, options={"verify_signature": False})
    payload["session_protection"] = claim
    invalid = jwt.encode(payload, _SECRET, algorithm="HS256", headers={"kid": "override"})
    with pytest.raises(TokenError, match="must be a boolean"):
        verify_token(invalid, secret=_SECRET)


@pytest.mark.parametrize("caller_protected", [False, True])
@pytest.mark.parametrize("mirrored", [False, True])
@pytest.mark.asyncio
async def test_the_bridge_fixes_the_callers_declaration_in_the_answer(
    monkeypatch: pytest.MonkeyPatch, caller_protected: bool, mirrored: bool
) -> None:
    parent = _auth()

    async def delegate(auth: AuthInfo, **kwargs: Any) -> Delegated:
        return Delegated(
            org="example",
            namespaces=["notes"],
            roles=["viewer"],
            ns_roles=None,
            write_namespaces=None,
            read_scope="",
            read_user_id="",
            ttl_seconds=60,
            parents=[*auth.parents, auth.token_id],
            parent_subjects=[*auth.parent_subjects, auth.subject],
            parent_iats=[*auth.parent_iats, auth.issued_at or 0],
            creator="reader",
            is_service_account=False,
        )

    monkeypatch.setattr(bridge, "delegate", delegate)
    with request_declaration({"headers": [_HEADER] if caller_protected else []}):
        token = await bridge.answer_token(parent, "example", "notes", mirrored=mirrored)
    answer = verify_token(token)
    assert answer.session_protection is caller_protected
    assert answer.answer_mirrors is (caller_protected and mirrored)
    with request_declaration({"headers": [] if caller_protected else [_HEADER]}):
        assert protected_for(answer) is caller_protected
        child = verify_token((await delegate(answer)).mint("sa:child"))
        assert not child.session_protection
        assert not child.answer_mirrors
        assert not protected_for(child)
        # A forged local field is not inherited by ordinary delegation either.
        assert not verify_token(
            (await delegate(replace(answer, session_protection=True))).mint("sa:other-child")
        ).session_protection
