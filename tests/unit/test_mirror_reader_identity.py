"""Only a sign-in or an explicitly minted personal token names a mirror reader."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import jwt
import pytest
from fastapi import HTTPException

from server.auth import delegation
from server.auth import jwt as tokens
from server.identity.app import me_service_accounts as me
from server.settings import settings
from tests.unit.test_token_mint import _caller, saved
from tests.unit.test_token_mint import grants as grants


@pytest.fixture(autouse=True)
def auth_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)


@pytest.mark.parametrize(
    ("claims", "subject"),
    [
        ({"session_id": "sid"}, "alice"),
        ({"session_id": "sid", "first_party": True}, "alice"),
        ({}, ""),
        ({"is_service_account": True}, ""),
        ({"creator": "alice"}, ""),
        ({"creator": "alice", "session_id": "sid"}, ""),
        ({"is_service_account": True, "session_id": "sid"}, ""),
        ({"creator": "alice", "reads_as_creator": True, "is_service_account": True}, "alice"),
        ({"reads_as_creator": True}, ""),
        ({"session_id": "sid", "impersonated_by": "bob"}, ""),
        ({"creator": "alice", "reads_as_creator": True, "impersonated_by": "bob"}, ""),
        ({"session_id": "sid", "agent_principal": "agent:bot"}, ""),
        ({"session_id": "sid", "read_user_id": "end-user"}, ""),
    ],
)
def test_reader_subject_matrix(claims: dict[str, Any], subject: str) -> None:
    from server.auth.reader_identity import reader_subject

    assert reader_subject(_caller(**claims)) == subject
    assert reader_subject(None) == ""


def test_auth_off_has_no_reader_even_with_a_primary_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.reader_identity import reader_subject

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    assert reader_subject(_caller(session_id="sid")) == ""


@pytest.mark.parametrize("personal", [False, True])
def test_signed_reader_claim_roundtrip(personal: bool) -> None:
    token = tokens.mint_token(
        "sa:personal", "o", ["a"], ["viewer"], creator="alice", reads_as_creator=personal
    )
    assert tokens.verify_token(token).reads_as_creator is personal
    decoded = jwt.decode(token, options={"verify_signature": False})
    assert ("reads_as_creator" in decoded) is personal


@pytest.mark.parametrize("value", ["true", "false", 0, 1, None, [], {}])
def test_malformed_reader_claim_is_refused(value: Any) -> None:
    token = tokens.mint_token("alice", "o", ["a"], ["viewer"])
    decoded = jwt.decode(token, options={"verify_signature": False})
    decoded["reads_as_creator"] = value
    from server.auth import keyring

    key = keyring.signing_key()
    forged = jwt.encode(decoded, key.secret, algorithm=key.algorithm, headers={"kid": key.kid})
    with pytest.raises(tokens.TokenError, match="reads_as_creator"):
        tokens.verify_token(forged)


async def _mint(
    monkeypatch: pytest.MonkeyPatch, caller: tokens.AuthInfo, *, personal: bool, **payload: Any
) -> dict[str, Any]:
    monkeypatch.setattr("server.auth.bearer.verify_bearer", AsyncMock(return_value=caller))
    request: Any = SimpleNamespace(headers={"Authorization": "Bearer t"}, state=SimpleNamespace())
    route = me.me_personal_tokens_create if personal else me.me_service_accounts_create
    return await route(request, {"name": "personal-agent", "namespaces": ["a"], **payload})


async def test_personal_mint_is_listed_and_never_wider_than_its_session(
    monkeypatch: pytest.MonkeyPatch, grants: Any
) -> None:
    parent = _caller(session_id="sid", first_party=True, roles=(), ns_roles=(("a", "viewer"),))
    response = await _mint(monkeypatch, parent, personal=True, creator="bob", org="elsewhere")
    token = tokens.verify_token(response["token"])
    assert token.reads_as_creator and token.creator == "alice" and token.org == "o"
    assert token.ns_roles == (("a", "viewer"),)
    assert saved[0]["kind"] == "personal_agent" and saved[0]["created_by"] == "alice"
    assert saved[0]["sa_subject"] == token.subject
    assert saved[0]["inherit_creator_grants"]


async def test_service_account_mint_never_takes_a_reader_claim_from_the_body(
    monkeypatch: pytest.MonkeyPatch, grants: Any
) -> None:
    response = await _mint(
        monkeypatch,
        _caller(session_id="sid", first_party=True),
        personal=False,
        reads_as_creator=True,
    )
    token = tokens.verify_token(response["token"])
    assert not token.reads_as_creator
    assert saved[0]["kind"] == "service_account"


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"session_id": "sid", "first_party": False},
        {"session_id": "sid", "first_party": True, "is_service_account": True},
        {"session_id": "sid", "first_party": True, "creator": "alice"},
        {"session_id": "sid", "first_party": True, "impersonated_by": "bob"},
        {"session_id": "sid", "first_party": True, "agent_principal": "agent:bot"},
        {"session_id": "sid", "first_party": True, "read_user_id": "end-user"},
    ],
)
async def test_personal_mint_refuses_nonprimary_sessions_before_writing(
    monkeypatch: pytest.MonkeyPatch, grants: Any, claims: dict[str, Any]
) -> None:
    with pytest.raises(HTTPException) as failure:
        await _mint(monkeypatch, _caller(**claims), personal=True)
    assert failure.value.status_code == 403
    assert saved == []


async def test_personal_mint_refuses_auth_off(monkeypatch: pytest.MonkeyPatch, grants: Any) -> None:
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    with pytest.raises(HTTPException) as failure:
        await _mint(monkeypatch, _caller(session_id="sid", first_party=True), personal=True)
    assert failure.value.status_code == 403 and saved == []


async def test_personal_mint_refuses_on_behalf_header(
    monkeypatch: pytest.MonkeyPatch, grants: Any
) -> None:
    caller = _caller(session_id="sid", first_party=True)
    monkeypatch.setattr("server.auth.bearer.verify_bearer", AsyncMock(return_value=caller))
    request: Any = SimpleNamespace(
        headers={"Authorization": "Bearer t", "X-Loom-On-Behalf-Of": "bob"}, state=SimpleNamespace()
    )
    with pytest.raises(HTTPException) as failure:
        await me.me_personal_tokens_create(request, {"namespaces": ["a"]})
    assert failure.value.status_code == 403 and saved == []


async def test_delegation_never_copies_a_personal_reader_claim(
    monkeypatch: pytest.MonkeyPatch, grants: Any
) -> None:
    response = await _mint(monkeypatch, _caller(session_id="sid", first_party=True), personal=True)
    parent = tokens.verify_token(response["token"])
    assert parent.reads_as_creator
    child = await delegation.delegate(
        parent,
        org="o",
        requested={"a": "viewer"},
        scoped=True,
        ttl_seconds=60,
        is_service_account=True,
    )
    assert not child.as_auth("sa:child").reads_as_creator
    assert not tokens.verify_token(child.mint("sa:child")).reads_as_creator


@pytest.mark.parametrize(
    ("claims", "reader"),
    [
        ({"session_id": "sid", "first_party": True}, "alice"),
        ({"subject": "sa:personal", "creator": "alice", "reads_as_creator": True}, "alice"),
        ({"subject": "sa:ci", "creator": "alice", "is_service_account": True}, ""),
        ({"session_id": "sid", "impersonated_by": "bob"}, ""),
        ({}, ""),
    ],
)
async def test_answer_token_explicitly_reads_as_the_callers_person_and_its_child_does_not(
    monkeypatch: pytest.MonkeyPatch, grants: Any, claims: dict[str, Any], reader: str
) -> None:
    from server.assistant.app import runtime
    from server.auth.reader_identity import reader_subject

    token = tokens.verify_token(
        await runtime.answer_token(_caller(namespaces=("a",), **claims), "o", "a")
    )
    assert token.reads_as_creator is bool(reader)
    assert reader_subject(token) == reader
    child = await delegation.delegate(
        token,
        org="o",
        requested={"a": "viewer"},
        scoped=True,
        ttl_seconds=30,
        is_service_account=True,
    )
    child_auth = tokens.verify_token(child.mint("sa:answer-child"))
    assert not child_auth.reads_as_creator and not reader_subject(child_auth)
    from server.core.errors import AssistantSelfCallError

    with pytest.raises(AssistantSelfCallError):
        runtime.refuse_self_call(child_auth)


async def test_personal_kind_survives_listing_and_revocation(
    monkeypatch: pytest.MonkeyPatch, grants: Any
) -> None:
    from server.auth.service_accounts import revoked_row
    from server.core.datetimes import naive_utc_now_seconds

    parent = _caller(session_id="sid", first_party=True)
    await _mint(monkeypatch, parent, personal=True)
    [stored] = saved
    revoked = revoked_row(stored, revoked_at=naive_utc_now_seconds(), revoked_by=parent.subject)
    assert revoked == {
        **stored,
        "revoked_at": revoked["revoked_at"],
        "revoked_by": "alice",
        "version": revoked["version"],
    }
    assert revoked["kind"] == "personal_agent" and revoked["version"] > stored["version"]

    class Client:
        async def query_all(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [stored]

    monkeypatch.setattr("server.core.ch.get_client", lambda: Client())
    request: Any = SimpleNamespace(headers={"Authorization": "Bearer t"}, state=SimpleNamespace())
    listed = await me.me_service_accounts_list(request)
    assert listed["rows"][0]["kind"] == "personal_agent"
    assert listed["rows"][0]["sa_subject"] == stored["sa_subject"]


async def test_personal_mint_binds_the_once_verified_primary_session(
    monkeypatch: pytest.MonkeyPatch, grants: Any
) -> None:
    parent = _caller(session_id="sid", first_party=True)
    checked: list[str] = []

    async def verify(token: str) -> tokens.AuthInfo:
        checked.append(token)
        return parent

    monkeypatch.setattr("server.auth.bearer.verify_bearer", verify)
    request: Any = SimpleNamespace(headers={"Authorization": "Bearer t"}, state=SimpleNamespace())
    result = await me.me_personal_tokens_create(request, {"namespaces": ["a"]})
    assert tokens.verify_token(result["token"]).creator == parent.subject
    assert checked == ["t"]
