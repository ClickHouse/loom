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
from datetime import UTC, datetime, timedelta
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


def _token(
    name: str,
    created_by: str,
    *,
    lineage: list[str] | None = None,
    created_at: datetime = datetime(2026, 1, 1),
) -> dict[str, Any]:
    """A loom.service_accounts row: a token `created_by` made, never expiring."""
    return {
        "org": "acme",
        "name": name,
        "sa_subject": f"sa:{name}-abc123",
        "namespaces": ["ns"],
        "roles": ["writer"],
        "ttl_days": 0,
        "created_by": created_by,
        "created_at": created_at,
        "expires_at": None,
        "revoked_at": None,
        "revoked_by": "",
        "version": 1,
        "kind": "mcp",
        "client": "claude-code",
        "inherit_creator_grants": True,
        "ns_roles": '{"ns": "writer"}',
        "lineage": [created_by] if lineage is None else lineage,
        "saves_to": "",
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

    connected = False

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
        if "reason, revoked_by, issued_before" in sql:  # a subject's own row
            return self.revoked().get(p["j"])
        return revocation_read(self.revocations, sql, params)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        p = params or {}
        if "loom.revoked_tokens" in sql:  # the refresh, a subject's keys, the list
            keys, since = p.get("keys"), p.get("since")
            rows = [r for k, r in self.revoked().items() if keys is None or k in keys]
            if since is not None:
                rows = [r for r in rows if r["revoked_at"] >= since.replace(tzinfo=None)]
            return rows[: p["lim"]] if "lim" in p else rows
        if "loom.service_accounts" in sql:
            return [{**r, "live": r["revoked_at"] is None} for r in self._made_by(p["subs"])]
        if "loom.oauth_state" in sql:  # each live session, by when its newest token was written
            assert p["t"] == "session_refresh"
            now, newest = int(time.time()), {}
            for s in self.sessions:
                if s["subject"] == p["s"] and s["exp"] > now:
                    newest[s["family"]] = max(newest.get(s["family"], 0), s.get("at", now - 3600))
            return [{"family": f, "at": at} for f, at in sorted(newest.items())]
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
    from tests.unit.claims_fake import memory_claims

    s = _Store()
    monkeypatch.setattr("server.core.ch.get_client", lambda: s)
    monkeypatch.setattr("server.observability.audit.get_client", lambda: s)
    monkeypatch.setattr(type(admin_routes.settings), "auth_actually_required", lambda self: False)
    memory_claims(monkeypatch)  # the subject's lease, without a lease table
    return s


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_account_routes.router)
    return TestClient(app)


def test_a_busy_token_name_does_not_leave_the_login_subject_active(store: _Store) -> None:
    store.accounts.append(
        {"org": "acme", "owner_email": "account-id", "contact_email": "lee@example.com"}
    )
    store.tokens.append(_token("laptop", "lee@example.com"))
    store.busy_names.add("laptop")
    token = mint_token("account-id", "acme", ["ns"], ["platform_admin"])
    with pytest.raises(GrantBusyError):
        _client().post(
            "/v1/admin/revoke_subject", json={"org": "acme", "subject": "lee@example.com"}
        )
    with pytest.raises(TokenError):
        verify_token(token)
    assert {row["subject"]["token_id"] for row in store.audits} == {"lee@example.com", "account-id"}


def test_a_temporary_suspension_does_not_permanently_mark_newer_tokens(store: _Store) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    issued = now - timedelta(minutes=1)
    store.tokens.append(_token("new", "lee", created_at=issued.replace(tzinfo=None)))
    asyncio.run(revocation.revoke("lee", expires_at=now + timedelta(days=1)))
    response = _client().post(
        "/v1/admin/revoke_subject",
        json={
            "org": "acme",
            "subject": "lee",
            "issued_before": (now - timedelta(hours=1)).isoformat(),
        },
    )
    assert response.status_code == 200
    assert store.heads()["new"]["revoked_at"] is None
    for row in store.revocations:
        if row["jti"] == "lee":
            row["expires_at"] = now.replace(tzinfo=None) - timedelta(seconds=1)
    revocation._recent.clear()
    asyncio.run(revocation.refresh())
    assert not revocation.is_revoked("lee", issued_at=int(issued.timestamp()))


