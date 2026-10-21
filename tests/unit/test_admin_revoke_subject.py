"""`POST /v1/admin/revoke_subject` and `GET /v1/admin/revocations`.

A platform admin revokes every token and sign-in session of a subject through
the code `loom revoke-token --subject` runs (`server.auth.subject_revocation`):
the subject's row refuses its tokens and the tokens it made, those tokens are
listed revoked as a SCIM deactivation lists them, the revocation is audited in
the org the request names, and a repeat answers the same.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import cli
from server.auth import revocation
from server.auth.jwt import TokenError, mint_token, verify_token
from server.core.errors import GrantBusyError
from server.identity.app import admin_account_routes
from tests.unit.revocation_fakes import revocation_read

pytestmark = pytest.mark.usefixtures("stub_model_command")


def _token(name: str, created_by: str, *, lineage: list[str] | None = None) -> dict[str, Any]:
    """A loom.service_accounts row: a token `created_by` made, never expiring."""
    return {
        "org": "acme",
        "name": name,
        "sa_subject": f"sa:{name}-abc123",
        "namespaces": ["ns"],
        "roles": ["writer"],
        "ttl_days": 0,
        "created_by": created_by,
        "created_at": datetime(2026, 1, 1),
        "expires_at": None,
        "revoked_at": None,
        "revoked_by": "",
        "version": 1,
        "kind": "mcp",
        "client": "claude-code",
        "inherit_creator_grants": True,
        "ns_roles": '{"ns": "writer"}',
        "lineage": [created_by] if lineage is None else lineage,
    }


class _Store:
    """loom.revoked_tokens, loom.service_accounts, the session refresh rows of
    loom.oauth_state, the accounts loom.namespaces ties spellings with, and
    loom.audit_log, as the revocation path uses them."""

    def __init__(self) -> None:
        self.revocations: list[dict[str, Any]] = []
        self.tokens: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []
        self.accounts: list[dict[str, Any]] = []  # org, owner_email, contact_email
        self.audits: list[dict[str, Any]] = []
        self.busy_names: set[str] = set()  # a token name whose revoked row cannot be written
        self.directory_down = False

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    def revoked(self) -> dict[str, dict[str, Any]]:
        """The newest unexpired row of each key, as under FINAL."""
        newest: dict[str, dict[str, Any]] = {}
        for r in self.revocations:
            if r["jti"] not in newest or r["version"] > newest[r["jti"]]["version"]:
                newest[r["jti"]] = r
        now = datetime.now(UTC).replace(tzinfo=None)
        return {k: r for k, r in newest.items() if r["expires_at"] > now}

    def heads(self) -> dict[str, dict[str, Any]]:
        newest: dict[str, dict[str, Any]] = {}
        for r in self.tokens:
            if r["name"] not in newest or r["version"] >= newest[r["name"]]["version"]:
                newest[r["name"]] = r
        return newest

    def _made_by(self, subs: list[str]) -> list[dict[str, Any]]:
        return [
            r
            for r in self.heads().values()
            if r["created_by"] in subs or set(r["lineage"]) & set(subs)
        ]

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        p = params or {}
        if "loom.service_accounts" in sql:  # one name, read again under its claim
            live = [r for r in self._made_by(p["subs"]) if r["revoked_at"] is None]
            return next((r for r in live if r["name"] == p["name"]), None)
        return revocation_read(self.revocations, sql, params)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        p = params or {}
        if "loom.revoked_tokens" in sql:  # the refresh, a subject's keys, the list
            keys = p.get("keys")
            return [r for k, r in self.revoked().items() if keys is None or k in keys]
        if "loom.service_accounts" in sql:
            return [{**r, "live": r["revoked_at"] is None} for r in self._made_by(p["subs"])]
        if "loom.oauth_state" in sql:
            assert p["t"] == "session_refresh"
            now = int(time.time())
            live = {s["family"] for s in self.sessions if s["subject"] == p["s"] and s["exp"] > now}
            return [{"family": f} for f in sorted(live)]
        if "loom.namespaces" in sql:  # the spellings tied to a subject
            if self.directory_down:
                raise ConnectionError("ClickHouse is away")
            sub = p["sub"].lower()
            return [
                a
                for a in self.accounts
                if a["org"] == p["org"] and sub in (a["owner_email"], a["contact_email"])
            ]
        raise AssertionError(f"unexpected read: {sql}")

    async def insert(self, table: str, rows: list[dict[str, Any]], *_: Any, **__: Any) -> None:
        if table == "loom.revoked_tokens":
            self.revocations.extend(dict(r) for r in rows)
        elif table == "loom.service_accounts":
            if any(r["name"] in self.busy_names for r in rows):
                raise GrantBusyError(f"another change to the token name {rows[0]['name']!r}")
            self.tokens.extend(dict(r) for r in rows)
        elif table == "loom.audit_log":
            self.audits.extend({**r, "subject": json.loads(r["subject"])} for r in rows)
        else:
            raise AssertionError(f"unexpected insert into {table}")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    from server.insights.app import admin_routes

    s = _Store()
    monkeypatch.setattr("server.core.ch.get_client", lambda: s)
    monkeypatch.setattr("server.observability.audit.get_client", lambda: s)
    monkeypatch.setattr(type(admin_routes.settings), "auth_actually_required", lambda self: False)
    return s


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_account_routes.router)
    return TestClient(app)


def _lee_with_tokens_and_sessions(store: _Store) -> None:
    """lee made `laptop`, whose token made `child`; kim made `ci`. lee has a
    live sign-in session, one a logout ended, and one whose refresh token ran
    out; kim has one."""
    store.tokens += [
        _token("laptop", "lee"),
        _token("child", "sa:laptop-abc123", lineage=["lee", "sa:laptop-abc123"]),
        _token("ci", "kim"),
    ]
    later, earlier = int(time.time()) + 86400, int(time.time()) - 60
    store.sessions += [
        {"subject": "lee", "family": "s-live", "exp": later},
        {"subject": "lee", "family": "s-ended", "exp": later},
        {"subject": "lee", "family": "s-lapsed", "exp": earlier},
        {"subject": "kim", "family": "s-kim", "exp": later},
    ]
    asyncio.run(revocation.revoke("s-ended", reason="logout", revoked_by="oauth"))


def test_a_subject_revoked_for_good_takes_its_sessions_and_the_tokens_it_made(
    store: _Store,
) -> None:
    _lee_with_tokens_and_sessions(store)
    lee = mint_token("lee", "acme", ["ns"], ["writer"])  # a sign-in session's token
    laptop = mint_token("sa:laptop-abc123", "acme", ["ns"], ["writer"], creator="lee")
    kim = mint_token("kim", "acme", ["ns"], ["writer"])

    body = {"org": "acme", "subject": "lee", "reason": "offboarded"}
    r = _client().post("/v1/admin/revoke_subject", json=body)

    assert r.status_code == 200, r.text
    out = r.json()
    assert {k: out[k] for k in ("subjects", "org", "reason", "sessions", "tokens")} == {
        "subjects": ["lee"],
        "org": "acme",
        "reason": "offboarded",
        "sessions": 1,  # the live one; the ended and the lapsed are not ended by it
        "tokens": 2,  # laptop, and child below it
    }
    row = store.revoked()["lee"]
    assert row["issued_before"] is None and row["revoked_by"] == "admin:local"
    assert row["expires_at"] == datetime(2105, 12, 31)  # for good: as long as the table holds it
    for token in (lee, laptop):
        with pytest.raises(TokenError, match="revoked"):
            verify_token(token)
    assert verify_token(kim).subject == "kim"
    heads = store.heads()
    assert {n for n, t in heads.items() if t["revoked_at"] is not None} == {"laptop", "child"}
    assert {heads[n]["revoked_by"] for n in ("laptop", "child")} == {"admin:local"}
    (audit,) = store.audits
    assert audit["event_type"] == "token.revoked" and audit["org"] == "acme"
    assert audit["actor"] == "admin:local"
    assert {
        k: audit["subject"][k] for k in ("matched_claim", "token_id", "sessions", "tokens")
    } == {
        "matched_claim": "subject",
        "token_id": "lee",
        "sessions": 1,
        "tokens": 2,
    }


def test_a_repeat_answers_the_same_and_marks_nothing_more(store: _Store) -> None:
    _lee_with_tokens_and_sessions(store)
    c, body = _client(), {"org": "acme", "subject": "lee"}
    first = c.post("/v1/admin/revoke_subject", json=body).json()
    marked = len(store.tokens)
    again = c.post("/v1/admin/revoke_subject", json=body).json()
    assert (again["sessions"], again["tokens"]) == (first["sessions"], first["tokens"]) == (1, 2)
    assert len(store.tokens) == marked
    assert store.revoked()["lee"]["issued_before"] is None
    assert [a["event_type"] for a in store.audits] == ["token.revoked"] * 2


def test_only_a_platform_admin_revokes_or_lists(store: _Store, monkeypatch) -> None:
    from server.insights.app import admin_routes

    monkeypatch.setattr(type(admin_routes.settings), "auth_actually_required", lambda self: True)
    writer = mint_token("lee", "acme", ["lee"], ["writer"])
    org_admin = mint_token("boss", "acme", ["*"], ["org_admin"])
    platform = mint_token("ops", "acme", ["*"], ["platform_admin"])
    c, body = _client(), {"org": "acme", "subject": "kim"}
    assert c.post("/v1/admin/revoke_subject", json=body).status_code == 401
    for token in (writer, org_admin):
        auth = {"Authorization": f"Bearer {token}"}
        assert c.post("/v1/admin/revoke_subject", json=body, headers=auth).status_code == 403
        assert c.get("/v1/admin/revocations", headers=auth).status_code == 403
    assert store.revocations == [] and store.audits == []
    auth = {"Authorization": f"Bearer {platform}"}
    r = c.post("/v1/admin/revoke_subject", json=body, headers=auth)
    assert r.status_code == 200 and store.revoked()["kim"]["revoked_by"] == "admin:ops"
    assert store.audits[0]["actor"] == "admin:ops"
    listed = c.get("/v1/admin/revocations", params={"subject": "kim"}, headers=auth)
    assert [row["jti"] for row in listed.json()["revocations"]] == ["kim"]


@pytest.mark.parametrize(
    "body",
    [
        {"org": "acme", "subject": "user:u-1"},  # a grant principal: no token's sub
        {"org": "acme", "subject": "everyone"},
        {"org": "acme", "subject": "acme/User:u-1"},  # an end user's key is lowercase
        {"org": "acme", "subject": "other/user:u-1"},  # another org's end user
        {"org": "acme", "subject": "  "},
        {"org": "acme", "subject": "a\x00b"},
        {"org": "*", "subject": "lee"},
        {"org": "", "subject": "lee"},
        {"org": "acme", "subjects": "lee"},  # a misspelled field
        {"org": "acme", "subject": "lee", "expires_at": "2027-01-01T00:00:00Z"},
    ],
)
def test_a_revocation_that_would_cut_off_no_one_writes_nothing(
    store: _Store, body: dict[str, Any]
) -> None:
    r = _client().post("/v1/admin/revoke_subject", json=body)
    assert r.status_code == 422, r.text
    assert store.revocations == [] and store.audits == []


def test_an_end_user_is_revoked_in_their_own_org(store: _Store) -> None:
    body = {"org": "acme", "subject": "acme/user:u-1"}
    out = _client().post("/v1/admin/revoke_subject", json=body).json()
    assert (out["sessions"], out["tokens"]) == (0, 0)
    assert "acme/user:u-1" in store.revoked()
    assert store.audits[0]["org"] == "acme"


def test_an_email_reaches_the_login_handle_its_account_signs_in_with(store: _Store) -> None:
    """A control plane knows the person's email; their sign-in tokens carry the
    handle of the account an SSO sign-in with it made, which is revoked too."""
    store.accounts.append(
        {"org": "acme", "owner_email": "u-lee-1a2b", "contact_email": "lee@acme.io"}
    )
    store.tokens.append(_token("laptop", "u-lee-1a2b"))
    store.sessions.append(
        {"subject": "u-lee-1a2b", "family": "s-1", "exp": int(time.time()) + 3600}
    )
    signed_in = mint_token("u-lee-1a2b", "acme", ["u-lee-1a2b"], ["admin"])
    by_email = mint_token("lee@acme.io", "acme", ["u-lee-1a2b"], ["writer"])

    body = {"org": "acme", "subject": "lee@acme.io"}
    out = _client().post("/v1/admin/revoke_subject", json=body).json()

    assert out["subjects"] == ["lee@acme.io", "u-lee-1a2b"]
    assert (out["sessions"], out["tokens"]) == (1, 1)
    for token in (signed_in, by_email):
        with pytest.raises(TokenError, match="revoked"):
            verify_token(token)
    assert [a["subject"]["token_id"] for a in store.audits] == ["lee@acme.io", "u-lee-1a2b"]
    assert {a["org"] for a in store.audits} == {"acme"}
    # Only the org named resolves spellings: elsewhere the email stands alone.
    other = _client().post("/v1/admin/revoke_subject", json={**body, "org": "other"}).json()
    assert other["subjects"] == ["lee@acme.io"]


def test_a_directory_that_cannot_be_read_revokes_nothing(store: _Store) -> None:
    store.directory_down = True
    with pytest.raises(ConnectionError):
        _client().post("/v1/admin/revoke_subject", json={"org": "acme", "subject": "lee@acme.io"})
    assert store.revocations == [] and store.audits == []


def test_marking_that_does_not_finish_leaves_the_revocation_and_its_audit(
    store: _Store,
) -> None:
    _lee_with_tokens_and_sessions(store)
    store.busy_names = {"child"}
    with pytest.raises(GrantBusyError):
        _client().post("/v1/admin/revoke_subject", json={"org": "acme", "subject": "lee"})
    assert store.revoked()["lee"]["issued_before"] is None  # in force all the same
    (audit,) = store.audits
    assert audit["subject"]["token_id"] == "lee"
    store.busy_names = set()  # the retry finishes it
    out = _client().post("/v1/admin/revoke_subject", json={"org": "acme", "subject": "lee"})
    assert out.json()["tokens"] == 2
    assert all(t["revoked_at"] is not None for n, t in store.heads().items() if n != "ci")


def test_the_list_narrows_to_one_subjects_rows(store: _Store) -> None:
    async def seed() -> None:
        await revocation.revoke("lee", reason="offboarded", revoked_by="cli:root")
        cutoff = datetime.now(UTC)
        await revocation.cut_off("lee", issued_before=cutoff, reason="bot replaced", revoked_by="x")
        await revocation.scim_revoke("lee", reason="scim: deactivated", revoked_by="scim")
        await revocation.revoke("kim", reason="left", revoked_by="cli:root")

    asyncio.run(seed())
    c = _client()
    mine = c.get("/v1/admin/revocations", params={"subject": "lee"}).json()["revocations"]
    assert sorted((row["jti"], row["kind"]) for row in mine) == [
        ("lee", "cutoff"),
        ("lee", "revocation"),
        ("lee", "scim"),
    ]
    every = c.get("/v1/admin/revocations").json()["revocations"]
    assert sorted({row["jti"] for row in every}) == ["kim", "lee"] and len(every) == 4


def test_the_cli_revokes_a_subject_for_good_as_the_route_does(
    store: _Store, capsys: pytest.CaptureFixture[str]
) -> None:
    _lee_with_tokens_and_sessions(store)
    assert cli.main(["revoke-token", "--subject", "lee", "--reason", "offboarded", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["matched_claim"], out["sessions"], out["tokens"]) == ("subject", 1, 2)
    assert {n for n, t in store.heads().items() if t["revoked_at"] is not None} == {
        "laptop",
        "child",
    }
    (audit,) = store.audits
    assert audit["org"] == "-" and audit["actor"].startswith("cli:")  # every org's
    assert audit["subject"]["tokens"] == 2


def test_a_cli_revocation_that_lapses_leaves_the_token_list_alone(
    store: _Store, capsys: pytest.CaptureFixture[str]
) -> None:
    _lee_with_tokens_and_sessions(store)
    marked = len(store.tokens)
    rc = cli.main(["revoke-token", "--subject", "lee", "--expires-at", "2099-01-01T00:00:00Z"])
    assert rc == 0 and "sign-in session" not in capsys.readouterr().out
    assert len(store.tokens) == marked  # they work again once it lapses
    assert store.revoked()["lee"]["expires_at"] == datetime(2099, 1, 1)
    assert "tokens" not in store.audits[0]["subject"]


def test_a_cli_revocation_whose_marking_fails_says_run_it_again(
    store: _Store, capsys: pytest.CaptureFixture[str]
) -> None:
    _lee_with_tokens_and_sessions(store)
    store.busy_names = {"laptop"}
    assert cli.main(["revoke-token", "--subject", "lee"]) == 1
    err = capsys.readouterr().err
    assert "revocation not finished, run it again" in err and "laptop" in err
    assert "lee" in store.revoked()
