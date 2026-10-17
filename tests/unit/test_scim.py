"""SCIM deprovisioning: the filter and PatchOp parsing, the bearer gate, and
the deactivate / reactivate state machine with its retries."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import login, oauth, revocation, scim, scim_routes
from server.auth.scim_routes import ScimError, parse_active, parse_filter, patched_active
from server.observability import audit
from server.settings import settings
from tests.unit.schema_ddl import column_names

# The real read; the suite's conftest stands a stub in for it.
_DEACTIVATED = scim.deactivated
_TOKEN = "scim-test-token-" + "x" * 32


# ── parsing ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ('userName eq "lee@example.com"', "lee@example.com"),
        ('  USERNAME EQ "Lee@Example.com" ', "Lee@Example.com"),
        (r'userName eq "a\"b@example.com"', 'a"b@example.com'),
    ],
)
def test_the_username_filter_is_parsed(text: str, value: str) -> None:
    assert parse_filter(text) == value


@pytest.mark.parametrize(
    "text",
    [
        'userName co "lee"',
        'emails.value eq "lee@example.com"',
        'userName eq "a" or userName eq "b"',
        "userName eq lee@example.com",
        'userName eq "bad \\x"',
        "",
    ],
)
def test_any_other_filter_is_an_invalid_filter(text: str) -> None:
    with pytest.raises(ScimError) as e:
        parse_filter(text)
    assert (e.value.status, e.value.scim_type) == (400, "invalidFilter")


@pytest.mark.parametrize(
    ("body", "active"),
    [
        ({"Operations": [{"op": "replace", "value": {"active": False}}]}, False),
        ({"Operations": [{"op": "Replace", "path": "active", "value": "False"}]}, False),
        ({"Operations": [{"op": "add", "path": "active", "value": True}]}, True),
        ({"Operations": [{"op": "replace", "path": "name.givenName", "value": "Lee"}]}, None),
        ({"Operations": [{"op": "remove", "path": "title"}]}, None),
        ({"Operations": []}, None),
    ],
)
def test_a_patch_sets_active_and_drops_the_rest(body: dict[str, Any], active: bool | None) -> None:
    assert patched_active(body) is active


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"Operations": [{"op": "remove", "path": "active"}]},
        {"Operations": [{"op": "replace", "path": "active", "value": "no"}]},
        {"Operations": ["active"]},
    ],
)
def test_a_patch_that_cannot_set_active_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ScimError) as e:
        patched_active(body)
    assert e.value.status == 400


def test_active_takes_booleans_and_their_strings() -> None:
    assert parse_active(True) is True and parse_active("true") is True
    assert parse_active(False) is False and parse_active(" FALSE ") is False
    with pytest.raises(ScimError):
        parse_active(0)


# ── the state machine, over an in-memory table ────────────────────────────


class _Table:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def newest(self, uid: str) -> dict[str, Any] | None:
        rows = [r for r in self.rows if r["id"] == uid]
        return dict(max(rows, key=lambda r: r["version"])) if rows else None

    def live(self) -> list[dict[str, Any]]:
        heads = [self.newest(uid) for uid in {r["id"] for r in self.rows}]
        return sorted((r for r in heads if r and not r["deleted"]), key=lambda r: r["user_name"])

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        if "count()" in sql:
            return {"n": len(self.live())}
        row = self.newest(params["id"]) if params else None
        if "max(version)" in sql:
            return {"v": row["version"] if row else 0}
        if "active = 0" in sql:
            return {"x": 1} if row and not row["active"] else None
        return row

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self.live()[params["skip"] : params["skip"] + params["count"]]

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert table == scim.TABLE
        self.rows.extend(dict(r) for r in rows)


class _World:
    """The table, and what deactivation does to revocations and the audit log."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.table = _Table()
        self.accounts: dict[str, login.Identity] = {}
        self.outright: dict[str, str] = {}  # subject -> reason
        self.narrowed: list[tuple[str, datetime]] = []
        self.audits: list[tuple[str, dict[str, Any]]] = []
        self.fail_revoke = False

        async def resolve(email: str) -> login.Identity | None:
            return self.accounts.get(email)

        async def revoked_outright(subject: str) -> bool:
            return subject in self.outright

        async def revoke(subject: str, *, reason: str, revoked_by: str) -> dict[str, Any]:
            if self.fail_revoke:
                raise RuntimeError("ClickHouse is away")
            self.outright[subject] = reason
            return {}

        async def narrow(
            key: str, *, reason: str, issued_before: datetime, revoked_by: str
        ) -> bool:
            if self.outright.get(key) != reason:
                return False
            del self.outright[key]
            self.narrowed.append((key, issued_before))
            return True

        async def emit(event: str, **kw: Any) -> str:
            self.audits.append((event, kw))
            return ""

        @asynccontextmanager
        async def held(uid: str) -> AsyncIterator[None]:
            yield

        async def no_wait(_s: float) -> None:
            return None

        monkeypatch.setattr(scim, "get_client", lambda: self.table)
        monkeypatch.setattr(scim, "_held", held)
        monkeypatch.setattr(scim, "asyncio", SimpleNamespace(sleep=no_wait))
        monkeypatch.setattr(login, "resolve_identity_for_email", resolve)
        monkeypatch.setattr(revocation, "revoked_outright", revoked_outright)
        monkeypatch.setattr(revocation, "revoke", revoke)
        monkeypatch.setattr(revocation, "narrow", narrow)
        monkeypatch.setattr(audit, "emit", emit)

    def person(self, email: str, subject: str, org: str = "acme") -> None:
        self.accounts[email] = (subject, org, [subject], ["writer"])

    @property
    def events(self) -> list[str]:
        return [e for e, _ in self.audits]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _World(monkeypatch)