def test_direct_cutoff_writers_cannot_be_overwritten_by_a_weaker_admin_cutoff(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import subject_revocation

    now = datetime.now(UTC).replace(microsecond=0)
    weaker, stronger = now - timedelta(hours=1), now - timedelta(minutes=1)
    stored = revocation.stored

    async def interleave() -> None:
        read, release = asyncio.Event(), asyncio.Event()

        async def paused(key: str) -> Any:
            snapshot = await stored(key)
            if not read.is_set():
                read.set()
                await release.wait()
            return snapshot

        monkeypatch.setattr(revocation, "stored", paused)
        admin = asyncio.create_task(subject_revocation.record("sa:bot", by="admin", at=weaker))
        await read.wait()
        replacement = asyncio.create_task(
            revocation.cut_off(
                "sa:bot", issued_before=stronger, reason="replacement", revoked_by="admin"
            )
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(admin, replacement)

    asyncio.run(interleave())
    revocation._recent.clear()
    asyncio.run(revocation.refresh())
    assert revocation.is_revoked("sa:bot", issued_at=int((now - timedelta(minutes=30)).timestamp()))
    assert store.revoked()[revocation.CUTOFF_PREFIX + "sa:bot"][
        "issued_before"
    ] == stronger.replace(tzinfo=None)


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
    assert out["issued_before"] is None and out["outright"] == ["lee"]
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
    marked, written = len(store.tokens), len(store.revocations)
    again = c.post("/v1/admin/revoke_subject", json=body).json()
    assert again == first and (again["sessions"], again["tokens"]) == (1, 2)
    assert (len(store.tokens), len(store.revocations)) == (marked, written)
    assert store.revoked()["lee"]["issued_before"] is None
    assert [a["event_type"] for a in store.audits] == ["token.revoked"] * 2
    assert [a["subject"]["kept"] for a in store.audits] == [False, True]


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
        {"org": "acme", "subject": "lee", "issued_before": "2026-01-31T12:00:00"},  # no offset
        {"org": "acme", "subject": "lee", "issued_before": "yesterday"},
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


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def test_a_cutoff_ends_what_came_before_it_and_lets_a_later_sign_in_live(store: _Store) -> None:
    """A platform's deprovisioning passes `issued_before`: everything the person
    held before it is refused for good, a sign-in after it (a re-invitation)
    works, and the login handle the email signs in with is covered as the email is."""
    store.accounts.append(
        {"org": "acme", "owner_email": "u-lee-1a2b", "contact_email": "lee@acme.io"}
    )
    now = int(time.time())
    t = now - 600  # for ten minutes ago, so tokens made after it exist already
    cut = t + 1 + revocation.CLOCK_SKEW_S  # its second, and a replica clock running ahead
    store.tokens += [
        _token("old", "u-lee-1a2b", created_at=_at(t - 60).replace(tzinfo=None)),
        _token("new", "u-lee-1a2b", created_at=_at(cut + 60).replace(tzinfo=None)),
    ]
    store.sessions += [
        {"subject": "u-lee-1a2b", "family": "s-old", "exp": now + 86400, "at": t - 60},
        {"subject": "u-lee-1a2b", "family": "s-new", "exp": now + 86400, "at": cut + 60},
    ]
    spellings = ("u-lee-1a2b", "lee@acme.io")
    before = [mint_token(s, "acme", ["ns"], ["writer"], issued_at=t - 60) for s in spellings]
    after = [mint_token(s, "acme", ["ns"], ["writer"], issued_at=cut + 60) for s in spellings]

    body = {"org": "acme", "subject": "lee@acme.io", "issued_before": _at(t).isoformat()}
    out = _client().post("/v1/admin/revoke_subject", json=body).json()

    assert out["subjects"] == ["lee@acme.io", "u-lee-1a2b"] and out["outright"] == []
    assert datetime.fromisoformat(out["issued_before"]) == _at(cut)
    assert (out["sessions"], out["tokens"]) == (1, 1)  # the session refreshed since lives on
    for token in before:
        with pytest.raises(TokenError, match="revoked"):
            verify_token(token)
    assert [verify_token(token).subject for token in after] == list(spellings)
    # A sign-in mints only once ClickHouse says the subject is not revoked for it.
    signs_in = revocation.subjects_revoked(list(spellings), issued_at=cut)
    assert not asyncio.run(signs_in)
    assert asyncio.run(revocation.subjects_revoked(["u-lee-1a2b"], issued_at=cut - 1))
    heads = store.heads()
    assert heads["old"]["revoked_at"] is not None and heads["new"]["revoked_at"] is None
    recorded = [datetime.fromisoformat(a["subject"]["issued_before"]) for a in store.audits]
    assert recorded == [_at(cut).replace(tzinfo=None)] * 2  # naive UTC, as the row keeps it


def test_a_cutoff_never_lifts_an_outright_revocation(store: _Store) -> None:
    c = _client()
    c.post("/v1/admin/revoke_subject", json={"org": "acme", "subject": "lee"})  # a ban
    ban = store.revoked()["lee"]
    body = {"org": "acme", "subject": "lee", "issued_before": _at(int(time.time())).isoformat()}
    out = c.post("/v1/admin/revoke_subject", json=body).json()
    assert out["outright"] == ["lee"] and store.revoked()["lee"] == ban  # untouched
    assert revocation.CUTOFF_PREFIX + "lee" in store.revoked()  # recorded beside it
    with pytest.raises(TokenError, match="revoked"):  # minted after the cutoff: still refused
        verify_token(mint_token("lee", "acme", ["ns"], ["writer"]))


def test_a_later_cutoff_stays_and_a_repeat_writes_nothing_more(store: _Store) -> None:
    c, now = _client(), int(time.time())
    late = {"org": "acme", "subject": "lee", "issued_before": _at(now - 60).isoformat()}
    first = c.post("/v1/admin/revoke_subject", json=late).json()
    written = len(store.revocations)
    assert c.post("/v1/admin/revoke_subject", json=late).json() == first
    early = {**late, "issued_before": _at(now - 3600).isoformat()}  # a retry sent long ago
    answer = c.post("/v1/admin/revoke_subject", json=early).json()
    assert len(store.revocations) == written
    cut = now - 60 + 1 + revocation.CLOCK_SKEW_S
    assert datetime.fromisoformat(answer["issued_before"]) == _at(cut)  # the one in force
    assert revocation.is_revoked("lee", issued_at=cut - 1)  # still refused up to the later one
    assert not revocation.is_revoked("lee", issued_at=cut)


def test_a_time_boxed_revocation_holds_until_it_lapses_and_the_cutoff_after(
    store: _Store,
) -> None:
    """A suspension (`loom revoke-token --expires-at`) refuses every token of the
    subject until it lapses; a cutoff the request adds never shortens it, and
    holds once it has lapsed."""
    lapses = datetime.now(UTC) + timedelta(days=1)
    asyncio.run(revocation.revoke("lee", expires_at=lapses, reason="paused", revoked_by="cli:op"))
    now = int(time.time())
    t = now - 600
    cut = t + 1 + revocation.CLOCK_SKEW_S
    body = {"org": "acme", "subject": "lee", "issued_before": _at(t).isoformat()}
    out = _client().post("/v1/admin/revoke_subject", json=body).json()
    assert out["outright"] == ["lee"]  # the suspension is in force
    assert store.revoked()["lee"]["expires_at"] == lapses.replace(tzinfo=None)  # untouched
    after = mint_token("lee", "acme", ["ns"], ["writer"], issued_at=cut + 60)
    before = mint_token("lee", "acme", ["ns"], ["writer"], issued_at=t - 60)
    with pytest.raises(TokenError, match="revoked"):
        verify_token(after)  # refused while the suspension lasts
    for r in store.revocations:  # the day goes by: the suspension lapses
        if r["jti"] == "lee":
            r["expires_at"] = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    revocation._recent.clear()  # by then this process has long let go of its own write
    asyncio.run(revocation.refresh())
    assert verify_token(after).subject == "lee"
    with pytest.raises(TokenError, match="revoked"):
        verify_token(before)  # the cutoff holds after it


def test_an_instant_ahead_of_the_server_counts_as_now(store: _Store) -> None:
    """A caller's clock running ahead never sets a cutoff that refuses the
    subject's next sign-ins, nor is refused: the cutoff is now, on any replica."""
    ahead = datetime.now(UTC) + timedelta(days=365)
    body = {"org": "acme", "subject": "lee", "issued_before": ahead.isoformat()}
    out = _client().post("/v1/admin/revoke_subject", json=body).json()
    applied = datetime.fromisoformat(out["issued_before"])
    soon = datetime.now(UTC) + timedelta(seconds=2 + revocation.CLOCK_SKEW_S)
    assert applied <= soon and out["outright"] == []


def test_a_revocation_of_a_subject_another_is_changing_is_refused(store, monkeypatch) -> None:
    from server.core import leases

    async def taken(name: str, **_: Any) -> None:
        return None  # another holder keeps the subject's lease

    monkeypatch.setattr(leases, "take", taken)
    with pytest.raises(GrantBusyError, match="retry"):
        _client().post("/v1/admin/revoke_subject", json={"org": "acme", "subject": "lee"})
    assert store.revocations == []


def test_an_email_in_any_case_reaches_its_spellings_as_stored(store: _Store) -> None:
    """A revocation matches a token's subject exactly: the address as sent, its
    lower case (how accounts keep it) and the handle are all revoked."""
    store.accounts.append(
        {"org": "acme", "owner_email": "u-lee-1a2b", "contact_email": "lee@acme.io"}
    )
    body = {"org": "acme", "subject": "Lee@Acme.io"}
    out = _client().post("/v1/admin/revoke_subject", json=body).json()
    assert out["subjects"] == ["Lee@Acme.io", "lee@acme.io", "u-lee-1a2b"]
    with pytest.raises(TokenError, match="revoked"):
        verify_token(mint_token("lee@acme.io", "acme", ["ns"], ["writer"]))


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
    assert "revoked 'lee', but marking the tokens it made revoked did not finish" in err
    assert "laptop" in err and "run it again" in err
    assert "lee" in store.revoked()


def test_a_cli_revocation_the_lease_store_refuses_writes_nothing_and_says_so(
    store: _Store, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import leases

    async def away(name: str, **_: Any) -> None:
        raise leases.LeaseUnavailable("the lease store did not answer")

    monkeypatch.setattr(leases, "take", away)
    assert cli.main(["revoke-token", "--subject", "lee"]) == 1
    err = capsys.readouterr().err
    assert "revocation not recorded, nothing was written" in err and "did not answer" in err
    assert store.revocations == [] and store.audits == []


def test_a_cli_repeat_says_already_revoked_and_keeps_the_first_reason(
    store: _Store, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["revoke-token", "--subject", "lee", "--reason", "offboarded"]) == 0
    written = len(store.revocations)
    capsys.readouterr()
    assert cli.main(["revoke-token", "--subject", "lee", "--reason", "again"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("already revoked subject 'lee' for good") and ": offboarded" in out
    assert len(store.revocations) == written and store.revoked()["lee"]["reason"] == "offboarded"
    assert cli.main(["revoke-token", "--subject", "lee", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["already_revoked"] is True


def test_the_list_is_bounded_and_narrowed_by_time(store: _Store) -> None:
    async def seed() -> None:
        for n in range(3):
            await revocation.revoke(f"sa:old-{n}", reason="r", revoked_by="x")

    asyncio.run(seed())
    c = _client()
    assert len(c.get("/v1/admin/revocations", params={"limit": 2}).json()["revocations"]) == 2
    assert c.get("/v1/admin/revocations", params={"limit": 0}).status_code == 422
    later = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    assert c.get("/v1/admin/revocations", params={"since": later}).json()["revocations"] == []
    assert (
        c.get("/v1/admin/revocations", params={"since": "2026-01-01T00:00:00"}).status_code == 422
    )
    padded = c.get("/v1/admin/revocations", params={"subject": " sa:old-1 "}).json()
    assert [row["jti"] for row in padded["revocations"]] == ["sa:old-1"]


def test_recording_a_later_alias_failure_still_audits_earlier_revocations(store, monkeypatch):
    store.accounts.append(
        {"org": "acme", "owner_email": "account-id", "contact_email": "lee@example.com"}
    )
    insert = store.insert

    async def fail_login_subject(table, rows, **kwargs):
        if table == "loom.revoked_tokens" and any(r["jti"] == "account-id" for r in rows):
            raise ConnectionError("revocation write unavailable")
        return await insert(table, rows, **kwargs)

    monkeypatch.setattr(store, "insert", fail_login_subject)
    with pytest.raises(ConnectionError):
        _client().post(
            "/v1/admin/revoke_subject", json={"org": "acme", "subject": "lee@example.com"}
        )
    assert set(store.revoked()) == {"lee@example.com"}
    assert [row["subject"]["token_id"] for row in store.audits] == ["lee@example.com"]


@pytest.mark.parametrize("cutoff", [False, True])
def test_a_committed_revocation_is_audited_without_waiting_for_refresh(store, monkeypatch, cutoff):
    from contextlib import asynccontextmanager

    from server.auth import subject_revocation
    from server.core import leases

    holding = leases.holding

    @asynccontextmanager
    async def brief_claim(name, **kwargs):
        async with holding(name, ttl_s=0.05, wait_s=0) as held:
            yield held

    async def blocked_refresh():
        await asyncio.Event().wait()

    monkeypatch.setattr(leases, "holding", brief_claim)
    monkeypatch.setattr(revocation, "refresh", blocked_refresh)
    at = datetime.now(UTC) - timedelta(hours=1) if cutoff else None
    asyncio.run(subject_revocation.revoke_in_org("acme", "lee", by="admin", at=at))
    assert len(store.revocations) == 1
    assert [row["subject"]["token_id"] for row in store.audits] == ["lee"]


@pytest.mark.parametrize("cutoff", [False, True])
def test_retaining_a_revocation_updates_the_local_snapshot(store, monkeypatch, cutoff):
    from server.auth import subject_revocation

    at = datetime.now(UTC) - timedelta(hours=1) if cutoff else None
    asyncio.run(subject_revocation.record("lee", by="other-replica", at=at))
    monkeypatch.setattr(revocation, "_revoked_jtis", set())
    monkeypatch.setattr(revocation, "_revoked_before", {})
    monkeypatch.setattr(revocation, "_cut_off", {})
    monkeypatch.setattr(revocation, "_recent", [])
    done = asyncio.run(subject_revocation.revoke_in_org("acme", "lee", by="admin", at=at))
    assert done[0][1].kept
    assert revocation.is_revoked(
        "lee", issued_at=int((datetime.now(UTC) - timedelta(days=1)).timestamp())
    )


@pytest.mark.parametrize("claim_flag", ["--subject", "--jti"])
@pytest.mark.parametrize("narrowing", ["cutoff", "expiry"])
@pytest.mark.parametrize("subject", ["lee", " lee "])
def test_the_cli_cannot_restore_tokens_whose_names_were_freed(
    store, capsys, claim_flag, narrowing, subject
):
    from server.auth.jwt import _check_not_revoked

    now = datetime.now(UTC).replace(microsecond=0)
    issued = now - timedelta(hours=1)
    store.tokens.append(_token("laptop", "lee", created_at=issued.replace(tzinfo=None)))
    asyncio.run(revocation.refresh())
    assert cli.main(["revoke-token", "--subject", "lee"]) == 0
    capsys.readouterr()
    before = dict(store.revoked()["lee"])
    option = (
        ["--issued-before", (now - timedelta(hours=2)).isoformat()]
        if narrowing == "cutoff"
        else ["--expires-at", (now + timedelta(days=1)).isoformat()]
    )
    rc = cli.main(["revoke-token", claim_flag, subject, *option])
    assert store.heads()["laptop"]["revoked_at"] is not None
    with pytest.raises(TokenError, match="revoked"):
        _check_not_revoked(
            {"sub": "sa:laptop-abc123", "creator": "lee", "iat": int(issued.timestamp())}
        )
    assert rc == 2
    assert store.revoked()["lee"] == before


def test_the_cli_allows_new_tokens_after_a_safe_cutoff(store, capsys):
    from server.cli.access import _revocation_floor
    from server.core.datetimes import as_aware_utc

    assert cli.main(["revoke-token", "--subject", "lee"]) == 0
    capsys.readouterr()
    floor = as_aware_utc(_revocation_floor(store.revoked()["lee"]))
    assert cli.main(["revoke-token", "--subject", "lee", "--issued-before", floor.isoformat()]) == 0
    assert revocation.is_revoked("lee", issued_at=int(floor.timestamp()) - 1)
    assert not revocation.is_revoked("lee", issued_at=int(floor.timestamp()))
