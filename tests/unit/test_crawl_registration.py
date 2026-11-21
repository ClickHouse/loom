"""Real JWT and shared authorization rules for an exact retained source approval."""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any

import jwt
import pytest

from server.auth import authz
from server.auth import source_registration as registration
from server.auth.context import current_auth
from server.auth.jwt import AuthInfo, verify_token
from server.core.secrets_at_rest import seal, unseal
from server.operations.base import Role
from server.settings import settings
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_authority import world as world
from tests.unit.test_crawl_signing_authority import _SECOND
from tests.unit.test_crawl_signing_authority import key_view as key_view

pytestmark = [pytest.mark.asyncio, pytest.mark.claim_cap]
_SCOPE = registration.Scope("team", "shared-space", "crawl-source", "crawl-generation", "f" * 64)


def _token(**changes: Any) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": "person@example.com",
            "org": "team",
            "namespaces": ["shared-space"],
            "roles": ["org_admin"],
            "sa": False,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": now - 10,
            "exp": now + 3600,
            "jti": "registration-token",
            **changes,
        },
        _SECOND,
        algorithm="HS256",
        headers={"kid": "signer"},
    )


async def _approval(state: Any, **changes: Any):
    state.grant = Role.ADMIN
    raw = _token(**changes)
    auth = verify_token(raw)
    with acting_as(auth):
        registrant, encoded = await registration.capture(auth, _SCOPE)
    return auth, registrant, encoded, raw


async def test_retained_metadata_is_exact_scoped_and_never_becomes_a_request_principal(
    world: Any,
    key_view: list[str],
) -> None:
    state, _http = world
    auth, registrant, encoded, raw = await _approval(state)
    approval = registration.load(encoded, _SCOPE, registrant=registrant)
    before = current_auth()
    await approval.validate()
    assert current_auth() is before
    assert registrant == "person" and not isinstance(approval.claims, AuthInfo)
    assert approval.claims.roles == auth.roles and approval.claims.expires_at == auth.expires_at
    plain = unseal(registration.PURPOSE, encoded)
    assert raw not in plain and _SECOND not in plain
    assert json.loads(plain)["scope"] == dataclasses.asdict(_SCOPE)
    with pytest.raises(PermissionError, match="invalid auth principal"):
        await authz.check_authorized(approval.claims, Role.ADMIN, "team", "shared-space")


@pytest.mark.parametrize("field", ["creator", "impersonated_by", "parent_subjects"])
async def test_historical_approval_inspection_finds_typed_person_lineage_without_granting_authority(
    world: Any,
    key_view: list[str],
    field: str,
) -> None:
    state, _http = world
    _auth, _registrant, original, _raw = await _approval(state)
    frame = json.loads(unseal(registration.PURPOSE, original))
    # These are historical typed metadata, not a live AuthInfo or a newly approved source.
    frame["claims"][field] = ["deleted-alias"] if field == "parent_subjects" else "deleted-alias"
    if field == "parent_subjects":
        frame["claims"]["parent_iats"] = [frame["claims"]["issued_at"]]
    encoded = seal(registration.PURPOSE, json.dumps(frame))
    before = current_auth()
    assert registration.names_person(
        encoded,
        org="team",
        namespace="shared-space",
        source_id="crawl-source",
        spellings=("deleted-alias",),
    )
    assert not registration.names_person(
        encoded,
        org="team",
        namespace="shared-space",
        source_id="crawl-source",
        spellings=("someone-else",),
    )
    assert current_auth() is before


@pytest.mark.parametrize("field", ["org", "namespace", "source_id"])
async def test_person_inspection_cannot_retire_an_approval_copied_from_another_scope(
    world: Any,
    key_view: list[str],
    field: str,
) -> None:
    state, _http = world
    _auth, _registrant, encoded, _raw = await _approval(state)
    scope = {"org": "team", "namespace": "shared-space", "source_id": "crawl-source"}
    scope[field] = "foreign"
    with pytest.raises(registration.Refused):
        registration.names_person(encoded, **scope, spellings=("person", "person@example.com"))