async def test_deactivating_revokes_the_person_once_however_often_it_is_sent(world) -> None:
    world.person("lee@example.com", "lee")
    uid = (await scim.create("Lee@Example.com", active=True))["id"]
    assert uid == scim.user_id("lee@example.com") and world.outright == {}

    row = await scim.set_active(uid, False)
    assert row is not None and not row["active"] and (row["subject"], row["org"]) == ("lee", "acme")
    assert world.outright == {"lee": scim.DEACTIVATED}
    assert world.events == ["auth.scim_deactivated"]
    written = len(world.table.rows)

    await scim.set_active(uid, False)  # the provider's retry
    assert world.events == ["auth.scim_deactivated"] and len(world.table.rows) == written
    assert await _DEACTIVATED("lee@example.com")


async def test_every_row_written_carries_every_column(world) -> None:
    world.person("lee@example.com", "lee")
    uid = (await scim.create("lee@example.com", active=True))["id"]
    await scim.set_active(uid, False)
    await scim.delete(uid)
    assert [sorted(r) for r in world.table.rows] == [sorted(column_names("scim_users"))] * 3


async def test_a_retry_finishes_a_deactivation_whose_revoke_failed(world) -> None:
    world.person("lee@example.com", "lee")
    uid = (await scim.create("lee@example.com", active=True))["id"]
    world.fail_revoke = True
    with pytest.raises(RuntimeError):
        await scim.set_active(uid, False)
    assert not await _DEACTIVATED("lee@example.com")  # nothing recorded short of the revoke
    world.fail_revoke = False
    await scim.set_active(uid, False)
    assert world.outright == {"lee": scim.DEACTIVATED} and await _DEACTIVATED("lee@example.com")


