"""SCIM deprovisioning: the filter and PatchOp parsing, the bearer gate, and
the deactivate / reactivate state machine with its retries."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import grant_claims, login, oauth, revocation, scim, scim_routes, service_accounts
from server.auth.scim_routes import (
    ScimError,
    parse_active,
    parse_filter,
    patched_active,
    patched_names,
    user_name_and_email,
)
from server.core import ch
from server.core.errors import GrantBusyError
from server.identity.app import me_service_accounts
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
        patched_names(body, _WORK_ROW)  # an operation that is not an object is refused here
    assert e.value.status == 400


@pytest.mark.parametrize(
    ("body", "email"),
    [
        ({"userName": "Lee@Example.com"}, "lee@example.com"),
        (
            {
                "userName": "lee.upn@corp.example",
                "emails": [
                    {"value": "lee@home.example"},
                    {"value": "Lee@Example.com", "primary": True},
                ],
            },
            "lee@example.com",
        ),
        ({"userName": "lee", "emails": [{"value": "lee@example.com"}]}, "lee@example.com"),
    ],
)
def test_the_email_is_the_primary_one_else_the_username(body: dict[str, Any], email: str) -> None:
    assert user_name_and_email(body)[1] == email


@pytest.mark.parametrize("body", [{}, {"userName": "lee"}, {"userName": " "}])
def test_a_user_with_no_email_is_refused(body: dict[str, Any]) -> None:
    with pytest.raises(ScimError):
        user_name_and_email(body)


def test_active_takes_booleans_and_their_strings() -> None:
    assert parse_active(True) is True and parse_active("true") is True
    assert parse_active(False) is False and parse_active(" FALSE ") is False
    with pytest.raises(ScimError):
        parse_active(0)


# ── the state machine, over an in-memory table ────────────────────────────


class _Table:
    """loom.scim_users, loom.namespaces as `_subjects` reads it, and the
    narrowed revocations as `_lifted_before` does."""

    def __init__(
        self,
        accounts: dict[str, list[str]],
        elsewhere: dict[str, list[str]],
        narrowed: list[tuple[str, datetime]],
        lifted: dict[str, str],
    ) -> None:
        self.rows: list[dict[str, Any]] = []
        self.accounts = accounts
        self.elsewhere = elsewhere
        self.narrowed = narrowed
        self.lifted = lifted
        self.fail_insert = False
        self.tokens: list[dict[str, Any]] = []  # loom.service_accounts
        self.fail_token_insert: str | None = None  # an insert with this token name fails
        self.token_inserts = 0
        self.claimed: set[tuple[str, str]] = set()  # token names a claim holds
        self.shuffle: random.Random | None = None  # the store's rows come in no set order

    def token(self, org: str, name: str) -> dict[str, Any] | None:
        rows = [r for r in self.tokens if (r["org"], r["name"]) == (org, name)]
        return max(rows, key=lambda r: r["version"]) if rows else None

    def made_by(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """The tokens `subs` (any, when absent) made, revoked or not;
        unexpired, when `sql` says so."""
        subs = params.get("subs")
        unexpired = "expires_at > now()" in sql
        return [
            r
            for r in self.heads()
            if (subs is None or r["created_by"] in subs)
            and not (unexpired and r["expires_at"] and r["expires_at"] <= datetime.now())
        ]

    def heads(self) -> list[dict[str, Any]]:
        newest: dict[tuple[str, str], dict[str, Any]] = {}
        for r in self.tokens:
            key = (r["org"], r["name"])
            if key not in newest or r["version"] >= newest[key]["version"]:
                newest[key] = r
        return [newest[key] for key in sorted(newest)]

    def live_tokens(self) -> list[dict[str, Any]]:
        return [r for r in self.heads() if r["revoked_at"] is None]

    def newest(self, uid: str) -> dict[str, Any] | None:
        rows = [r for r in self.rows if r["id"] == uid]
        return dict(max(rows, key=lambda r: r["version"])) if rows else None

    def live(self) -> list[dict[str, Any]]:
        heads = [self.newest(uid) for uid in {r["id"] for r in self.rows}]
        return sorted((r for r in heads if r and not r["deleted"]), key=lambda r: r["user_name"])

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        if "loom.service_accounts" in sql:
            p = params or {}
            live = [r for r in self.made_by(sql, p) if r["revoked_at"] is None]
            return next((r for r in live if (r["org"], r["name"]) == (p["org"], p["name"])), None)
        if "loom.revoked_tokens" in sql:
            cutoffs = [c for s, c in self.narrowed if s in self._lifted_with(params)]
            return {"cutoff": max(cutoffs, default=None)}
        if "count()" in sql:
            return {"n": len(self.live())}
        if "user_name =" in sql:
            return next((r for r in self.live() if r["user_name"] == params["n"]), None)
        if "max(version)" in sql:
            newest = self.newest(params["k"])
            return {"v": max(time.time_ns(), newest["version"] + 1 if newest else 0)}
        if "has(subjects" in sql:
            heads = [self.newest(uid) for uid in {r["id"] for r in self.rows} - {params["id"]}]
            held = [r for r in heads if r and not r["active"] and params["s"] in r["subjects"]]
            return {"x": 1} if held else None
        row = self.newest(params["id"]) if params else None
        if "active = 0" in sql:
            return {"x": 1} if row and not row["active"] else None
        return row

    def _lifted_with(self, params: dict[str, Any]) -> list[str]:
        return [s for s in params["s"] if self.lifted.get(s) == params["r"]]

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "loom.service_accounts" in sql:
            rows = [{**r, "live": r["revoked_at"] is None} for r in self.made_by(sql, params)]
            if self.shuffle is not None:
                self.shuffle.shuffle(rows)
            return rows
        if "loom.revoked_tokens" in sql:
            return [{"jti": s} for s in self._lifted_with(params)]
        if "loom.namespace_grants" in sql:
            return [{"org": o} for o in self.elsewhere.get(params["s"], [])]
        if "loom.namespaces" in sql:
            subjects = self.accounts.get(params["email"], [])
            return [{"owner_email": s, "org": "acme"} for s in subjects]
        return self.live()[params["skip"] : params["skip"] + params["count"]]

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if self.fail_insert:
            raise RuntimeError("ClickHouse is away")
        if table == "loom.service_accounts":
            if any(r["name"] == self.fail_token_insert for r in rows):
                raise RuntimeError("ClickHouse is away")
            assert all((r["org"], r["name"]) in self.claimed for r in rows)
            self.token_inserts += 1
            self.tokens.extend(dict(r) for r in rows)
            return
        assert table == scim.TABLE
        self.rows.extend(dict(r) for r in rows)


class _World:
    """The tables, and what deactivation does to revocations and the audit log."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.accounts: dict[str, list[str]] = {}  # email -> subjects it reaches
        self.elsewhere: dict[str, list[str]] = {}  # subject -> other orgs it is in
        self.outright: dict[str, str] = {}  # subject -> reason
        self.narrowed: list[tuple[str, datetime]] = []
        self.lifted: dict[str, str] = {}  # subject -> reason its narrowing was written with
        self.table = _Table(self.accounts, self.elsewhere, self.narrowed, self.lifted)
        self.audits: list[tuple[str, dict[str, Any]]] = []
        self.fail_revoke = False
        self.claim_limit: int | None = None  # names held at once before a claim runs out
        self.claims: dict[tuple[str, str], asyncio.Lock] = {}  # one holder a name, as a lease
        self.leases: list[str] = []
        self.holding = 0  # SCIM leases held now
        self.waits: list[float] = []  # what the real `wait_out` sleeps

        async def has_outright_row(subject: str) -> bool:
            return subject in self.outright

        async def stale_snapshot(subject: str) -> bool:
            raise AssertionError("SCIM reads the store, not this process's snapshot")

        async def revoke(
            subject: str, *, reason: str, revoked_by: str, reread: bool = True
        ) -> dict[str, Any]:
            if self.fail_revoke:
                raise RuntimeError("ClickHouse is away")
            self.outright[subject] = reason
            self.lifted.pop(subject, None)  # replaced under FINAL
            return {}

        async def narrow(
            key: str, *, reason: str, issued_before: datetime, revoked_by: str, reread: bool = True
        ) -> bool:
            if self.outright.get(key) != reason:
                return False
            del self.outright[key]
            self.narrowed.append((key, issued_before))
            self.lifted[key] = revocation.lifted_reason(reason)
            return True

        async def cutoffs(subjects: list[str]) -> dict[str, int | None]:
            found: dict[str, int | None] = {}
            for s in subjects:
                cutoff = next((c for k, c in reversed(self.narrowed) if k == s), None)
                if s in self.outright:
                    found[s] = None
                elif s in self.lifted and cutoff is not None:
                    found[s] = int(cutoff.replace(tzinfo=cutoff.tzinfo or UTC).timestamp())
            return found

        async def emit(event: str, **kw: Any) -> str:
            self.audits.append((event, kw))
            return ""

        @asynccontextmanager
        async def held(name: str) -> AsyncIterator[None]:
            self.leases.append(name)
            self.holding += 1
            try:
                yield
            finally:
                self.holding -= 1

        @asynccontextmanager
        async def name_claim(org: str, name: str) -> AsyncIterator[None]:
            assert self.holding == 0, "token names are claimed past the SCIM leases"
            if self.claim_limit is not None and len(self.table.claimed) >= self.claim_limit:
                raise RuntimeError("a token name's claim ran out of time")
            lock = self.claims.setdefault((org, name), asyncio.Lock())
            try:  # a name another holds is waited on, then busy, as the real claim
                async with asyncio.timeout(1.0):
                    await lock.acquire()
            except TimeoutError:
                raise GrantBusyError(f"another change to the token name {name!r}") from None
            self.table.claimed.add((org, name))
            try:
                yield
            finally:
                self.table.claimed.discard((org, name))
                lock.release()

        async def no_wait(s: float) -> None:
            self.waits.append(s)

        async def no_refresh() -> None:
            return None

        monkeypatch.setattr(settings, "scim_token", _TOKEN)
        monkeypatch.setattr(scim, "get_client", lambda: self.table)
        monkeypatch.setattr(ch, "get_client", lambda: self.table)
        monkeypatch.setattr(scim, "_held", held)
        monkeypatch.setattr(grant_claims, "service_account_change", name_claim)
        monkeypatch.setattr(revocation, "asyncio", SimpleNamespace(sleep=no_wait))
        monkeypatch.setattr(revocation, "has_outright_row", has_outright_row)
        monkeypatch.setattr(revocation, "revoked_outright", stale_snapshot)
        monkeypatch.setattr(revocation, "revoke", revoke)
        monkeypatch.setattr(revocation, "narrow", narrow)
        monkeypatch.setattr(revocation, "cutoffs", cutoffs)
        monkeypatch.setattr(revocation, "refresh", no_refresh)
        monkeypatch.setattr(audit, "emit", emit)

    def person(self, email: str, *subjects: str) -> None:
        self.accounts[email] = list(subjects)

    @property
    def events(self) -> list[str]:
        return [e for e, _ in self.audits]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _World(monkeypatch)