@pytest.mark.parametrize("nested", [False, True])
async def test_unknown_retained_claim_restrictions_refuse_instead_of_being_ignored(
    world: Any,
    key_view: list[str],
    nested: bool,
) -> None:
    state, _http = world
    _auth, registrant, original, _raw = await _approval(state)
    frame = json.loads(unseal(registration.PURPOSE, original))
    target = frame["claims"]["signing_authority"] if nested else frame["claims"]
    target["future_restriction"] = {"deny": True}
    encoded = seal(registration.PURPOSE, json.dumps(frame))
    with pytest.raises(registration.Refused):
        registration.load(encoded, _SCOPE, registrant=registrant)


@pytest.mark.parametrize("field", ["namespace", "source_id", "generation", "org", "resource_hash"])
async def test_copied_ciphertext_cannot_approve_another_source_or_generation(
    world: Any,
    key_view: list[str],
    field: str,
) -> None:
    state, _http = world
    _auth, registrant, encoded, _raw = await _approval(state)
    changed = dataclasses.replace(_SCOPE, **{field: "different"})
    with pytest.raises(registration.Refused):
        registration.load(encoded, changed, registrant=registrant)


async def test_namespace_demotion_refuses_retained_named_claim_using_fresh_real_grants(
    world: Any,
    key_view: list[str],
) -> None:
    state, _http = world
    _auth, registrant, encoded, _raw = await _approval(state)
    approval = registration.load(encoded, _SCOPE, registrant=registrant)
    state.grant = Role.WRITER
    with pytest.raises(PermissionError):
        await approval.validate()


async def test_flat_wildcard_org_role_keeps_existing_additive_claim_semantics(
    world: Any,
    key_view: list[str],
) -> None:
    state, _http = world
    _auth, registrant, encoded, _raw = await _approval(state, namespaces=["*"])
    state.grant = Role.WRITER
    await registration.load(encoded, _SCOPE, registrant=registrant).validate()


@pytest.mark.parametrize(
    "change",
    [
        {"roles": ["admin"]},
        {"roles": ["org_admin"], "ns_roles": {"shared-space": "viewer"}},
    ],
)
async def test_registration_requires_both_real_flat_org_role_and_namespace_admin(
    world: Any,
    key_view: list[str],
    change: dict[str, Any],
) -> None:
    state, _http = world
    state.grant = Role.ADMIN
    auth = verify_token(_token(**change))
    with acting_as(auth), pytest.raises(PermissionError):
        await registration.capture(auth, _SCOPE)


@pytest.mark.parametrize("loss", ["signing-key", "token-revocation", "scim", "expiry"])
async def test_registration_rechecks_actual_signing_token_and_person_authority(
    monkeypatch: pytest.MonkeyPatch,
    world: Any,
    key_view: list[str],
    loss: str,
) -> None:
    state, _http = world
    auth, registrant, encoded, _raw = await _approval(state)
    approval = registration.load(encoded, _SCOPE, registrant=registrant)
    if loss == "signing-key":
        key_view.remove(_SECOND)
    elif loss == "token-revocation":
        state.cutoffs["registration-token"] = None
    elif loss == "scim":
        state.scim_ended = True
    else:
        assert auth.expires_at is not None
        monkeypatch.setattr(
            registration, "_now", lambda: auth.expires_at + settings.jwt_leeway_seconds + 1
        )
    with pytest.raises(PermissionError):
        await approval.validate()


async def test_offline_verified_or_changed_presenter_cannot_create_persistent_approval(
    world: Any,
    key_view: list[str],
) -> None:
    state, _http = world
    state.grant = Role.ADMIN
    offline = verify_token(_token(), secret=_SECOND)
    with acting_as(offline), pytest.raises(registration.Refused):
        await registration.capture(offline, _SCOPE)
    auth = verify_token(_token())
    with acting_as(auth), pytest.raises(registration.Refused):
        await registration.capture(dataclasses.replace(auth), _SCOPE)