async def test_reactivating_lifts_the_refusal_and_keeps_old_tokens_revoked(world) -> None:
    world.person("lee@example.com", "lee")
    uid = (await scim.create("lee@example.com", active=True))["id"]
    await scim.set_active(uid, False)
    before = time.time()
    row = await scim.set_active(uid, True)
    assert row is not None and row["active"]
    assert not await _DEACTIVATED("lee@example.com")
    [(subject, cutoff)] = world.narrowed
    assert subject == "lee" and cutoff.timestamp() > before  # tokens issued so far stay dead
    assert world.events == ["auth.scim_deactivated", "auth.scim_reactivated"]

    await scim.set_active(uid, True)
    assert len(world.narrowed) == 1 and len(world.events) == 2


async def test_an_operator_revocation_survives_deactivate_and_reactivate(world) -> None:
    world.person("lee@example.com", "lee")
    world.outright["lee"] = "offboarded"
    uid = (await scim.create("lee@example.com", active=True))["id"]
    await scim.set_active(uid, False)
    await scim.set_active(uid, True)
    assert world.outright == {"lee": "offboarded"} and world.narrowed == []


async def test_an_email_with_no_account_is_refused_by_email(world) -> None:
    uid = (await scim.create("new@example.com", active=False))["id"]
    assert await _DEACTIVATED("new@example.com") and world.outright == {}
    assert (await scim.get(uid) or {})["subject"] == ""
    # An account made since (by an operator) is revoked when the retry finds it.
    world.person("new@example.com", "u-new")
    await scim.set_active(uid, False)
    assert world.outright == {"u-new": scim.DEACTIVATED}


async def test_a_deleted_user_is_gone_but_stays_deactivated(world) -> None:
    world.person("lee@example.com", "lee")
    uid = (await scim.create("lee@example.com", active=True))["id"]
    with pytest.raises(scim.Exists):
        await scim.create("LEE@example.com", active=True)
    assert await scim.delete(uid)
    assert await scim.get(uid) is None and not await scim.delete(uid)
    assert await scim.set_active(uid, True) is None
    assert await _DEACTIVATED("lee@example.com")
    # Created again by the provider: assigned again, so active again.
    again = await scim.create("lee@example.com", active=True)
    assert again["id"] == uid and again["active"] and world.narrowed


async def test_a_deactivated_email_is_refused_at_sso_sign_in(monkeypatch) -> None:
    async def deactivated(email: str) -> bool:
        return email == "lee@example.com"

    monkeypatch.setattr(scim, "deactivated", deactivated)
    got = await oauth._account_for("lee@example.com", {}, "")
    assert got == oauth._Denied("this account is deactivated")


async def test_an_unreadable_deactivation_fails_the_sign_in_retryably(monkeypatch) -> None:
    class Away:
        async def query_one(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("ClickHouse is away")

    monkeypatch.setattr(scim, "get_client", lambda: Away())
    with pytest.raises(login.SignInUnavailable):
        await _DEACTIVATED("lee@example.com")


# ── revocation.narrow ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "narrows"),
    [
        ({"reason": scim.DEACTIVATED, "issued_before": None}, True),
        ({"reason": "offboarded", "issued_before": None}, False),
        ({"reason": scim.DEACTIVATED, "issued_before": datetime(2026, 1, 1)}, False),
        (None, False),
    ],
)
async def test_narrow_touches_only_its_own_outright_revocation(
    monkeypatch, stored: dict[str, Any] | None, narrows: bool
) -> None:
    from server.core import ch

    written: list[dict[str, Any]] = []

    class Read:
        async def query_one(self, *_a: Any, **_k: Any) -> dict[str, Any] | None:
            return stored

    async def revoke(key: str, **kw: Any) -> dict[str, Any]:
        written.append({"key": key, **kw})
        return {}

    monkeypatch.setattr(ch, "get_client", lambda: Read())
    monkeypatch.setattr(revocation, "revoke", revoke)
    monkeypatch.setattr(revocation, "_recent", [("revoke", "lee", None, time.monotonic())])
    cutoff = datetime.now(UTC)
    got = await revocation.narrow(
        "lee", reason=scim.DEACTIVATED, issued_before=cutoff, revoked_by="scim"
    )
    assert got is narrows
    if narrows:
        [w] = written
        assert (w["key"], w["issued_before"]) == ("lee", cutoff)
        assert revocation._recent == []  # a refresh cannot lay the outright one back
    else:
        assert written == [] and len(revocation._recent) == 1