async def _create(email: str, *, active: bool = True) -> str:
    return str((await scim.create(email, email, active=active))["id"])


async def test_deactivating_revokes_the_person_once_however_often_it_is_sent(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("Lee@Example.com")
    assert uid == scim.user_id("lee@example.com") and world.outright == {}

    row = await scim.set_active(uid, False)
    assert row is not None and not row["active"] and row["subjects"] == ["lee"]
    assert world.outright == {"lee": scim.DEACTIVATED}
    assert world.events == ["auth.scim_deactivated"]
    written = len(world.table.rows)

    await scim.set_active(uid, False)  # the provider's retry
    assert world.events == ["auth.scim_deactivated"] and len(world.table.rows) == written
    assert await _DEACTIVATED("lee@example.com")


async def test_an_email_reaching_several_accounts_revokes_them_all(world) -> None:
    # Two orgs, or a linked password account beside an SSO one: sign-in
    # refuses to pick, and deactivation must not either.
    world.person("lee@example.com", "lee", "lee-other-org")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    assert world.outright == {"lee": scim.DEACTIVATED, "lee-other-org": scim.DEACTIVATED}
    await scim.set_active(uid, True)
    assert sorted(s for s, _ in world.narrowed) == ["lee", "lee-other-org"]
    assert world.outright == {}


async def test_a_subject_someone_else_has_elsewhere_is_revoked_and_flagged(world) -> None:
    # JIT and self-signup name subjects, so another org may have an `alice` too.
    world.person("alice@example.com", "alice")
    world.elsewhere["alice"] = ["other-org"]
    await scim.set_active(await _create("alice@example.com"), False)
    assert world.outright == {"alice": scim.DEACTIVATED}  # fail closed
    [collateral] = [kw for e, kw in world.audits if e == "auth.scim_collateral"]
    assert collateral["subject"]["orgs"] == ["other-org"]
    assert "alice" not in str(collateral["subject"]["subject"])


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


def _narrowed_by_an_operator(world: _World, subject: str) -> datetime:
    """`loom revoke-token --subject <it> --issued-before <now>` on a collateral subject."""
    cutoff = _now()
    del world.outright[subject]
    world.narrowed.append((subject, cutoff))
    world.lifted[subject] = "operator"
    return cutoff


def _made_since(world: _World, name: str, created_by: str, created_at: datetime) -> str:
    token = {**_saved_token(name, created_by), "created_at": created_at}
    world.table.tokens.append(token)
    return str(token["sa_subject"])


async def _deactivated_then_narrowed(world: _World) -> str:
    """Lee deactivated, then narrowed by an operator, with tokens made on each
    side of the cutoff and a token each of those made."""
    world.person("lee@example.com", "lee")
    world.table.tokens.append(_saved_token("laptop", "lee"))
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    cutoff = _narrowed_by_an_operator(world, "lee")
    older = _made_since(world, "older", "lee", cutoff - timedelta(seconds=1))  # dead
    after = _made_since(world, "after", "lee", cutoff)  # issued at the cutoff: live
    _made_since(world, "older-child", older, cutoff)  # its maker is dead
    _made_since(world, "after-child", after, cutoff)
    return uid


async def test_a_repeated_deactivation_leaves_an_operators_narrowing_alone(world) -> None:
    uid = await _deactivated_then_narrowed(world)
    world.person("lee@example.com", "lee", "lee-new")
    await scim.set_active(uid, False)  # the provider sends it again
    assert world.outright == {"lee-new": scim.DEACTIVATED}  # only the account found since
    # The token list says what the token check does: the tokens made since live.
    assert [r["name"] for r in world.table.live_tokens()] == ["after", "after-child"]


async def test_a_token_listed_revoked_before_an_operator_lets_it_live_kills_no_child(
    world,
) -> None:
    world.person("lee@example.com", "lee")
    made = _now()
    parent = _made_since(world, "parent-bot", "lee", made)
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)  # parent-bot is listed revoked
    # The operator restores what lee made since a cutoff before parent-bot.
    del world.outright["lee"]
    world.narrowed.append(("lee", made - timedelta(seconds=10)))
    world.lifted["lee"] = "operator"
    _made_since(world, "child-bot", parent, made + timedelta(seconds=3))
    await scim.set_active(uid, False)  # the provider sends it again
    assert [r["name"] for r in world.table.live_tokens()] == ["child-bot"]


