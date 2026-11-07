"""The one token mint (`/v1/me/service_accounts.create`) goes through the one delegation
policy: the default `*` token follows the caller's live grants, a scoped caller's `*`
becomes its own namespace list, the token never carries an org-wide role, it expires (90
days by default), it is saved to the caller's token list, and it comes with every MCP
client's setup."""

from __future__ import annotations

import dataclasses
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import authz, delegation
from server.auth.jwt import AuthInfo, verify_token
from server.core import grant_cache
from server.identity.app import me_service_accounts as me
from server.operations.base import Role, grant_rank
from server.settings import settings

saved: list[dict[str, Any]] = []


class _CH:
    async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
        assert table == "loom.service_accounts"
        saved.extend(rows)

    async def query_all(self, sql: str, **kw: Any) -> list[dict[str, Any]]:
        return []  # no restricted namespace, no ranked grants, no kinds

    async def query_one(self, sql: str, **kw: Any) -> dict[str, Any] | None:
        if "max(version)" in sql:  # a new token's version, past its name's rows
            return {"v": time.time_ns()}
        return None  # no saved pick


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str, str], Role]:
    held: dict[tuple[str, str, str], Role] = {("o", "a", "alice"): Role.WRITER}

    async def grant_role_for(org: str, ns: str, subject: str) -> Role | None:
        return held.get((org, ns, subject))

    async def grant_rows(org: str, subject: str) -> grant_cache.GrantMap:
        mine = {ns: role for (o, ns, sub), role in held.items() if (o, sub) == (org, subject)}
        return grant_cache.GrantMap(
            ranks={ns: grant_rank(r.name.lower()) for ns, r in mine.items()}
        )

    async def account_active(org: str, creator: str) -> bool:
        return True

    monkeypatch.setattr(authz, "grant_role_for", grant_role_for)
    # What the minted token's default reads: its creator's account and live grants.
    monkeypatch.setattr(grant_cache, "_read", grant_rows)
    monkeypatch.setattr("server.auth.delegation.creator_account_active", account_active)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(settings, "public_base_url", "https://loom.example")
    saved.clear()
    return held


async def _setup(monkeypatch: pytest.MonkeyPatch, caller: AuthInfo, body: dict) -> dict[str, Any]:
    monkeypatch.setattr("server.auth.bearer.verify_bearer", lambda token: caller)
    request: Any = SimpleNamespace(headers={"Authorization": "Bearer t"}, state=SimpleNamespace())
    return await me.me_service_accounts_create(request, body)


def _caller(**kw: Any) -> AuthInfo:
    base: dict[str, Any] = {
        "subject": "alice",
        "org": "o",
        "namespaces": ("home",),
        "roles": (Role.PLATFORM_ADMIN,),
        "is_service_account": False,
        "token_id": "login-1",
        "issued_at": int(time.time()),
    }
    return AuthInfo(**{**base, **kw})


