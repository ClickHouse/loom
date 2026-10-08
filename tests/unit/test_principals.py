"""The principal builder makes the `AuthInfo` a token would, and `acting_as`
never leaves one behind."""

from __future__ import annotations

import dataclasses

import pytest

from server.auth.context import current_auth
from server.auth.jwt import AuthInfo
from server.operations.base import Role
from tests.unit.principals import acting_as, principal


def test_defaults_are_one_tenants_service_account() -> None:
    auth = principal("writer")
    assert (auth.org, auth.namespaces, auth.roles) == ("acme", ("shop",), (Role.WRITER,))
    assert auth.is_service_account and auth.subject == "sa:writer"
    assert auth.has_role(Role.VIEWER) and not auth.has_role(Role.ADMIN)


def test_no_role_is_no_role() -> None:
    assert principal().roles == () and not principal().has_role(Role.VIEWER)


def test_a_human_and_extra_claims() -> None:
    auth = principal(
        Role.ADMIN, service_account=False, read_scope="own_user", ns_roles=(("shop", "viewer"),)
    )
    assert auth.subject == "admin@example.com" and not auth.is_service_account
    assert auth.read_scope == "own_user" and auth.has_ns_roles()


def test_an_unknown_role_or_field_is_refused() -> None:
    with pytest.raises(KeyError):
        principal("superuser")
    with pytest.raises(TypeError):
        principal("viewer", clearance="top")


def test_every_required_authinfo_field_is_filled() -> None:
    required = {f.name for f in dataclasses.fields(AuthInfo) if f.default is dataclasses.MISSING}
    assert required == {"subject", "org", "namespaces", "roles", "is_service_account"}, (
        "AuthInfo gained a required field; give principal() a default for it"
    )


def test_acting_as_restores_the_previous_principal_even_on_error() -> None:
    outer, inner = principal("viewer"), principal("admin")
    with acting_as(outer):
        with pytest.raises(RuntimeError), acting_as(inner):
            assert current_auth() is inner
            raise RuntimeError
        assert current_auth() is outer
    assert current_auth() is None