async def test_a_delete_after_an_operators_narrowing_leaves_the_tokens_made_since(world) -> None:
    uid = await _deactivated_then_narrowed(world)
    await scim.delete(uid)  # a provider deleting the user a while after disabling it
    assert world.outright == {}
    assert [r["name"] for r in world.table.live_tokens()] == ["after", "after-child"]


async def test_the_audit_row_names_no_subject_in_clear(world) -> None:
    world.person("lee@example.com", "lee")
    await scim.set_active(await _create("lee@example.com"), False)
    [(_, row)] = world.audits
    assert "lee" not in str(row) and len(row["subject"]["subjects"]) == 1


async def test_every_row_written_carries_every_column(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    await scim.delete(uid)
    assert [sorted(r) for r in world.table.rows] == [sorted(column_names("scim_users"))] * 3


async def test_a_retry_finishes_a_deactivation_whose_revoke_failed(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    world.fail_revoke = True
    with pytest.raises(RuntimeError):
        await scim.set_active(uid, False)
    assert not await _DEACTIVATED("lee@example.com")  # nothing recorded short of the revoke
    world.fail_revoke = False
    await scim.set_active(uid, False)
    assert world.outright == {"lee": scim.DEACTIVATED} and await _DEACTIVATED("lee@example.com")


async def test_reactivating_lifts_the_refusal_and_keeps_old_tokens_revoked(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    before = time.time()
    row = await scim.set_active(uid, True)
    after = time.time()
    assert row is not None and row["active"]
    assert not await _DEACTIVATED("lee@example.com")
    [(subject, cutoff)] = world.narrowed
    assert subject == "lee" and cutoff.timestamp() > before  # tokens issued so far stay dead
    assert world.events == ["auth.scim_deactivated", "auth.scim_reactivated"]
    # Answered once a replica's clock behind this one's is past the cutoff too.
    [wait] = world.waits
    assert before <= cutoff.timestamp() + revocation.CLOCK_SKEW_S - wait <= after

    await scim.set_active(uid, True)
    assert len(world.narrowed) == 1 and len(world.events) == 2


def _saved_token(
    name: str, created_by: str, *, expires_at: datetime | None = None
) -> dict[str, Any]:
    return {
        "org": "acme",
        "name": name,
        "sa_subject": f"sa:{name}-abc123",
        "namespaces": ["ns"],
        "roles": ["writer"],
        "ttl_days": 0,
        "created_by": created_by,
        "created_at": datetime(2026, 1, 1),
        "expires_at": expires_at,
        "revoked_at": None,
        "revoked_by": "",
        "version": 1,
        "kind": "mcp",
        "client": "claude-code",
        "inherit_creator_grants": True,
        "ns_roles": '{"ns": "writer"}',
    }


async def test_a_deactivated_persons_tokens_read_revoked_and_free_their_names(world) -> None:
    world.person("lee@example.com", "lee")
    world.table.tokens += [_saved_token("laptop", "lee"), _saved_token("ci", "admin")]
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    laptop = world.table.token("acme", "laptop")
    assert laptop is not None and sorted(laptop) == sorted(column_names("service_accounts"))
    assert laptop["revoked_at"] is not None and laptop["revoked_by"] == "scim"
    carried = {k: laptop[k] for k in laptop if k not in ("revoked_at", "revoked_by", "version")}
    assert carried.items() <= _saved_token("laptop", "lee").items()
    assert world.table.live_tokens() == [_saved_token("ci", "admin")]  # the admin's own

    written = len(world.table.tokens)
    await scim.set_active(uid, False)  # the provider's retry
    await scim.set_active(uid, True)
    assert len(world.table.tokens) == written  # reactivating revives no token
    assert world.table.live_tokens() == [_saved_token("ci", "admin")]
    await me_service_accounts.refuse_taken_name("acme", "laptop", "lee")  # the name is free


async def test_a_failed_token_revocation_fails_the_deactivation(world) -> None:
    world.person("lee@example.com", "lee")
    world.table.tokens.append(_saved_token("laptop", "lee"))
    uid = await _create("lee@example.com")
    world.table.fail_insert = True
    with pytest.raises(RuntimeError):
        await scim.set_active(uid, False)
    world.table.fail_insert = False
    await scim.set_active(uid, False)  # the provider retries
    assert world.table.live_tokens() == [] and await _DEACTIVATED("lee@example.com")


async def test_tokens_made_by_the_persons_tokens_read_revoked_too(world) -> None:
    world.person("lee@example.com", "lee")
    parent = _saved_token("parent-bot", "lee")
    child = _saved_token("child-bot", parent["sa_subject"])
    grandchild = _saved_token("grandchild-bot", child["sa_subject"])
    world.table.tokens += [parent, child, grandchild, _saved_token("ci", "admin")]
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    for name in ("parent-bot", "child-bot", "grandchild-bot"):
        assert (world.table.token("acme", name) or {})["revoked_by"] == "scim"
    assert world.table.live_tokens() == [_saved_token("ci", "admin")]


async def test_a_token_the_person_revoked_still_takes_its_children(world) -> None:
    # Dead by the store whoever listed it revoked, so what it made is listed revoked too.
    world.person("lee@example.com", "lee")
    parent = {
        **_saved_token("parent-bot", "lee"),
        "revoked_at": datetime(2026, 2, 1),
        "revoked_by": "lee",
    }
    world.table.tokens += [parent, _saved_token("child-bot", parent["sa_subject"])]
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    assert world.table.live_tokens() == []


async def test_many_tokens_are_revoked_in_one_request_a_few_names_held_at_once(world) -> None:
    world.person("lee@example.com", "lee")
    world.table.tokens += [_saved_token(f"bot-{i:04}", "lee") for i in range(2000)]
    world.claim_limit = service_accounts._CLAIMS_AT_ONCE  # each task holds one name
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    assert world.table.live_tokens() == [] and world.table.token_inserts == 2000


async def test_two_passes_over_the_same_tokens_both_finish(world) -> None:
    # Two deactivations reaching the same subject: each pass claims the names
    # in its own order, as the store lists them in none.
    world.person("lee@example.com", "lee")
    world.person("lee.work@example.com", "lee")
    world.table.tokens += [_saved_token(f"bot-{i:04}", "lee") for i in range(300)]
    world.table.shuffle = random.Random(7)
    one, two = await _create("lee@example.com"), await _create("lee.work@example.com")
    await asyncio.gather(scim.set_active(one, False), scim.set_active(two, False))
    assert world.table.live_tokens() == []
    assert world.table.token_inserts == 300  # each name revoked once, the other pass re-read it


async def test_a_failed_insert_keeps_the_ones_before_it_and_the_retry_does_the_rest(
    world,
) -> None:
    world.person("lee@example.com", "lee")
    world.table.tokens += [_saved_token(f"bot-{i:04}", "lee") for i in range(300)]
    uid = await _create("lee@example.com")
    world.table.fail_token_insert = "bot-0150"
    with pytest.raises(RuntimeError):
        await scim.set_active(uid, False)
    landed = 300 - len(world.table.live_tokens())
    assert landed >= 100 and world.table.token_inserts == landed  # siblings were cancelled
    assert world.table.claimed == set()
    world.table.fail_token_insert = None
    written = len(world.table.tokens)
    await scim.set_active(uid, False)  # the provider retries
    assert world.table.live_tokens() == []
    assert len(world.table.tokens) - written == 300 - landed  # only what the first try left


async def test_a_late_deactivation_pass_leaves_a_token_made_after_reactivating(
    world, monkeypatch
) -> None:
    world.person("lee@example.com", "lee")
    world.table.tokens.append(_saved_token("laptop", "lee"))
    uid = await _create("lee@example.com")
    claim = grant_claims.service_account_change
    reached, resume = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def paused(org: str, name: str) -> AsyncIterator[None]:
        reached.set()
        await resume.wait()
        async with claim(org, name):
            yield

    monkeypatch.setattr(grant_claims, "service_account_change", paused)
    deactivating = asyncio.create_task(scim.set_active(uid, False))
    await reached.wait()  # the pass has listed `laptop` and waits for its name
    await scim.set_active(uid, True)
    # The old `laptop` reads dead, so its name is free: the person makes a new
    # one, issued at the cutoff, so live, however the clocks stand.
    [(_, cutoff)] = world.narrowed
    made = cutoff.astimezone(UTC).replace(tzinfo=None)
    new = {**_saved_token("laptop", "lee"), "sa_subject": "sa:laptop-def456"}
    world.table.tokens.append({**new, "created_at": made, "version": 2})
    resume.set()
    await deactivating
    assert [r["sa_subject"] for r in world.table.live_tokens()] == ["sa:laptop-def456"]


async def test_an_expired_token_is_left_as_it_is(world) -> None:
    world.person("lee@example.com", "lee")
    expired = _saved_token("old", "lee", expires_at=datetime(2026, 1, 2))
    world.table.tokens.append(expired)
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    assert world.table.tokens == [expired]


async def test_a_retry_of_an_inactive_user_revokes_what_a_failed_try_left(world) -> None:
    world.person("lee@example.com", "lee")
    parent = _saved_token("parent-bot", "lee")
    world.table.tokens += [parent, _saved_token("child-bot", parent["sa_subject"])]
    uid = await _create("lee@example.com")
    world.table.fail_token_insert = "child-bot"
    with pytest.raises(RuntimeError):
        await scim.set_active(uid, False)
    assert await _DEACTIVATED("lee@example.com")  # the row landed; the child did not
    assert [r["name"] for r in world.table.live_tokens()] == ["child-bot"]
    world.table.fail_token_insert = None
    await scim.set_active(uid, False)  # the provider retries
    assert world.table.live_tokens() == []


async def test_a_retried_reactivation_still_waits_out_the_first_ones_cutoff(
    world, monkeypatch
) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    world.table.fail_insert = True
    with pytest.raises(RuntimeError):  # narrowed, then the row's write failed
        await scim.set_active(uid, True)
    world.table.fail_insert = False
    waited: list[datetime] = []

    async def wait(cutoff: datetime) -> None:
        waited.append(cutoff)

    monkeypatch.setattr(revocation, "wait_out", wait)
    row = await scim.set_active(uid, True)  # the provider's retry narrows nothing
    assert row is not None and row["active"]
    [(_, cutoff)] = world.narrowed
    assert waited == [cutoff]


async def test_a_reactivation_retried_after_a_timeout_waits_out_the_rest(
    world, monkeypatch
) -> None:
    # The provider gave up during the first one's wait; the row is already active.
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    await scim.set_active(uid, True)
    await scim.set_active(uid, True)
    first, retry = world.waits
    assert 0 < retry <= first

    await scim.update(uid, user_name="lee@example.com")  # no `active`: nothing read or waited
    assert len(world.waits) == 2

    later = time.time() + 60
    monkeypatch.setattr(revocation, "time", SimpleNamespace(time=lambda: later))
    await scim.set_active(uid, True)  # a late retry answers at once
    assert world.waits[-1] == 0


async def test_a_retried_re_create_waits_out_the_cutoff_then_answers_409(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    with pytest.raises(scim.Exists):  # live, nothing lifted: refused at once
        await _create("lee@example.com")
    assert world.waits == []

    await scim.set_active(uid, False)
    await scim.delete(uid)
    await _create("lee@example.com")  # created again: reactivated, and waits
    with pytest.raises(scim.Exists):  # the provider timed out and sends it again
        await _create("lee@example.com")
    first, retry = world.waits
    assert 0 < retry <= first


async def test_a_reactivation_waits_once_its_leases_are_let_go(world, monkeypatch) -> None:
    # Held through the wait, a deactivation sent meanwhile would wait on the
    # lease past `_LEASE_WAIT_S` and answer busy.
    events: list[str] = []

    @asynccontextmanager
    async def held(name: str) -> AsyncIterator[None]:
        events.append(f"held {name.split(':')[0]}")
        yield
        events.append(f"let go {name.split(':')[0]}")

    async def wait(cutoff: datetime) -> None:
        events.append("waited")

    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    monkeypatch.setattr(scim, "_held", held)
    monkeypatch.setattr(revocation, "wait_out", wait)
    await scim.set_active(uid, True)
    assert events[-2:] == ["let go scim-user", "waited"] and events.count("waited") == 1

    await scim.delete(uid)
    events.clear()
    await scim.create("lee@example.com", "lee@example.com", active=True)  # created again
    assert events[-3:] == ["let go scim-username", "let go scim-user", "waited"]


async def test_an_operator_revocation_survives_deactivate_and_reactivate(world) -> None:
    world.person("lee@example.com", "lee")
    world.outright["lee"] = "offboarded"
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    await scim.set_active(uid, True)
    assert world.outright == {"lee": "offboarded"} and world.narrowed == []


async def test_a_deactivation_after_a_failed_reactivation_revokes_outright_again(world) -> None:
    world.person("lee@example.com", "lee", "lee-ops")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    world.table.fail_insert = True
    with pytest.raises(RuntimeError):  # narrowed, then the row's write failed
        await scim.set_active(uid, True)
    world.table.fail_insert = False
    world.lifted["lee-ops"] = "offboarded"  # an operator narrowed this one their own way
    await scim.set_active(uid, False)  # the row still says inactive
    assert world.outright == {"lee": scim.DEACTIVATED}
    assert world.lifted == {"lee-ops": "offboarded"}


async def test_an_email_with_no_account_is_refused_by_email(world) -> None:
    uid = await _create("new@example.com", active=False)
    assert await _DEACTIVATED("new@example.com") and world.outright == {}
    assert (await scim.get(uid) or {})["subjects"] == []
    # An account made since (by an operator) is revoked when the retry finds it.
    world.person("new@example.com", "u-new")
    await scim.set_active(uid, False)
    assert world.outright == {"u-new": scim.DEACTIVATED}


async def test_nothing_is_refused_once_scim_is_off(world, monkeypatch) -> None:
    await _create("lee@example.com", active=False)
    monkeypatch.setattr(settings, "scim_token", "")
    assert not await _DEACTIVATED("lee@example.com")


async def test_a_deleted_user_is_gone_but_stays_deactivated(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    with pytest.raises(scim.Exists):
        await _create("LEE@example.com")
    assert await scim.delete(uid)
    assert world.events == ["auth.scim_deactivated", "auth.scim_deleted"]
    assert await scim.get(uid) is None and not await scim.delete(uid)
    assert await scim.set_active(uid, True) is None
    assert await _DEACTIVATED("lee@example.com")
    # Created again by the provider: assigned again, so active again.
    again = await scim.create("lee@example.com", "lee@example.com", active=True)
    assert again["id"] == uid and again["active"] and world.narrowed


async def test_a_retried_delete_finishes_the_token_list_and_still_answers_404(world) -> None:
    world.person("lee@example.com", "lee")
    world.table.tokens.append(_saved_token("laptop", "lee"))
    uid = await _create("lee@example.com")
    world.table.fail_token_insert = "laptop"
    with pytest.raises(RuntimeError):
        await scim.delete(uid)
    assert await scim.get(uid) is None and len(world.table.live_tokens()) == 1
    world.table.fail_token_insert = None
    assert not await scim.delete(uid)  # the provider retries: 404, and the list is done
    assert world.table.live_tokens() == []
    assert world.events == ["auth.scim_deactivated", "auth.scim_deleted"]


async def test_deleting_an_inactive_user_is_audited_too(world) -> None:
    uid = await _create("lee@example.com", active=False)
    await scim.delete(uid)
    assert world.events == ["auth.scim_deactivated", "auth.scim_deleted"]


async def test_a_deactivated_email_is_refused_where_every_sso_sign_in_ends(monkeypatch) -> None:
    async def deactivated(email: str) -> bool:
        return email == "lee@example.com"

    async def emit(event: str, **kw: Any) -> str:
        return ""

    monkeypatch.setattr(scim, "deactivated", deactivated)
    monkeypatch.setattr(audit, "emit", emit)
    # Before JIT could make an account, and again at the end of a link.
    got = await oauth._account_for("lee@example.com", {}, "")
    assert got == oauth._Denied("this account is deactivated")
    identity = ("lee", "acme", ["lee"], ["writer"])
    page = await oauth._signed_in_as({"kind": "dashboard"}, identity, "", "lee@example.com")
    assert page.status_code == 403 and b"deactivated" in page.body


async def test_an_unreadable_deactivation_fails_the_sign_in_retryably(world, monkeypatch) -> None:
    class Away:
        async def query_one(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("ClickHouse is away")

    monkeypatch.setattr(scim, "get_client", lambda: Away())
    with pytest.raises(login.SignInUnavailable):
        await _DEACTIVATED("lee@example.com")


# ── revocation.narrow ─────────────────────────────────────────────────────

_OURS = {"reason": scim.DEACTIVATED, "issued_before": None, "version": 7}


@pytest.mark.parametrize(
    ("stored", "narrows"),
    [
        (_OURS, True),
        ({**_OURS, "reason": "offboarded"}, False),
        ({**_OURS, "issued_before": datetime(2026, 1, 1)}, False),
        (None, False),
    ],
)
async def test_narrow_touches_only_its_own_outright_revocation(
    monkeypatch, stored: dict[str, Any] | None, narrows: bool
) -> None:
    written = _narrowing(monkeypatch, stored, None)
    cutoff = datetime.now(UTC)
    got = await revocation.narrow(
        "lee", reason=scim.DEACTIVATED, issued_before=cutoff, revoked_by="scim"
    )
    assert got is narrows
    if narrows:
        [w] = written
        assert (w["key"], w["issued_before"]) == ("lee", cutoff)
    else:
        assert written == []


async def test_narrow_puts_back_a_revocation_written_while_it_ran(monkeypatch) -> None:
    raced = {
        "reason": "offboarded",
        "revoked_by": "operator",
        "issued_before": None,
        "expires_at": datetime(2105, 12, 31),
    }
    written = _narrowing(monkeypatch, _OURS, raced)
    await revocation.narrow(
        "lee", reason=scim.DEACTIVATED, issued_before=datetime.now(UTC), revoked_by="scim"
    )
    assert [w["reason"] for w in written] == [f"{scim.DEACTIVATED}, lifted", "offboarded"]
    assert written[-1]["issued_before"] is None  # outright again, on top


def _narrowing(
    monkeypatch: pytest.MonkeyPatch, stored: dict[str, Any] | None, raced: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Fake the reads `narrow` makes: the row it narrows, then any written since."""
    from server.core import ch

    written: list[dict[str, Any]] = []
    answers = [stored, raced]

    class Read:
        async def query_one(self, *_a: Any, **_k: Any) -> dict[str, Any] | None:
            return answers.pop(0)

    async def revoke(key: str, **kw: Any) -> dict[str, Any]:
        written.append({"key": key, **kw})
        return {}

    monkeypatch.setattr(ch, "get_client", lambda: Read())
    monkeypatch.setattr(revocation, "revoke", revoke)
    return written


# ── the routes ────────────────────────────────────────────────────────────


@pytest.fixture
def client(world: _World) -> TestClient:
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


def test_a_short_token_refuses_to_start_and_doctor_says_so(monkeypatch) -> None:
    from server.cli import doctor

    scim_routes.require_scim_token_length("")  # off is fine
    scim_routes.require_scim_token_length("x" * 32)
    with pytest.raises(RuntimeError, match="at least 32"):
        scim_routes.require_scim_token_length("x" * 31)
    monkeypatch.setattr(settings, "scim_token", "x" * 31)
    assert doctor._doctor_scim_token_check(settings)[1] == doctor.FAIL
    monkeypatch.setattr(settings, "scim_token", "")
    assert doctor._doctor_scim_token_check(settings)[1] == doctor.PASS


def test_a_provider_creates_finds_and_deactivates_a_person(client: TestClient, world) -> None:
    world.person("lee@example.com", "lee")
    assert client.get("/scim/v2/ServiceProviderConfig").json()["patch"] == {"supported": True}
    found = client.get("/scim/v2/Users", params={"filter": 'userName eq "lee.upn@corp.example"'})
    assert found.json()["totalResults"] == 0

    created = client.post(
        "/scim/v2/Users",
        content=b'{"schemas": [], "userName": "Lee.UPN@corp.example", "name": {"givenName": "Lee"},'
        b' "emails": [{"value": "lee@example.com", "type": "work", "primary": true}]}',
        headers={"Content-Type": "application/scim+json"},
    )
    assert created.status_code == 201, created.text
    user = created.json()
    uid = user["id"]
    assert user["userName"] == "lee.upn@corp.example" and user["active"] is True
    assert user["emails"] == [{"value": "lee@example.com", "primary": True}]
    assert uid == scim.user_id("lee@example.com")  # keyed on the address sign-in carries
    assert created.headers["location"].endswith(f"/scim/v2/Users/{uid}")
    again = client.post("/scim/v2/Users", json={"userName": "lee@example.com"})
    assert again.status_code == 409

    found = client.get("/scim/v2/Users", params={"filter": 'userName eq "lee.upn@corp.example"'})
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


def test_a_patch_cannot_change_the_email(client: TestClient, world) -> None:
    created = client.post(
        "/scim/v2/Users",
        json={"userName": "lee.upn@corp.example", "emails": [{"value": "lee@example.com"}]},
    )
    uid = created.json()["id"]
    for op in [
        {"op": "Replace", "path": 'emails[type eq "work"].value', "value": "new@example.com"},
        {"op": "replace", "path": "emails", "value": [{"value": "new@example.com"}]},
        {"op": "replace", "value": {"emails": [{"value": "new@example.com", "primary": True}]}},
        {"op": "remove", "path": "emails"},
    ]:
        r = client.patch(f"/scim/v2/Users/{uid}", json={"Operations": [op]})
        assert r.status_code == 400 and r.json()["scimType"] == "mutability", op
    same = {"op": "replace", "path": 'emails[type eq "work"].value', "value": "Lee@example.com"}
    assert client.patch(f"/scim/v2/Users/{uid}", json={"Operations": [same]}).status_code == 200


def test_a_username_rename_is_kept_and_found(client: TestClient, world) -> None:
    created = client.post(
        "/scim/v2/Users",
        json={"userName": "lee.upn@corp.example", "emails": [{"value": "lee@example.com"}]},
    )
    uid = created.json()["id"]
    rename = {
        "Operations": [{"op": "replace", "path": "userName", "value": "lee.new@corp.example"}]
    }
    assert client.patch(f"/scim/v2/Users/{uid}", json=rename).json()["userName"] == (
        "lee.new@corp.example"
    )
    found = client.get("/scim/v2/Users", params={"filter": 'userName eq "lee.new@corp.example"'})
    assert [u["id"] for u in found.json()["Resources"]] == [uid]
    put = client.put(
        f"/scim/v2/Users/{uid}",
        json={"userName": "lee.put@corp.example", "emails": [{"value": "lee@example.com"}]},
    )
    assert put.json()["userName"] == "lee.put@corp.example"


def test_a_username_that_is_the_email_cannot_change_by_patch(client: TestClient, world) -> None:
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    rename = {"Operations": [{"op": "replace", "value": {"userName": "new@example.com"}}]}
    r = client.patch(f"/scim/v2/Users/{uid}", json=rename)
    assert r.status_code == 400 and r.json()["scimType"] == "mutability"


_URN = "urn:ietf:params:scim:schemas:core:2.0:User:"
# A user keyed on the work entry of their emails.
_WORK_ROW = {"email": "lee@example.com", "email_type": "work", "email_primary": 0}


@pytest.mark.parametrize(
    "op",
    [
        {"op": "replace", "path": f"{_URN}active", "value": False},
        {"op": "replace", "value": {f"{_URN}active": False}},
    ],
)
def test_a_schema_qualified_active_is_active(op: dict[str, Any]) -> None:
    assert patched_active({"Operations": [op]}) is False


@pytest.mark.parametrize(
    "op",
    [
        {"op": "replace", "path": 'emails[type eq "work"]', "value": {"value": "new@example.com"}},
        {
            "op": "replace",
            "path": f'{_URN}emails[type eq "work"].value',
            "value": "new@example.com",
        },
        {"op": "replace", "value": {f"{_URN}emails": [{"value": "new@example.com"}]}},
        {"op": "add", "path": "emails", "value": [{"value": "new@example.com", "primary": True}]},
    ],
)
def test_every_shape_of_an_email_change_is_seen(op: dict[str, Any]) -> None:
    _, entry = patched_names({"Operations": [op]}, _WORK_ROW)
    assert entry is not None and entry["value"] == "new@example.com"


def test_a_sub_attribute_of_an_email_changes_no_address() -> None:
    op = {"op": "replace", "path": 'emails[type eq "work"].primary', "value": True}
    _, entry = patched_names({"Operations": [op]}, _WORK_ROW)
    assert entry is not None and entry["value"] == "lee@example.com"


def test_a_schema_qualified_patch_deactivates_and_refuses_a_new_email(
    client: TestClient, world
) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    entry = {"op": "replace", "path": 'emails[type eq "work"]', "value": {"value": "b@example.com"}}
    r = client.patch(f"/scim/v2/Users/{uid}", json={"Operations": [entry]})
    assert r.status_code == 400 and r.json()["scimType"] == "mutability"
    off = {"op": "replace", "value": {f"{_URN}active": False}}
    assert (
        client.patch(f"/scim/v2/Users/{uid}", json={"Operations": [off]}).json()["active"] is False
    )
    assert world.outright == {"lee": scim.DEACTIVATED}


def test_a_username_another_user_holds_is_refused(client: TestClient, world) -> None:
    def user(name: str, email: str) -> dict[str, Any]:
        return {"userName": name, "emails": [{"value": email}]}

    first = client.post("/scim/v2/Users", json=user("lee.upn@corp.example", "lee@example.com"))
    assert first.status_code == 201
    taken = client.post("/scim/v2/Users", json=user("lee.upn@corp.example", "kim@example.com"))
    assert taken.status_code == 409 and taken.json()["scimType"] == "uniqueness"
    kim = client.post("/scim/v2/Users", json=user("kim.upn@corp.example", "kim@example.com"))
    rename = {
        "Operations": [{"op": "replace", "path": "userName", "value": "LEE.upn@corp.example"}]
    }
    r = client.patch(f"/scim/v2/Users/{kim.json()['id']}", json=rename)
    assert r.status_code == 409 and r.json()["scimType"] == "uniqueness"


async def test_reactivating_one_user_keeps_a_subject_another_inactive_user_reaches(world) -> None:
    world.person("a@example.com", "alice")
    world.person("b@example.com", "alice")  # two addresses, one subject
    a, b = await _create("a@example.com"), await _create("b@example.com")
    await scim.set_active(a, False)
    await scim.set_active(b, False)  # alice is already revoked, and B lists her
    await scim.set_active(a, True)
    assert world.outright == {"alice": scim.DEACTIVATED} and world.narrowed == []
    await scim.set_active(b, True)  # the last one holding her lets her go
    assert world.outright == {} and [s for s, _ in world.narrowed] == ["alice"]


def test_a_patch_refused_for_an_email_still_deactivates(client: TestClient, world) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    ops = [
        {"op": "replace", "path": "active", "value": False},
        {"op": "replace", "path": "userName", "value": "new@example.com"},
    ]
    r = client.patch(f"/scim/v2/Users/{uid}", json={"Operations": ops})
    assert r.status_code == 400 and r.json()["scimType"] == "mutability"
    assert world.outright == {"lee": scim.DEACTIVATED}  # fail closed
    assert client.get(f"/scim/v2/Users/{uid}").json()["active"] is False


def test_a_put_refused_for_an_email_still_deactivates(client: TestClient, world) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    body = {"userName": "lee@example.com", "emails": [{"value": "b@example.com"}], "active": False}
    r = client.put(f"/scim/v2/Users/{uid}", json=body)
    assert r.status_code == 400 and r.json()["scimType"] == "mutability"
    assert world.outright == {"lee": scim.DEACTIVATED}


@pytest.mark.parametrize(
    "other",
    [
        # A leaver's other address changed in the same sync cycle as the disable.
        {"op": "add", "path": 'emails[type eq "other"].value', "value": "lee@home.example"},
        {"op": "remove", "path": 'emails[type eq "other"]'},
        {"op": "add", "path": "emails", "value": [{"value": "x@home.example", "type": "home"}]},
    ],
)
def test_another_address_changing_beside_a_disable_is_fine(
    client: TestClient, world, other: dict[str, Any]
) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    ops = [{"op": "replace", "path": "active", "value": False}, other]
    r = client.patch(f"/scim/v2/Users/{uid}", json={"Operations": ops})
    assert r.status_code == 200 and r.json()["active"] is False
    assert r.json()["emails"] == [{"value": "lee@example.com", "primary": True}]
    assert world.outright == {"lee": scim.DEACTIVATED}


@pytest.mark.parametrize(
    ("emails", "email"),
    [
        (
            [
                {"value": "h@example.com", "type": "home"},
                {"value": "w@example.com", "type": "work"},
            ],
            "w@example.com",
        ),
        (
            [
                {"value": "w@example.com", "type": "work"},
                {"value": "p@example.com", "primary": True},
            ],
            "p@example.com",
        ),
        (
            [{"value": "a@example.com", "type": "other"}, {"value": "b@example.com"}],
            "a@example.com",
        ),
    ],
)
def test_the_email_is_the_primary_else_the_work_else_the_first(
    emails: list[dict[str, Any]], email: str
) -> None:
    assert user_name_and_email({"userName": "x", "emails": emails})[1] == email


def _user(client: TestClient, emails: list[dict[str, Any]]) -> str:
    created = client.post(
        "/scim/v2/Users", json={"userName": "lee.upn@corp.example", "emails": emails}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _patch(client: TestClient, uid: str, *ops: dict[str, Any]) -> Any:
    return client.patch(f"/scim/v2/Users/{uid}", json={"Operations": list(ops)})


def test_an_unchanged_work_address_passes(client: TestClient, world) -> None:
    uid = _user(client, [{"value": "lee@example.com", "type": "work"}])
    same = {"op": "replace", "path": 'emails[type eq "work"].value', "value": "Lee@example.com"}
    assert _patch(client, uid, same).status_code == 200


def test_a_change_through_the_keyed_entrys_own_type_is_refused(client: TestClient, world) -> None:
    uid = _user(client, [{"value": "lee@example.com", "type": "other"}])
    moved = {"op": "replace", "path": 'emails[type eq "other"].value', "value": "new@example.com"}
    r = _patch(client, uid, moved)
    assert r.status_code == 400 and r.json()["scimType"] == "mutability"


def test_a_quoted_primary_filter_selects_the_primary(client: TestClient, world) -> None:
    uid = _user(client, [{"value": "lee@example.com", "primary": True}])
    for op in (
        {"op": "replace", "path": 'emails[primary eq "true"].value', "value": "new@example.com"},
        {"op": "replace", "path": "emails[primary eq true].value", "value": "new@example.com"},
    ):
        r = _patch(client, uid, op)
        assert r.status_code == 400 and r.json()["scimType"] == "mutability", op


def test_a_new_work_entry_would_take_a_plain_entrys_place(client: TestClient, world) -> None:
    uid = _user(client, [{"value": "lee@example.com"}])
    work = {"op": "add", "path": 'emails[type eq "work"].value', "value": "new@example.com"}
    assert _patch(client, uid, work).status_code == 400
    home = {"op": "add", "path": 'emails[type eq "home"].value', "value": "lee@home.example"}
    assert _patch(client, uid, home).status_code == 200


def test_a_put_naming_another_user_is_refused_before_anything(client: TestClient, world) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    body = {"id": "someone-else", "userName": "lee@example.com", "active": False}
    r = client.put(f"/scim/v2/Users/{uid}", json=body)
    assert r.status_code == 400 and r.json()["scimType"] == "invalidValue"
    assert world.outright == {}  # nothing applied: the request is about another user


def _subject_lease(subject: str) -> str:
    return f"scim-subject:{scim._keyed(subject)[:32]}"


async def test_leases_go_user_then_username_then_each_subject_sorted(world) -> None:
    world.person("lee@example.com", "lee", "alice")
    uid = await _create("lee@example.com")
    # An active user staying active touches no subject, so takes no subject lease.
    assert world.leases == [f"scim-user:{uid}", f"scim-username:{scim.user_id('lee@example.com')}"]
    world.leases.clear()
    await scim.set_active(uid, False)
    by_subject = [_subject_lease(s) for s in ("alice", "lee")]  # sorted by subject
    assert world.leases == [f"scim-user:{uid}", *by_subject]
    world.leases.clear()
    await scim.set_active(uid, True)
    assert world.leases == [f"scim-user:{uid}", *by_subject]


async def test_a_rename_of_an_active_user_waits_on_no_subject(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    world.leases.clear()
    await scim.update(uid, user_name="lee.new@corp.example")
    assert world.leases == [
        f"scim-user:{uid}",
        f"scim-username:{scim.user_id('lee.new@corp.example')}",
    ]


async def test_a_lease_the_store_cannot_give_is_busy(monkeypatch) -> None:
    from contextlib import asynccontextmanager as cm

    from server.core import leases

    @cm
    async def away(*_a: Any, **_k: Any) -> AsyncIterator[bool]:
        raise leases.LeaseUnavailable("the lease store did not answer")
        yield True

    monkeypatch.setattr(leases, "holding", away)
    with pytest.raises(scim.Busy):
        async with scim._held("scim-user:x"):
            pass


def test_a_busy_user_answers_a_scim_503(client: TestClient, world, monkeypatch) -> None:
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]

    async def busy(*_a: Any, **_k: Any) -> None:
        raise scim.Busy

    monkeypatch.setattr(scim, "update", busy)
    r = _patch(client, uid, {"op": "replace", "path": "active", "value": False})
    assert r.status_code == 503 and r.headers["retry-after"]
    assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


async def test_a_shared_subject_left_revoked_is_logged_and_nothing_waits(world, caplog) -> None:
    world.person("a@example.com", "alice")
    world.person("b@example.com", "alice")
    a, b = await _create("a@example.com"), await _create("b@example.com")
    await scim.set_active(a, False)
    await scim.set_active(b, False)
    with caplog.at_level("WARNING", logger="server.auth.scim"):
        await scim.set_active(a, True)
    assert "another deactivated user still reaches" in caplog.text
    assert world.waits == []  # nothing narrowed, so no cutoff to wait out


async def test_an_operator_revocation_makes_no_reactivation_wait(world) -> None:
    world.person("lee@example.com", "lee")
    world.outright["lee"] = "offboarded"
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    await scim.set_active(uid, True)
    assert world.waits == []


_PRIMARY_ROW = {"email": "lee@example.com", "email_type": "work", "email_primary": 1}


def test_a_stored_primary_entry_ranks_as_primary() -> None:
    # A secondary address of a user keyed on their primary entry may change.
    other = {"op": "replace", "path": 'emails[type eq "other"].value', "value": "x@example.com"}
    assert patched_names({"Operations": [other]}, _PRIMARY_ROW) == (None, None)
    demoted = {"op": "replace", "path": "emails[primary eq true].primary", "value": False}
    with pytest.raises(ScimError) as e:
        patched_names({"Operations": [demoted]}, _PRIMARY_ROW)
    assert e.value.scim_type == "mutability"


@pytest.mark.parametrize("flag", [True, "True", "true", 1])
def test_every_spelling_of_primary_is_kept_as_primary(world, flag: Any) -> None:
    async def run() -> dict[str, Any]:
        entry = {"value": "lee@example.com", "primary": flag}
        return await scim.create(
            "lee@example.com", "lee@example.com", active=True, email_entry=entry
        )

    import asyncio

    assert asyncio.run(run())["email_primary"] == 1


def test_a_malformed_path_is_refused_and_still_deactivates(client: TestClient, world) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    broken = {"op": "replace", "path": 'emails[type eq "work"', "value": "b@example.com"}
    r = _patch(client, uid, {"op": "replace", "path": "active", "value": False}, broken)
    assert r.status_code == 400 and r.json()["scimType"] == "invalidPath"
    assert world.outright == {"lee": scim.DEACTIVATED}


def test_a_failed_request_still_deactivates(client: TestClient, world, monkeypatch) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(scim_routes, "patched_names", broken)
    r = _patch(client, uid, {"op": "replace", "path": "active", "value": False})
    assert r.status_code == 503 and world.outright == {"lee": scim.DEACTIVATED}


def test_a_whole_entry_write_must_be_an_object(client: TestClient, world) -> None:
    uid = _user(client, [{"value": "lee@example.com", "type": "work"}])
    r = _patch(client, uid, {"op": "replace", "path": 'emails[type eq "work"]', "value": "b@x.com"})
    assert r.status_code == 400 and r.json()["scimType"] == "invalidValue"


async def test_an_account_made_while_deactivating_is_revoked_too(world, monkeypatch) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    write = scim._write

    async def sign_in_meanwhile(row: dict[str, Any]) -> None:
        # A first sign-in's JIT lands after the accounts were read, before the row says inactive.
        if not row["active"] and "u-lee" not in world.accounts["lee@example.com"]:
            world.person("lee@example.com", "lee", "u-lee")
        await write(row)

    monkeypatch.setattr(scim, "_write", sign_in_meanwhile)
    row = await scim.set_active(uid, False)
    assert row is not None and row["subjects"] == ["lee", "u-lee"]
    assert world.outright == {"lee": scim.DEACTIVATED, "u-lee": scim.DEACTIVATED}
    assert world.events == ["auth.scim_deactivated"]  # one transition, audited once


def test_a_busy_deactivation_is_not_tried_twice(client: TestClient, world, monkeypatch) -> None:
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    calls: list[Any] = []

    async def busy(*a: Any, **k: Any) -> None:
        calls.append(k)
        raise scim.Busy

    monkeypatch.setattr(scim, "update", busy)
    r = _patch(client, uid, {"op": "replace", "path": "active", "value": False})
    assert r.status_code == 503 and len(calls) == 1


def test_a_store_outage_answers_a_scim_503(client: TestClient, monkeypatch) -> None:
    from server.core.errors import BackendUnavailableError

    async def away(*_a: Any, **_k: Any) -> None:
        raise BackendUnavailableError("ClickHouse could not be reached")

    monkeypatch.setattr(scim, "get", away)
    r = client.get("/scim/v2/Users/x")
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert r.headers["content-type"].startswith("application/scim+json")
    assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


def test_an_unexpected_error_answers_a_scim_503(client: TestClient, monkeypatch) -> None:
    async def broken(*_a: Any, **_k: Any) -> None:
        raise Exception("Code: 241. Memory limit exceeded")

    monkeypatch.setattr(scim, "get", broken)
    r = client.get("/scim/v2/Users/x")
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
    assert r.headers["content-type"].startswith("application/scim+json")
    assert r.json() == {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
        "status": "503",
        "detail": "the request failed; retry",
    }


async def test_the_transition_is_audited_even_when_the_re_read_fails(world, monkeypatch) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    reads = scim._subjects
    calls = 0

    async def second_read_fails(email: str) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 2:  # the re-read after the row landed
            raise RuntimeError("ClickHouse is away")
        return await reads(email)

    monkeypatch.setattr(scim, "_subjects", second_read_fails)
    with pytest.raises(RuntimeError):
        await scim.set_active(uid, False)
    assert world.events == [
        "auth.scim_deactivated"
    ]  # the retry finds it inactive: no second chance
    await scim.set_active(uid, False)
    assert world.events == ["auth.scim_deactivated"]


def test_unknown_paths_and_methods_answer_scim_errors(client: TestClient) -> None:
    for method, path, status in [
        ("GET", "/Groups", 404),
        ("GET", "/Schemas", 404),
        ("POST", "/Users/x", 405),
        ("DELETE", "/Users", 405),
    ]:
        r = client.request(method, f"/scim/v2{path}")
        assert r.status_code == status, (method, path)
        assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    client.headers.pop("Authorization")
    assert client.get("/scim/v2/Groups").status_code == 401  # probing needs the token too


def test_every_loom_error_answers_in_scim_format(client: TestClient, monkeypatch) -> None:
    from server.core.errors import BackendTimeoutError, OverloadedError, RateLimitedError

    for error, status in [
        (OverloadedError("busy"), 503),
        (BackendTimeoutError("slow"), 503),
        (RateLimitedError("slow down"), 429),
    ]:

        async def fail(*_a: Any, _e: Exception = error, **_k: Any) -> None:
            raise _e

        monkeypatch.setattr(scim, "get", fail)
        r = client.get("/scim/v2/Users/x")
        assert r.status_code == status, error
        assert r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
        assert (r.headers.get("retry-after") == "5") is (status == 503)


def test_a_store_failure_is_not_deactivated_twice(client: TestClient, world, monkeypatch) -> None:
    from server.core.errors import BackendTimeoutError

    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    calls: list[Any] = []

    async def slow(*a: Any, **k: Any) -> None:
        calls.append(k)
        raise BackendTimeoutError("a write ran past its deadline")

    monkeypatch.setattr(scim, "update", slow)
    r = _patch(client, uid, {"op": "replace", "path": "active", "value": False})
    assert r.status_code == 503 and len(calls) == 1


def test_a_driver_error_is_not_deactivated_twice(client: TestClient, world, monkeypatch) -> None:
    from clickhouse_connect.driver.exceptions import DatabaseError

    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    calls: list[Any] = []

    async def failing(*a: Any, **k: Any) -> None:
        calls.append(k)
        raise DatabaseError("Code: 241. Memory limit exceeded")

    monkeypatch.setattr(scim, "update", failing)
    r = _patch(client, uid, {"op": "replace", "path": "active", "value": False})
    assert r.status_code == 503 and len(calls) == 1


def test_a_deeply_nested_body_is_a_400(client: TestClient) -> None:
    r = client.post("/scim/v2/Users", content=b"[" * 100_000 + b"]" * 100_000)
    assert r.status_code == 400 and r.json()["scimType"] == "invalidSyntax"