# ── the routes ────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, world: _World) -> TestClient:
    monkeypatch.setattr(settings, "scim_token", _TOKEN)
    app = FastAPI()
    scim_routes.attach(app)
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {_TOKEN}"
    return c


@pytest.mark.parametrize("header", [None, "Bearer wrong", f"Basic {_TOKEN}", _TOKEN])
def test_every_route_wants_the_scim_token(client: TestClient, header: str | None) -> None:
    headers = {} if header is None else {"Authorization": header}
    client.headers.pop("Authorization")
    for method, path in [
        ("GET", "/ServiceProviderConfig"),
        ("GET", "/Users"),
        ("DELETE", "/Users/x"),
    ]:
        r = client.request(method, f"/scim/v2{path}", headers=headers)
        assert r.status_code == 401, (method, path)
        assert r.headers["content-type"].startswith("application/scim+json")
        assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


def test_scim_is_not_mounted_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "scim_token", "")
    app = FastAPI()
    scim_routes.attach(app)
    assert TestClient(app).get("/scim/v2/ServiceProviderConfig").status_code == 404


def test_a_provider_creates_finds_and_deactivates_a_person(client: TestClient, world) -> None:
    world.person("lee@example.com", "lee")
    assert client.get("/scim/v2/ServiceProviderConfig").json()["patch"] == {"supported": True}
    found = client.get("/scim/v2/Users", params={"filter": 'userName eq "lee@example.com"'})
    assert found.json()["totalResults"] == 0

    created = client.post(
        "/scim/v2/Users",
        content=b'{"schemas": [], "userName": "Lee@example.com", "name": {"givenName": "Lee"}}',
        headers={"Content-Type": "application/scim+json"},
    )
    assert created.status_code == 201, created.text
    user = created.json()
    uid = user["id"]
    assert user["userName"] == "lee@example.com" and user["active"] is True
    assert created.headers["location"].endswith(f"/scim/v2/Users/{uid}")
    assert client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).status_code == 409

    found = client.get("/scim/v2/Users", params={"filter": 'userName eq "lee@example.com"'})
    assert [u["id"] for u in found.json()["Resources"]] == [uid]
    off = {"Operations": [{"op": "replace", "value": {"active": False}}]}
    patched = client.patch(f"/scim/v2/Users/{uid}", json=off)
    assert patched.status_code == 200 and patched.json()["active"] is False
    assert world.outright == {"lee": scim.DEACTIVATED}

    put = client.put(f"/scim/v2/Users/{uid}", json={"userName": "lee@example.com", "active": True})
    assert put.json()["active"] is True and world.narrowed
    renamed = client.put(f"/scim/v2/Users/{uid}", json={"userName": "x@example.com"})
    assert renamed.status_code == 400 and renamed.json()["scimType"] == "mutability"

    assert client.delete(f"/scim/v2/Users/{uid}").status_code == 204
    assert client.get(f"/scim/v2/Users/{uid}").status_code == 404
    assert client.get("/scim/v2/Users").json()["totalResults"] == 0


def test_bad_requests_answer_scim_errors(client: TestClient) -> None:
    bad_filter = client.get("/scim/v2/Users", params={"filter": 'name co "x"'})
    assert bad_filter.status_code == 400 and bad_filter.json()["scimType"] == "invalidFilter"
    not_json = client.post("/scim/v2/Users", content=b"{")
    assert not_json.status_code == 400 and not_json.json()["scimType"] == "invalidSyntax"
    no_email = client.post("/scim/v2/Users", json={"userName": "lee"})
    assert no_email.status_code == 400 and no_email.json()["scimType"] == "invalidValue"
    assert client.patch("/scim/v2/Users/nobody", json={"Operations": []}).status_code == 404
