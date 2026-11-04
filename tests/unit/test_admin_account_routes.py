"""The platform-admin API for accounts and bots: each route runs the shared
action the host CLI runs, with the admin as its actor, and only a platform
admin reaches it."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import machines
from server.auth.jwt import mint_token
from server.identity.app import account_admin, admin_account_routes


@pytest.fixture
def calls(monkeypatch) -> list[tuple[str, Any]]:
    seen: list[tuple[str, Any]] = []

    async def adopt_plan(domain: str, org: str = "", skip: Any = ()) -> list[dict[str, Any]]:
        seen.append(("plan", (domain, list(skip))))
        if "typo@acme.io" in skip:
            raise ValueError("no unlinked account at acme.io has email typo@acme.io")
        return [{"email": "lee@acme.io", "skip": ""}]

    async def adopt(plan: list[dict[str, Any]], *, by: str, keep_tokens: bool) -> int:
        seen.append(("adopt", by))
        return len(plan)

    async def unlink(email: str, org: str = "", *, by: str) -> list[tuple[str, str]]:
        seen.append(("unlink", email))
        return [("acme", "lee")]

    async def password_link(email: str, org: str = "", *, by: str) -> tuple[str, list[str]]:
        if email == "nobody@acme.io":
            raise ValueError("nobody@acme.io has no SSO account")
        return "the-code", ["u-lee-1"]

    async def create_bot(bot: machines.Machine, *, by: str, replace: bool) -> None:
        seen.append(("create", bot["name"]))

    async def disable_bot(name: str, *, by: str) -> bool:
        seen.append(("disable", name))
        return True

    async def listed() -> list[dict[str, Any]]:
        return [
            {
                "name": "triage",
                "org": "acme",
                "active": 1,
                "ns_roles": [("triage", "writer")],
                "has_key": 1,
                "issuer": "",
                "audience": "",
                "claims": {},
                "agent_claim": "",
                "jwks_uri": "",
                "max_token_s": 3600,
                "max_agents": 1000,
                "max_access_token_s": 86400,
                "platform_admin": 0,
                "updated_by": "cli:root",
            }
        ]

    async def link_account(
        email: str, org: str, namespace: str, *, by: str
    ) -> tuple[str, list[str], bool]:
        seen.append(("link", (email, org, namespace)))
        if email == "taken@acme.io":
            raise ValueError("taken@acme.io is already acme/lee's; release that one first")
        return "lee@home.example", ["secret"], True

    for name, fake in [
        ("link_account", link_account),
        ("adopt_plan", adopt_plan),
        ("adopt", adopt),
        ("unlink", unlink),
        ("password_link", password_link),
        ("create_bot", create_bot),
        ("disable_bot", disable_bot),
    ]:
        monkeypatch.setattr(account_admin, name, fake)
    monkeypatch.setattr(machines, "listed", listed)
    from server.insights.app import admin_routes

    monkeypatch.setattr(type(admin_routes.settings), "auth_actually_required", lambda self: False)
    monkeypatch.setattr(admin_account_routes.oauth.settings, "oauth_issuer", "https://loom.example")
    return seen


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_account_routes.router)
    return TestClient(app)


def test_every_route_runs_the_shared_action(calls) -> None:
    c = _client()
    adopted = c.post("/v1/admin/sso.adopt", json={"domain": "acme.io", "apply": True}).json()
    assert adopted["linked"] == 1 and adopted["revokes_tokens"] is True
    assert c.post("/v1/admin/sso.unlink", json={"email": "lee@acme.io"}).json() == {
        "unlinked": [{"org": "acme", "namespace": "lee"}]
    }
    link = c.post("/v1/admin/sso.password_link", json={"email": "lee@acme.io"}).json()
    assert link["url"] == "https://loom.example/oauth/set-password?code=the-code"
    assert (
        c.post("/v1/admin/sso.password_link", json={"email": "nobody@acme.io"}).status_code == 422
    )
    made = c.post(
        "/v1/admin/bots.create",
        json={
            "name": "triage",
            "org": "acme",
            "namespace_roles": ["triage=writer"],
            "generate_key": True,
        },
    ).json()
    assert made["subject"] == "sa:triage" and "PRIVATE KEY" in made["private_key_pem"]
    assert c.post("/v1/admin/bots.list").json()["bots"][0]["namespace_roles"] == ["triage=writer"]
    assert c.post("/v1/admin/bots.disable", json={"name": "triage"}).json() == {"disabled": True}
    assert [name for name, _ in calls] == ["plan", "adopt", "unlink", "create", "disable"]


def test_a_dry_run_adopt_writes_nothing(calls) -> None:
    body = {"domain": "acme.io", "skip": ["ci-bot@acme.io"]}
    out = _client().post("/v1/admin/sso.adopt", json=body).json()
    assert out["applied"] is False and out["linked"] == 0
    assert calls == [("plan", ("acme.io", ["ci-bot@acme.io"]))]


def test_a_bot_without_a_key_or_trust_is_refused(calls) -> None:
    r = _client().post(
        "/v1/admin/bots.create", json={"name": "x", "org": "acme", "namespace_roles": ["a=writer"]}
    )
    assert r.status_code == 422 and not calls


def test_only_a_platform_admin_reaches_them(calls, monkeypatch) -> None:
    from server.insights.app import admin_routes

    monkeypatch.setattr(type(admin_routes.settings), "auth_actually_required", lambda self: True)
    writer = mint_token(subject="lee", org="acme", namespaces=["lee"], roles=["writer"])
    admin = mint_token(subject="ops", org="acme", namespaces=["*"], roles=["platform_admin"])
    c = _client()
    for token, status in [(writer, 403), (admin, 200)]:
        r = c.post("/v1/admin/bots.list", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == status
    r = c.post(
        "/v1/admin/bots.disable",
        json={"name": "triage"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert r.status_code == 200 and calls[-1] == ("disable", "triage")


def test_a_bots_longest_token_reaches_it_and_its_listing(calls, monkeypatch) -> None:
    saved: list[machines.Machine] = []

    async def create_bot(bot: machines.Machine, *, by: str, replace: bool) -> None:
        saved.append(bot)

    monkeypatch.setattr(account_admin, "create_bot", create_bot)
    c = _client()
    body = {"name": "fleet", "org": "acme", "namespace_roles": ["a=writer"], "generate_key": True}
    assert c.post("/v1/admin/bots.create", json=body).status_code == 200
    assert (
        c.post("/v1/admin/bots.create", json={**body, "max_access_token_s": 86400}).status_code
        == 200
    )
    assert [b["max_access_token_s"] for b in saved] == [3600, 86400]
    for wrong in (59, 86401):
        r = c.post("/v1/admin/bots.create", json={**body, "max_access_token_s": wrong})
        assert r.status_code == 422 and "max_access_token_s" in r.text
    assert len(saved) == 2
    listed = c.post("/v1/admin/bots.list").json()["bots"][0]
    assert listed["max_access_token_s"] == 86400


def test_generating_a_key_and_giving_one_is_refused(calls) -> None:
    r = _client().post(
        "/v1/admin/bots.create",
        json={
            "name": "x",
            "org": "acme",
            "namespace_roles": ["a=writer"],
            "generate_key": True,
            "public_key_pem": "-----BEGIN PUBLIC KEY-----",
        },
    )
    assert r.status_code == 422 and not calls


def test_a_late_failure_still_hands_back_the_generated_key(calls, monkeypatch) -> None:
    async def half_done(bot: Any, *, by: str, replace: bool) -> None:
        raise RuntimeError("Keeper away after the save")

    monkeypatch.setattr(account_admin, "create_bot", half_done)
    r = _client().post(
        "/v1/admin/bots.create",
        json={"name": "t", "org": "acme", "namespace_roles": ["t=writer"], "generate_key": True},
    )
    assert r.status_code == 200 and "PRIVATE KEY" in r.json()["private_key_pem"]
    assert "send this again" in r.json()["warning"]


def test_a_failure_with_no_key_to_hand_back_is_an_error(calls, monkeypatch) -> None:
    async def down(bot: Any, *, by: str, replace: bool) -> None:
        raise RuntimeError("ClickHouse away")

    monkeypatch.setattr(account_admin, "create_bot", down)
    pem = machines.generate_key()[1].decode()
    with pytest.raises(RuntimeError):
        _client().post(
            "/v1/admin/bots.create",
            json={
                "name": "t",
                "org": "acme",
                "namespace_roles": ["t=writer"],
                "public_key_pem": pem,
            },
        )


def test_a_password_link_is_absolute_without_an_issuer_set(calls, monkeypatch) -> None:
    monkeypatch.setattr(admin_account_routes.oauth.settings, "oauth_issuer", "")
    monkeypatch.setattr(admin_account_routes.oauth.settings, "public_base_url", "")
    link = _client().post("/v1/admin/sso.password_link", json={"email": "lee@acme.io"}).json()
    assert link["url"] == "http://testserver/oauth/set-password?code=the-code"


def test_a_bot_whose_claim_was_never_had_hands_out_no_key(calls, monkeypatch) -> None:
    from server.auth.grant_claims import NotClaimed
    from server.core.errors import GrantBusyError

    for refusal in (GrantBusyError("another change to the bot t is running"), NotClaimed("x")):

        async def not_had(
            bot: Any, *, by: str, replace: bool, refusal: Exception = refusal
        ) -> None:
            raise refusal

        monkeypatch.setattr(account_admin, "create_bot", not_had)
        with pytest.raises(type(refusal)):
            _client().post(
                "/v1/admin/bots.create",
                json={
                    "name": "t",
                    "org": "acme",
                    "namespace_roles": ["t=writer"],
                    "generate_key": True,
                },
            )


@pytest.mark.parametrize(
    "body",
    [
        {"domain": "acme.io", "skip": ["typo@acme.io"], "apply": True},  # matches nothing
        {"domain": "acme.io", "skips": ["ci-bot@acme.io"], "apply": True},  # a misspelled field
    ],
)
def test_an_adoption_whose_skip_cannot_hold_applies_nothing(calls, body: dict[str, Any]) -> None:
    r = _client().post("/v1/admin/sso.adopt", json=body)
    assert r.status_code == 422
    assert "adopt" not in [name for name, _ in calls]


def test_an_unlink_with_a_misspelled_org_is_refused(calls) -> None:
    # Dropped, `orgs` would leave org empty and unlink the email in every org.
    r = _client().post("/v1/admin/sso.unlink", json={"email": "lee@acme.io", "orgs": "acme"})
    assert r.status_code == 422
    assert calls == []


def test_a_link_points_one_account_at_the_providers_address(calls) -> None:
    c = _client()
    body = {"email": "lee@acme.io", "org": "acme", "namespace": "lee"}
    out = c.post("/v1/admin/sso.link", json=body).json()
    assert out == {
        "linked": {"org": "acme", "namespace": "lee"},
        "was": "lee@home.example",
        "carried_revoked": ["secret"],
        "elsewhere": True,
    }
    refused = c.post("/v1/admin/sso.link", json={**body, "email": "taken@acme.io"})
    assert refused.status_code == 422 and "release that one first" in refused.text


# The calls that change how an account signs in: the link points the admin
# account `ops` at an address someone else holds at the provider.
_REBINDS = [
    ("/v1/admin/sso.link", {"email": "attacker@idp.example", "org": "acme", "namespace": "ops"}),
    ("/v1/admin/sso.password_link", {"email": "ops@acme.io"}),
    ("/v1/admin/sso.unlink", {"email": "ops@acme.io"}),
    ("/v1/admin/sso.adopt", {"domain": "acme.io", "apply": True}),
]


@pytest.mark.parametrize(
    ("subject", "service_account", "session", "status"),
    [
        ("sa:cp", True, {}, 403),  # a platform-admin bot's own token
        ("ops", False, {"session_id": "s-1", "first_party": False}, 403),  # a third-party client's
        ("ops", False, {"session_id": "s-1", "first_party": True}, 200),  # a person's `loom login`
    ],
)
def test_only_a_person_rebinds_how_an_account_signs_in(
    calls, monkeypatch, subject: str, service_account: bool, session: Any, status: int
) -> None:
    """A token refused a lasting platform-admin bot must not link an admin's account
    to an address it holds either: signed in there as the admin, it would register one."""
    from server.insights.app import admin_routes

    monkeypatch.setattr(type(admin_routes.settings), "auth_actually_required", lambda self: True)
    token = mint_token(
        subject, "acme", ["*"], ["platform_admin"], is_service_account=service_account, **session
    )
    c, auth = _client(), {"Authorization": f"Bearer {token}"}
    bot = {"name": "cp", "org": "acme", "namespace_roles": [], "generate_key": True}
    for path, body in [*_REBINDS, ("/v1/admin/bots.create", {**bot, "platform_admin": True})]:
        r = c.post(path, json=body, headers=auth)
        assert r.status_code == status, r.text
        if status == 403:  # the same refusal as the bot's
            assert r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
    # Refused, nothing is linked, so the provider's address signs in as no one.
    expected = ["link", "unlink", "plan", "adopt", "create"] if status == 200 else []
    assert [name for name, _ in calls] == expected
    # The gate passes the token: a dry-run adoption, which writes nothing, still answers.
    dry = c.post("/v1/admin/sso.adopt", json={"domain": "acme.io"}, headers=auth)
    assert dry.status_code == 200 and dry.json()["applied"] is False