@pytest.mark.asyncio
async def test_the_default_token_follows_live_grants_and_drops_org_wide_roles(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    out = await _setup(monkeypatch, _caller(), {})
    token = verify_token(out["token"])
    assert token.namespaces == ("*",) and token.roles == (Role.ADMIN,)
    assert token.creator == "alice" and token.parents == ("login-1",)
    assert token.expires_at is not None
    assert token.expires_at - time.time() > (delegation.DEFAULT_TTL_DAYS - 1) * 86400
    assert out["reads_everything"] and out["namespaces"] == ["*"] and out["ns_roles"] == {}
    assert out["roles"] == ["admin"]
    assert out["name"].startswith("token-") and out["mcp_url"] == "https://loom.example/mcp"
    assert set(out["clients"]) == {
        "claude-desktop",
        "claude-code",
        "codex",
        "cursor",
        "vscode",
        "cline",
        "windsurf",
        "zed",
    }
    assert out["token"] in str(out["clients"]["cursor"]["config"])


@pytest.mark.asyncio
async def test_a_scoped_caller_gets_its_own_list_not_the_org(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    grants[("o", "b", "alice")] = Role.WRITER
    scoped = _caller(namespaces=("a",), roles=(), ns_roles=(("a", "viewer"),))
    out = await _setup(monkeypatch, scoped, {})
    token = verify_token(out["token"])
    assert token.namespaces == ("a",) and token.roles == (Role.VIEWER,)
    assert not out["reads_everything"] and out["namespaces"] == ["a"]


@pytest.mark.asyncio
async def test_every_token_is_saved_under_its_own_subject_and_name(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    out = await _setup(monkeypatch, _caller(), {"name": "laptop"})
    token = verify_token(out["token"])
    [row] = saved
    assert token.subject == row["sa_subject"] == out["sa_subject"]
    assert token.subject.startswith("sa:laptop-") and row["name"] == out["name"] == "laptop"
    assert token.is_service_account and row["created_by"] == "alice"


def test_a_generated_name_holds_the_day_and_differs_per_token() -> None:
    from datetime import datetime

    day = datetime(2026, 9, 18, 14, 41, 12)
    a, b = me.generated_name(day), me.generated_name(day)
    assert a != b and a.startswith("token-2026-09-18-") and len(a) == len("token-2026-09-18-") + 12
    assert me.token_name(a) == a  # a name the create route itself accepts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"namespaces": ["*", {"namespace": "a", "role": "viewer"}]},
        {"namespaces": "*", "role": "viewer"},
        {"role": "viewer"},
        {"namespaces": []},
        {"namespaces": "a"},
    ],
)
async def test_a_role_beside_star_or_a_malformed_list_is_a_422(
    monkeypatch: pytest.MonkeyPatch, grants: dict, body: dict
) -> None:
    with pytest.raises(HTTPException) as err:
        await _setup(monkeypatch, _caller(), body)
    assert err.value.status_code == 422 and not saved


@pytest.mark.asyncio
async def test_saves_to_is_asked_in_the_org_the_mcp_transport_resolves(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    asked: list[str] = []

    async def _default(auth: Any, org: str) -> str:
        asked.append(org)
        return "a"

    monkeypatch.setattr(me, "default_namespace", _default)
    monkeypatch.setattr(me, "caller_org", lambda auth: "signup-org")
    out = await _setup(monkeypatch, _caller(), {})
    assert asked == ["signup-org"] and out["saves_to"] == "a"


@pytest.mark.asyncio
async def test_no_list_or_a_null_one_reads_everything(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    out = await _setup(monkeypatch, _caller(), {"namespaces": None})
    assert out["reads_everything"] and verify_token(out["token"]).namespaces == ("*",)


@pytest.fixture
def restricted(monkeypatch: pytest.MonkeyPatch, grants: dict) -> list[tuple[str, dict]]:
    """`secret` and its bound tracker record are restricted; alice reads both. `vault` is
    restricted and alice's grant there is the delegation's only: her own read of it is
    gone. Returns the direct grants each mint wrote, as (token subject, roles)."""
    import server.identity.app.namespace_classification as nc
    import server.tracker.app.binding as binding
    from server.identity.app import me_callers, token_grants

    grants[("o", "secret", "alice")] = Role.VIEWER
    grants[("o", "tracker-secret", "alice")] = Role.VIEWER
    grants[("o", "vault", "alice")] = Role.VIEWER
    own = {"secret": "viewer", "tracker-secret": "viewer"}

    async def _isr(org: str, ns: str) -> bool:
        return ns in {"secret", "tracker-secret", "vault"}

    async def _role(org: str, ns: str, subject: str) -> str | None:
        return own.get(ns) if subject == "alice" else None

    written: list[tuple[str, dict]] = []

    async def _grant(org: str, sa: str, creator: str, roles: dict, **_: Any) -> list[str]:
        written.append((sa, dict(roles)))
        return list(roles)

    monkeypatch.setattr(nc, "is_restricted", _isr)
    monkeypatch.setattr(me, "is_restricted", _isr)
    monkeypatch.setattr(
        binding, "tracker_namespaces_for", lambda ns: ["tracker-secret"] if ns == "secret" else []
    )
    monkeypatch.setattr(me_callers, "caller_role_on", _role)
    monkeypatch.setattr(token_grants, "grant_restricted_to_token", _grant)
    return written


@pytest.mark.asyncio
async def test_a_restricted_repository_beside_star_brings_its_tracker_and_a_capped_grant(
    monkeypatch: pytest.MonkeyPatch, restricted: list
) -> None:
    out = await _setup(monkeypatch, _caller(), {"namespaces": ["a", "*", "secret"]})
    token = verify_token(out["token"])
    assert token.namespaces == ("a", "*", "secret", "tracker-secret")
    # One role for the token, not lowered by what alice only reads...
    assert token.roles == (Role.ADMIN,)
    # ...while its own grants there are her role, the most a reader of them may hold.
    assert restricted == [(out["sa_subject"], {"secret": "viewer", "tracker-secret": "viewer"})]
    assert out["restricted_namespaces"] == ["secret", "tracker-secret"]


@pytest.mark.asyncio
async def test_a_listed_restricted_repository_brings_its_tracker_too(
    monkeypatch: pytest.MonkeyPatch, restricted: list
) -> None:
    out = await _setup(monkeypatch, _caller(), {"namespaces": ["a", "secret"]})
    token = verify_token(out["token"])
    assert token.namespaces == ("a", "secret", "tracker-secret")
    assert dict(token.ns_roles)["tracker-secret"] == "viewer"
    assert restricted == [(out["sa_subject"], {"secret": "viewer", "tracker-secret": "viewer"})]


@pytest.mark.asyncio
async def test_a_restricted_grant_is_no_wider_than_the_role_the_list_asks_for(
    monkeypatch: pytest.MonkeyPatch, restricted: list
) -> None:
    from server.identity.app import me_callers

    async def _writer(org: str, ns: str, subject: str) -> str | None:
        return "writer"

    monkeypatch.setattr(me_callers, "caller_role_on", _writer)
    out = await _setup(
        monkeypatch, _caller(), {"namespaces": [{"namespace": "secret", "role": "viewer"}]}
    )
    assert restricted == [(out["sa_subject"], {"secret": "viewer", "tracker-secret": "viewer"})]


@pytest.mark.asyncio
async def test_star_under_a_narrow_parent_grants_its_restricted_names_where_the_subject_holds_one(
    monkeypatch: pytest.MonkeyPatch, restricted: list
) -> None:
    # The parent's own claim names `secret` and `vault`; alice holds a grant on `secret`
    # only. Both come into the child through `*`: `secret` gets a grant, `vault` none.
    parent = _caller(
        namespaces=("a", "secret", "vault"),
        roles=(),
        ns_roles=(("a", "writer"), ("secret", "viewer"), ("vault", "viewer")),
    )
    out = await _setup(monkeypatch, parent, {})
    token = verify_token(out["token"])
    assert set(token.namespaces) >= {"a", "secret", "vault"}
    assert restricted[0][1].get("secret") == "viewer" and "vault" not in restricted[0][1]


@pytest.mark.asyncio
async def test_a_bound_tracker_the_parent_cannot_reach_is_left_out_not_refused(
    monkeypatch: pytest.MonkeyPatch, restricted: list
) -> None:
    parent = _caller(namespaces=("secret",), roles=(), ns_roles=(("secret", "viewer"),))
    out = await _setup(monkeypatch, parent, {"namespaces": ["secret"]})
    assert verify_token(out["token"]).namespaces == ("secret",)


@pytest.mark.asyncio
async def test_a_standard_repository_brings_no_tracker_record(
    monkeypatch: pytest.MonkeyPatch, restricted: list
) -> None:
    # `a` is standard but bound to the restricted record: a list reads what it ticks.
    import server.tracker.app.binding as binding

    monkeypatch.setattr(binding, "tracker_namespaces_for", lambda ns: ["tracker-secret"])
    out = await _setup(monkeypatch, _caller(), {"namespaces": ["a"]})
    assert verify_token(out["token"]).namespaces == ("a",) and restricted == [
        (out["sa_subject"], {})
    ]


@pytest.mark.asyncio
async def test_a_token_minting_a_token_needs_its_own_grant_on_a_restricted_namespace(
    monkeypatch: pytest.MonkeyPatch, restricted: list
) -> None:
    # A token alice made reads `secret` only through a grant of its own, as the gate
    # asks of it; holding none, it cannot hand one on, whatever alice holds.
    sa = _caller(subject="sa:bot", is_service_account=True, creator="alice", namespaces=("*",))
    with pytest.raises(HTTPException) as err:
        await _setup(monkeypatch, sa, {"namespaces": ["a", "*", "secret"]})
    assert err.value.status_code == 403 and not saved and not restricted


@pytest.mark.asyncio
@pytest.mark.parametrize("namespaces", [["a", "*", "vault"], ["vault"]])
async def test_a_restricted_namespace_the_minter_holds_no_grant_on_is_refused_before_saving(
    monkeypatch: pytest.MonkeyPatch, restricted: list, namespaces: list
) -> None:
    with pytest.raises(HTTPException) as err:
        await _setup(monkeypatch, _caller(), {"namespaces": namespaces})
    assert err.value.status_code == 403 and "vault" in err.value.detail["message"]
    assert not saved and not restricted


@pytest.mark.asyncio
async def test_a_failed_grant_read_saves_no_token(
    monkeypatch: pytest.MonkeyPatch, restricted: list
) -> None:
    from server.core.errors import BackendUnavailableError
    from server.identity.app import me_callers

    async def _down(org: str, ns: str, subject: str) -> str | None:
        raise BackendUnavailableError("grants could not be read")

    monkeypatch.setattr(me_callers, "caller_role_on", _down)
    with pytest.raises(BackendUnavailableError):
        await _setup(monkeypatch, _caller(), {"namespaces": ["a", "*", "secret"]})
    assert not saved and not restricted


@pytest.mark.asyncio
async def test_a_namespace_named_before_star_is_where_it_saves(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    grants[("o", "b", "alice")] = Role.WRITER
    out = await _setup(monkeypatch, _caller(), {"namespaces": ["b", "*"]})
    token = verify_token(out["token"])
    assert token.namespaces == ("b", "*") and token.roles == (Role.ADMIN,)
    assert out["reads_everything"] and out["saves_to"] == "b"


@pytest.mark.asyncio
async def test_an_unreadable_default_still_hands_over_the_saved_token(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    # The token is minted and listed before its default is asked: a 503 then
    # would leave it unseen and a retry would mint another.
    from server.core.errors import BackendUnavailableError

    async def unreadable(auth: Any, org: str) -> str:
        raise BackendUnavailableError("grants could not be read")

    monkeypatch.setattr(me, "default_namespace", unreadable)
    out = await _setup(monkeypatch, _caller(), {})
    assert out["saves_to"] is None and len(saved) == 1
    assert verify_token(out["token"]).subject == saved[0]["sa_subject"]


@pytest.mark.asyncio
async def test_a_service_account_mints_a_saved_service_account_token(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    sa = _caller(subject="sa:bot", is_service_account=True, creator="alice", namespaces=("*",))
    out = await _setup(monkeypatch, sa, {})
    assert verify_token(out["token"]).is_service_account
    assert [row["created_by"] for row in saved] == ["sa:bot"]


@pytest.mark.asyncio
async def test_a_namespace_the_caller_holds_no_grant_on_is_refused(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    for namespaces in (["c"], ["c", "*"]):
        with pytest.raises(HTTPException) as err:
            await _setup(monkeypatch, _caller(), {"namespaces": namespaces})
        assert err.value.status_code == 403 and "c" in err.value.detail["message"]
    out = await _setup(monkeypatch, _caller(), {"namespaces": ["a"]})
    token = verify_token(out["token"])
    assert token.namespaces == ("a",) and token.ns_roles == (("a", "writer"),)
    assert not out["reads_everything"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ttl_days", [-1, 3651, 10**9])
async def test_a_ttl_outside_ten_years_is_a_422(
    monkeypatch: pytest.MonkeyPatch, grants: dict, ttl_days: int
) -> None:
    with pytest.raises(HTTPException) as err:
        await _setup(monkeypatch, _caller(), {"ttl_days": ttl_days})
    assert err.value.status_code == 422


@pytest.mark.asyncio
async def test_the_saved_row_is_the_token_list_row(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    out = await _setup(monkeypatch, _caller(), {"ttl_days": 30})
    [row] = saved
    assert row["name"] == out["name"] and out["sa_subject"].startswith(f"sa:{out['name']}-")
    assert row["expires_at"] == row["created_at"] + timedelta(days=30)
    assert row["version"] > 0
    volatile = {"name", "created_at", "expires_at", "version"}
    assert {k: v for k, v in row.items() if k not in volatile} == {
        "org": "o",
        "sa_subject": out["sa_subject"],
        "namespaces": ["*"],
        "roles": ["admin"],
        "ns_roles": "{}",  # the column default, which the row left it to before
        "ttl_days": 30,
        "created_by": "alice",
        "revoked_at": None,
        "revoked_by": "",
        "kind": "service_account",
        "client": "",
        "inherit_creator_grants": True,
        "lineage": ["alice"],  # the subject that made it
    }


@pytest.mark.asyncio
async def test_the_label_needs_no_revocation_list_once_the_token_is_saved(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    # The row is saved before the label is worked out; a revocation list not
    # loaded (or stale) then must not fail the call and strand an unseen token.
    from server.auth import revocation

    monkeypatch.setattr(revocation, "loaded", lambda: False)
    out = await _setup(monkeypatch, _caller(), {"namespaces": ["a", "*"]})
    assert out["saves_to"] == "a" and len(saved) == 1


# The delegations the mint makes (scoped=False: the default `*`, and a home and a
# restricted namespace beside it) and a list's.
_SHAPES = [
    ({"*": None}, False),
    ({"a": None, "*": None, "secret": None}, False),
    ({"a": None}, True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("requested", "scoped"), _SHAPES)
async def test_the_label_identity_is_the_one_the_token_verifies_as(
    monkeypatch: pytest.MonkeyPatch, grants: dict, requested: dict, scoped: bool
) -> None:
    grants[("o", "secret", "alice")] = Role.WRITER
    caller = (
        _caller(namespaces=("a",), roles=(), ns_roles=(("a", "writer"),)) if scoped else _caller()
    )
    delegated = await delegation.delegate(
        caller,
        org="o",
        requested=requested,
        scoped=scoped,
        ttl_seconds=3600,
        is_service_account=True,
    )
    verified = verify_token(delegated.mint("sa:cursor-ab12cd"))
    built = delegated.as_auth("sa:cursor-ab12cd")
    # Set at verification only: the token's id and its iat/exp.
    minted_only = {"token_id", "issued_at", "expires_at"}
    for field in dataclasses.fields(AuthInfo):
        if field.name not in minted_only:
            assert getattr(built, field.name) == getattr(verified, field.name), field.name


@pytest.mark.asyncio
async def test_the_page_shows_the_expiry_the_saved_row_holds(
    monkeypatch: pytest.MonkeyPatch, grants: dict
) -> None:
    # A clock that moves between reads: the response must not read it again.
    ticks = iter(range(100))
    start = me.naive_utc_now_seconds()
    monkeypatch.setattr(me, "naive_utc_now_seconds", lambda: start + timedelta(seconds=next(ticks)))
    out = await _setup(monkeypatch, _caller(), {"ttl_days": 30})
    [row] = saved
    assert out["expires_at"] == row["expires_at"].isoformat()
