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
from tests.unit.revocation_fakes import revocation_read
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
    """loom.scim_users, loom.namespaces as `_subjects` reads it, and
    loom.revoked_tokens as the revocation module reads and writes it."""

    def __init__(self, accounts: dict[str, list[str]], elsewhere: dict[str, list[str]]) -> None:
        self.rows: list[dict[str, Any]] = []
        self.accounts = accounts
        self.elsewhere = elsewhere
        self.fail_insert = False  # any other insert fails
        self.fail_revoke = False  # an insert into loom.revoked_tokens fails
        self.revocations: list[dict[str, Any]] = []  # loom.revoked_tokens, every row written
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

    def revoked(self) -> dict[str, dict[str, Any]]:
        """The newest unexpired row of each key, as under FINAL."""
        newest: dict[str, dict[str, Any]] = {}
        for r in self.revocations:
            if r["jti"] not in newest or r["version"] > newest[r["jti"]]["version"]:
                newest[r["jti"]] = r
        now = datetime.now(UTC).replace(tzinfo=None)
        return {k: r for k, r in newest.items() if r["expires_at"] > now}

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
            return revocation_read(self.revocations, sql, params)
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

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if "loom.service_accounts" in sql:
            rows = [{**r, "live": r["revoked_at"] is None} for r in self.made_by(sql, params)]
            if self.shuffle is not None:
                self.shuffle.shuffle(rows)
            return rows
        if "loom.revoked_tokens" in sql:
            return [r for k, r in self.revoked().items() if k in params["keys"]]
        if "loom.namespace_grants" in sql:
            return [{"org": o} for o in self.elsewhere.get(params["s"], [])]
        if "loom.namespaces" in sql:
            subjects = self.accounts.get(params["email"], [])
            return [{"owner_email": s, "org": "acme"} for s in subjects]
        return self.live()[params["skip"] : params["skip"] + params["count"]]

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        if table == "loom.revoked_tokens":
            if self.fail_revoke:
                raise RuntimeError("ClickHouse is away")
            self.revocations.extend(dict(r) for r in rows)
            return
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
    """The tables, the real revocation writes and reads over them, and the audit log."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.accounts: dict[str, list[str]] = {}  # email -> subjects it reaches
        self.elsewhere: dict[str, list[str]] = {}  # subject -> other orgs it is in
        self.table = _Table(self.accounts, self.elsewhere)
        self.audits: list[tuple[str, dict[str, Any]]] = []
        self.revoked_at_audit: list[dict[str, Any]] = []  # SCIM's revocations as each audit lands
        self.claim_limit: int | None = None  # names held at once before a claim runs out
        self.claims: dict[tuple[str, str], asyncio.Lock] = {}  # one holder a name, as a lease
        self.leases: list[str] = []
        self.holding = 0  # SCIM leases held now
        self.waits: list[float] = []  # what the real `wait_out` sleeps

        async def stale_snapshot(subject: str) -> bool:
            raise AssertionError("SCIM reads the store, not this process's snapshot")

        async def emit(event: str, **kw: Any) -> str:
            self.audits.append((event, kw))
            self.revoked_at_audit.append(self.ours)
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
        monkeypatch.setattr(revocation, "revoked_outright", stale_snapshot)
        monkeypatch.setattr(revocation, "refresh", no_refresh)
        monkeypatch.setattr(audit, "emit", emit)

    def person(self, email: str, *subjects: str) -> None:
        self.accounts[email] = list(subjects)

    @property
    def events(self) -> list[str]:
        return [e for e, _ in self.audits]

    @property
    def ours(self) -> dict[str, datetime | None]:
        """SCIM's revocations: each subject's cutoff, None when outright."""
        rows = self.table.revoked().items()
        prefix = revocation.SCIM_PREFIX
        return {k.removeprefix(prefix): r["issued_before"] for k, r in rows if k.startswith(prefix)}

    @property
    def operators(self) -> dict[str, datetime | None]:
        """The subjects' own rows, which only an operator writes."""
        rows = self.table.revoked().items()
        return {k: r["issued_before"] for k, r in rows if not k.startswith("\x00")}

    async def keeps_out(self, subject: str, *, issued_at: int | None = None) -> bool:
        """Whether a token of `subject` issued at `issued_at` (now) is dead, by
        the combined rule the token list and the token check read."""
        when = int(time.time()) if issued_at is None else issued_at
        revoked = await revocation.cutoffs([subject])
        return subject in revoked and (revoked[subject] is None or when < revoked[subject])


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _World(monkeypatch)


async def _create(email: str, *, active: bool = True) -> str:
    return str((await scim.create(email, email, active=active))["id"])


async def test_deactivating_revokes_the_person_once_however_often_it_is_sent(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("Lee@Example.com")
    assert uid == scim.user_id("lee@example.com") and world.ours == {}

    row = await scim.set_active(uid, False)
    assert row is not None and not row["active"] and row["subjects"] == ["lee"]
    assert world.ours == {"lee": None} and world.operators == {}
    assert world.events == ["auth.scim_deactivated"]
    written, revoked = len(world.table.rows), len(world.table.revocations)

    await scim.set_active(uid, False)  # the provider's retry
    assert world.events == ["auth.scim_deactivated"] and len(world.table.rows) == written
    assert len(world.table.revocations) == revoked
    assert await _DEACTIVATED("lee@example.com")


async def test_an_email_reaching_several_accounts_revokes_them_all(world) -> None:
    # Two orgs, or a linked password account beside an SSO one: sign-in
    # refuses to pick, and deactivation must not either.
    world.person("lee@example.com", "lee", "lee-other-org")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    assert world.ours == {"lee": None, "lee-other-org": None}
    await scim.set_active(uid, True)
    assert sorted(world.ours) == ["lee", "lee-other-org"] and None not in world.ours.values()


async def test_a_subject_someone_else_has_elsewhere_is_revoked_and_flagged(world) -> None:
    # JIT and self-signup name subjects, so another org may have an `alice` too.
    world.person("alice@example.com", "alice")
    world.elsewhere["alice"] = ["other-org"]
    await scim.set_active(await _create("alice@example.com"), False)
    assert world.ours == {"alice": None}  # fail closed
    [(collateral, revoked)] = [
        (kw, ours)
        for (e, kw), ours in zip(world.audits, world.revoked_at_audit, strict=True)
        if e == "auth.scim_collateral"
    ]
    assert collateral["subject"]["orgs"] == ["other-org"]
    assert "alice" not in str(collateral["subject"]["subject"])
    assert revoked == {"alice": None}  # audited once the revocation landed


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


async def _given_back(subject: str, cutoff: datetime | None = None) -> datetime:
    """`loom revoke-token --subject <it> --issued-before <now> --scim` on a collateral subject."""
    cutoff = cutoff or _now()
    await revocation.scim_revoke(subject, issued_before=cutoff, reason="", revoked_by="cli:ops")
    return cutoff


async def _operator(subject: str, **kw: Any) -> None:
    """`loom revoke-token --subject <it>`, with `--expires-at` or `--issued-before` in `kw`."""
    await revocation.revoke(subject, reason="by hand", revoked_by="cli:ops", **kw)


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
    cutoff = await _given_back("lee")
    older = _made_since(world, "older", "lee", cutoff - timedelta(seconds=1))  # dead
    after = _made_since(world, "after", "lee", cutoff)  # issued at the cutoff: live
    _made_since(world, "older-child", older, cutoff)  # its maker is dead
    _made_since(world, "after-child", after, cutoff)
    return uid


async def test_a_repeated_deactivation_leaves_an_operators_narrowing_alone(world) -> None:
    uid = await _deactivated_then_narrowed(world)
    world.person("lee@example.com", "lee", "lee-new")
    cutoff = world.ours["lee"]
    await scim.set_active(uid, False)  # the provider sends it again
    assert world.ours == {"lee": cutoff, "lee-new": None}  # only the account found since
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
    await _given_back("lee", made - timedelta(seconds=10))
    _made_since(world, "child-bot", parent, made + timedelta(seconds=3))
    await scim.set_active(uid, False)  # the provider sends it again
    assert [r["name"] for r in world.table.live_tokens()] == ["child-bot"]


async def test_a_delete_after_an_operators_narrowing_leaves_the_tokens_made_since(world) -> None:
    uid = await _deactivated_then_narrowed(world)
    cutoff = world.ours["lee"]
    await scim.delete(uid)  # a provider deleting the user a while after disabling it
    assert world.ours == {"lee": cutoff}
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
    await scim.create("lee@example.com", "lee@example.com", active=True)  # a reactivation
    assert [sorted(r) for r in world.table.rows] == [sorted(column_names("scim_users"))] * 4
    columns = sorted(column_names("revoked_tokens"))
    assert [sorted(r) for r in world.table.revocations] == [columns] * 2


async def test_a_retry_finishes_a_deactivation_whose_revoke_failed(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    world.table.fail_revoke = True
    with pytest.raises(RuntimeError):
        await scim.set_active(uid, False)
    assert not await _DEACTIVATED("lee@example.com")  # nothing recorded short of the revoke
    world.table.fail_revoke = False
    await scim.set_active(uid, False)
    assert world.ours == {"lee": None} and await _DEACTIVATED("lee@example.com")


async def test_reactivating_lifts_the_refusal_and_keeps_old_tokens_revoked(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    before = time.time()
    row = await scim.set_active(uid, True)
    after = time.time()
    assert row is not None and row["active"]
    assert not await _DEACTIVATED("lee@example.com")
    [(subject, narrowed)] = world.ours.items()
    assert subject == "lee" and narrowed is not None
    cutoff = narrowed.replace(tzinfo=UTC)
    assert cutoff.timestamp() > before  # tokens issued so far stay dead
    assert world.events == ["auth.scim_deactivated", "auth.scim_reactivated"]
    # Answered once a replica's clock behind this one's is past the cutoff too.
    [wait] = world.waits
    assert before <= cutoff.timestamp() + revocation.CLOCK_SKEW_S - wait <= after

    written = len(world.table.revocations)
    await scim.set_active(uid, True)
    assert len(world.table.revocations) == written and len(world.events) == 2


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
    made = world.ours["lee"]
    assert made is not None
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
    refresh = revocation.refresh

    async def away() -> None:
        raise RuntimeError("ClickHouse is away")

    monkeypatch.setattr(revocation, "refresh", away)
    with pytest.raises(RuntimeError):  # written and narrowed, then the re-read failed
        await scim.set_active(uid, True)
    monkeypatch.setattr(revocation, "refresh", refresh)
    waited: list[datetime] = []

    async def wait(cutoff: datetime) -> None:
        waited.append(cutoff)

    monkeypatch.setattr(revocation, "wait_out", wait)
    cutoff = world.ours["lee"]
    assert cutoff is not None
    written = len(world.table.revocations)
    row = await scim.set_active(uid, True)  # the provider's retry narrows nothing
    assert row is not None and row["active"] and len(world.table.revocations) == written
    assert waited == [cutoff.replace(tzinfo=UTC)]


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


def _issued(seconds_from_now: int = 0) -> int:
    return int(time.time()) + seconds_from_now


async def test_an_operator_revocation_survives_deactivate_and_reactivate(world) -> None:
    world.person("lee@example.com", "lee")
    await _operator("lee")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    await scim.set_active(uid, True)
    assert world.operators == {"lee": None} and world.ours["lee"] is not None
    assert await world.keeps_out("lee", issued_at=_issued(60))


async def test_a_reactivation_never_lifts_an_operators_cutoff_ahead(world) -> None:
    # `loom revoke-token --subject lee --issued-before <in two weeks>`.
    world.person("lee@example.com", "lee")
    ahead = _now() + timedelta(days=14)
    await _operator("lee", issued_before=ahead)
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    await scim.set_active(uid, True)
    assert world.operators == {"lee": ahead}
    assert await world.keeps_out("lee", issued_at=_issued(60))


def _operator_writes_meanwhile(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> None:
    """`loom revoke-token --subject lee` (no lease) landing just before SCIM's write."""
    write = revocation.scim_revoke

    async def racing(subject: str, **scim_kw: Any) -> dict[str, Any]:
        await _operator(subject, **kw)
        return await write(subject, **scim_kw)

    monkeypatch.setattr(revocation, "scim_revoke", racing)


async def test_an_operator_time_box_during_a_deactivation_lets_no_one_in_early(
    world, monkeypatch
) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    lapsed = datetime.now(UTC) - timedelta(seconds=1)  # the time-box, once it has run out
    _operator_writes_meanwhile(monkeypatch, expires_at=lapsed)
    await scim.set_active(uid, False)
    assert world.operators == {} and world.ours == {"lee": None}
    assert await world.keeps_out("lee", issued_at=_issued(60))


async def test_an_operator_time_box_after_a_deactivation_lets_no_one_in_early(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    await _operator("lee", expires_at=datetime.now(UTC) + timedelta(days=14))
    await _operator("lee", expires_at=datetime.now(UTC) - timedelta(seconds=1))  # run out
    await scim.set_active(uid, False)  # the provider sends it again
    assert world.ours == {"lee": None}
    assert await world.keeps_out("lee", issued_at=_issued(60))


async def test_an_operator_lift_during_a_deactivation_lets_no_one_in(world, monkeypatch) -> None:
    # A password account linked to the email signs in by password, past the
    # operator's cutoff: SCIM's own revocation still holds it.
    world.person("lee@example.com", "lee")
    await _operator("lee")
    uid = await _create("lee@example.com")
    _operator_writes_meanwhile(monkeypatch, issued_before=_now())
    await scim.set_active(uid, False)
    assert world.ours == {"lee": None} and world.operators["lee"] is not None
    assert await world.keeps_out("lee", issued_at=_issued(60))
    await _operator("lee", issued_before=_now())  # and after it
    assert await world.keeps_out("lee", issued_at=_issued(60))


async def test_scim_never_writes_the_subjects_own_row(world) -> None:
    world.person("lee@example.com", "lee", "lee-ops")
    await _operator("lee-ops", issued_before=_now() + timedelta(days=2))
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    await scim.set_active(uid, False)
    await scim.set_active(uid, True)
    await scim.delete(uid)
    written = [r["jti"] for r in world.table.revocations if r["revoked_by"] == "scim"]
    assert written and all(k.startswith(revocation.SCIM_PREFIX) for k in written)
    assert [r["revoked_by"] for r in world.table.revocations if r["jti"] == "lee-ops"] == [
        "cli:ops"
    ]


async def test_a_reactivation_whose_row_write_failed_lifts_nothing(world) -> None:
    world.person("lee@example.com", "lee", "lee-ops")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    world.table.fail_insert = True
    with pytest.raises(RuntimeError):  # the row goes first, so nothing was narrowed
        await scim.set_active(uid, True)
    world.table.fail_insert = False
    assert world.ours == {"lee": None, "lee-ops": None}
    cutoff = await _given_back("lee-ops")  # an operator gave this one back meanwhile
    await scim.set_active(uid, False)  # the row still says inactive
    assert world.ours == {"lee": None, "lee-ops": cutoff}


async def test_a_reactivation_leaves_a_subject_given_back_as_it_is(world) -> None:
    world.person("lee@example.com", "lee", "alice")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    cutoff = await _given_back("alice", _now() - timedelta(hours=1))
    await scim.set_active(uid, True)
    assert world.ours["alice"] == cutoff and world.ours["lee"] is not None


async def test_an_operators_cutoff_ahead_is_never_waited_out(world) -> None:
    # `--scim --issued-before <in 40 s>`: a reactivation, its retry and a
    # retried re-create answer at once, and the operator's cutoff stays.
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    await scim.set_active(uid, False)
    ahead = await _given_back("lee", _now() + timedelta(seconds=40))
    await scim.set_active(uid, True)
    await scim.set_active(uid, True)
    with pytest.raises(scim.Exists):
        await _create("lee@example.com")
    assert world.waits == [] and world.ours == {"lee": ahead}


async def test_a_deactivation_whose_row_write_failed_is_lifted_by_active_true(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    world.table.fail_insert = True
    with pytest.raises(RuntimeError):  # the revocation landed, the row did not
        await scim.set_active(uid, False)
    world.table.fail_insert = False
    assert world.ours == {"lee": None} and not await _DEACTIVATED("lee@example.com")
    row = await scim.set_active(uid, True)  # the provider gives up and reactivates
    assert row is not None and row["active"] and world.ours["lee"] is not None
    assert not await world.keeps_out("lee", issued_at=_issued(60))


async def test_an_email_with_no_account_is_refused_by_email(world) -> None:
    uid = await _create("new@example.com", active=False)
    assert await _DEACTIVATED("new@example.com") and world.ours == {}
    assert (await scim.get(uid) or {})["subjects"] == []
    # An account made since (by an operator) is revoked when the retry finds it.
    world.person("new@example.com", "u-new")
    await scim.set_active(uid, False)
    assert world.ours == {"u-new": None}


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
    assert again["id"] == uid and again["active"] and world.ours["lee"] is not None


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


def test_a_retried_inactive_create_finishes_the_token_list(client: TestClient, world) -> None:
    world.person("lee@example.com", "lee")
    world.table.tokens.append(_saved_token("laptop", "lee"))
    user = {"userName": "lee@example.com", "active": False}
    world.table.fail_token_insert = "laptop"
    assert client.post("/scim/v2/Users", json=user).status_code == 503
    assert len(world.table.live_tokens()) == 1
    world.table.fail_token_insert = None
    assert client.post("/scim/v2/Users", json=user).status_code == 409  # the provider retries
    assert world.table.live_tokens() == []


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
    assert world.ours == {"lee": None}

    put = client.put(f"/scim/v2/Users/{uid}", json={"userName": "lee@example.com", "active": True})
    assert put.json()["active"] is True and world.ours["lee"] is not None
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
    assert world.ours == {"lee": None}


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
    assert world.ours == {"alice": None}
    await scim.set_active(b, True)  # the last one holding her lets her go
    assert list(world.ours) == ["alice"] and world.ours["alice"] is not None


async def test_active_true_on_an_active_user_keeps_a_subject_another_inactive_user_reaches(
    world,
) -> None:
    world.person("a@example.com", "alice", "a-own")
    world.person("b@example.com", "alice")
    a, b = await _create("a@example.com"), await _create("b@example.com")
    await scim.set_active(b, False)
    await scim.set_active(a, True)  # a provider's PATCH of A, still active
    await scim.update(a, active=True, user_name="a@example.com")  # and its PUT
    with pytest.raises(scim.Exists):
        await _create("a@example.com")  # and a retried create
    assert world.ours == {"alice": None}
    assert not await world.keeps_out("a-own", issued_at=_issued(60))


async def test_a_create_lifts_what_a_failed_inactive_create_revoked(world) -> None:
    world.person("lee@example.com", "lee")
    world.table.fail_insert = True
    with pytest.raises(RuntimeError):  # the revocation landed, the row did not
        await _create("lee@example.com", active=False)
    world.table.fail_insert = False
    assert world.ours == {"lee": None}
    await _create("lee@example.com")  # the provider creates the person active
    assert not await world.keeps_out("lee", issued_at=_issued(60))


async def test_a_retried_create_lifts_what_a_failed_deactivation_revoked(world) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    world.table.fail_insert = True
    with pytest.raises(RuntimeError):
        await scim.set_active(uid, False)
    world.table.fail_insert = False
    with pytest.raises(scim.Exists):  # the provider re-sends the create it timed out on
        await _create("lee@example.com")
    assert not await world.keeps_out("lee", issued_at=_issued(60))


async def test_a_reactivation_lifts_an_account_a_failed_second_pass_revoked(
    world, monkeypatch
) -> None:
    world.person("lee@example.com", "lee")
    uid = await _create("lee@example.com")
    write = scim._write
    writes = 0

    async def sign_in_meanwhile(row: dict[str, Any]) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:  # a first sign-in's JIT lands as the row says inactive
            world.person("lee@example.com", "lee", "u-lee")
        else:
            world.table.fail_insert = True  # and the pass that revokes it fails at the row
        await write(row)

    monkeypatch.setattr(scim, "_write", sign_in_meanwhile)
    with pytest.raises(RuntimeError):
        await scim.set_active(uid, False)
    monkeypatch.setattr(scim, "_write", write)
    world.table.fail_insert = False
    assert world.ours == {"lee": None, "u-lee": None}
    assert world.table.newest(uid)["subjects"] == ["lee"]  # type: ignore[index]
    await scim.set_active(uid, True)
    assert not await world.keeps_out("u-lee", issued_at=_issued(60))


def test_a_patch_refused_for_an_email_still_deactivates(client: TestClient, world) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    ops = [
        {"op": "replace", "path": "active", "value": False},
        {"op": "replace", "path": "userName", "value": "new@example.com"},
    ]
    r = client.patch(f"/scim/v2/Users/{uid}", json={"Operations": ops})
    assert r.status_code == 400 and r.json()["scimType"] == "mutability"
    assert world.ours == {"lee": None}  # fail closed
    assert client.get(f"/scim/v2/Users/{uid}").json()["active"] is False


def test_a_put_refused_for_an_email_still_deactivates(client: TestClient, world) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]
    body = {"userName": "lee@example.com", "emails": [{"value": "b@example.com"}], "active": False}
    r = client.put(f"/scim/v2/Users/{uid}", json=body)
    assert r.status_code == 400 and r.json()["scimType"] == "mutability"
    assert world.ours == {"lee": None}


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
    assert world.ours == {"lee": None}


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
    assert world.ours == {}  # nothing applied: the request is about another user


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
    world.leases.clear()
    await scim.set_active(uid, True)  # a provider's push: nothing left to lift
    assert world.leases == [f"scim-user:{uid}"]


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
    caplog.clear()
    with caplog.at_level("WARNING", logger="server.auth.scim"):
        await scim.set_active(a, True)  # a provider's push of A, already active
    assert "another deactivated user" not in caplog.text


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
    assert world.ours == {"lee": None}


def test_a_failed_request_still_deactivates(client: TestClient, world, monkeypatch) -> None:
    world.person("lee@example.com", "lee")
    uid = client.post("/scim/v2/Users", json={"userName": "lee@example.com"}).json()["id"]

    def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(scim_routes, "patched_names", broken)
    r = _patch(client, uid, {"op": "replace", "path": "active", "value": False})
    assert r.status_code == 503 and world.ours == {"lee": None}


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
    assert world.ours == {"lee": None, "u-lee": None}
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


# ── the invariants, over many sequences with failures ─────────────────────

_PEOPLE = {"a": "a@example.com", "b": "b@example.com"}
_REACHES = {"a": ["inv-a", "inv-shared"], "b": ["inv-b", "inv-shared"]}  # one subject shared
_OPS: list[tuple[str, str, bool]] = [
    *[(op, p, on) for p in _PEOPLE for op in ("create", "patch") for on in (True, False)],
    *[(op, p, True) for p in _PEOPLE for op in ("put", "delete", "jit")],
    *[(op, s, True) for s in ("inv-a", "inv-shared") for op in ("operator", "restore")],
]
# The write that fails: the user row's, a deactivation's revocation or a
# reactivation's lift, the first or the second of the request's. "jit": a first
# sign-in's account lands as the first row is written, and the second row fails.
_FAULTS = [
    *[(kind, n) for kind in ("row", "revoke", "lift") for n in (0, 1)],
    ("jit", 1),
]
_Unit = tuple[tuple[str, str, bool], tuple[str, int] | None, bool]


class _Run:
    """A world taken through a sequence of requests, its writes failing where told."""

    def __init__(self, world: _World) -> None:
        self.world = world
        self.fault: tuple[str, int] | None = None
        self.who = ""
        self.seen: dict[str, int] = {}
        self.dirty: set[str] = set()  # people whose last SCIM request failed
        self.operator: set[str] = set()  # subjects an operator revoked outright
        insert = world.table.insert

        async def insert_or_fail(table: str, rows: list[dict[str, Any]]) -> None:
            kind = "row" if table == scim.TABLE else "other"
            if table == "loom.revoked_tokens" and rows[0]["revoked_by"] == "scim":
                kind = "lift" if rows[0]["issued_before"] else "revoke"
            n = self.seen.get(kind, 0)
            self.seen[kind] = n + 1
            if self.fault and kind == ("row" if self.fault[0] == "jit" else self.fault[0]):
                if self.fault[0] == "jit" and n == 0:
                    self.jit(self.who)
                if n == self.fault[1]:
                    raise RuntimeError("injected")
            await insert(table, rows)

        world.table.insert = insert_or_fail  # type: ignore[method-assign]

    def jit(self, person: str) -> None:
        if person in _PEOPLE and f"inv-{person}-jit" not in self.world.accounts[_PEOPLE[person]]:
            self.world.accounts[_PEOPLE[person]].append(f"inv-{person}-jit")

    async def step(self, op: tuple[str, str, bool], fault: tuple[str, int] | None) -> bool:
        """Send `op`; whether the injected failure fired."""
        self.fault, self.who, self.seen = fault, op[1], {}
        try:
            settled = await self._send(*op)
        except RuntimeError:
            self.dirty.add(op[1])
            return True
        if settled:
            self.dirty.discard(op[1])
        return False

    async def _send(self, kind: str, arg: str, on: bool) -> bool:
        """Whether the request settles the person: finishes what a failed one left."""
        email = _PEOPLE.get(arg, "")
        if kind == "create":
            before = self.world.table.newest(scim.user_id(email))
            try:
                await scim.create(email, email, active=on)
            except scim.Exists:
                return before is not None and bool(before["active"]) == on
        elif kind == "patch":
            return await scim.set_active(scim.user_id(email), on) is not None
        elif kind == "put":
            entry = {"type": "work", "primary": True}
            return (
                await scim.update(scim.user_id(email), active=True, email_entry=entry) is not None
            )
        elif kind == "delete":
            await scim.delete(scim.user_id(email))
            return self.world.table.newest(scim.user_id(email)) is not None
        elif kind == "jit":
            self.jit(arg)
            return False
        elif kind == "operator":
            await _operator(arg)
            self.operator.add(arg)
            return False
        else:
            await _given_back(arg)
            return False
        return True

    async def check(self) -> None:
        w = self.world
        rows = {p: w.table.newest(scim.user_id(e)) for p, e in _PEOPLE.items()}
        keys = w.table.revoked()
        later = _issued(60)  # a fresh sign-in's token, past any cutoff so far
        # (b) An operator's revocation stands, and SCIM never narrows one it did not write.
        assert w.operators == dict.fromkeys(self.operator)
        for s in self.operator:
            assert await w.keeps_out(s, issued_at=later), s
        history = sorted(w.table.revocations, key=lambda r: r["version"])
        for key in {r["jti"] for r in history}:
            mine = [r for r in history if r["jti"] == key]
            for before, r in zip(mine, mine[1:], strict=False):
                if r["revoked_by"] == "scim" and r["issued_before"] is not None:
                    assert before["revoked_by"] == "scim" and before["issued_before"] is None
        # (a) An inactive user's subjects stay revoked, unless an operator gave one back.
        for row in rows.values():
            for s in row["subjects"] if row and not row["active"] else []:
                given_back = keys.get(revocation.SCIM_PREFIX + s, {}).get("revoked_by") == "cli:ops"
                assert given_back or await w.keeps_out(s, issued_at=later), (s, row)
        # (c) A subject only active, settled users reach lets a fresh token in.
        for s in sorted({s for subjects in w.accounts.values() for s in subjects}):
            reachers = [p for p, e in _PEOPLE.items() if s in w.accounts[e]]
            held = [p for p in reachers if p in self.dirty or (rows[p] and not rows[p]["active"])]
            if not held and s not in self.operator:
                assert not await w.keeps_out(s, issued_at=later), s

    def state(self) -> tuple[Any, ...]:
        """The users and the revocations, a cutoff's time aside."""
        w = self.world
        rows = [w.table.newest(scim.user_id(e)) for e in _PEOPLE.values()]
        users = tuple(
            (r["active"], r["deleted"], tuple(r["subjects"])) if r else None for r in rows
        )
        keys = [
            (k, r["revoked_by"], r["issued_before"] is None) for k, r in w.table.revoked().items()
        ]
        return users, tuple(sorted(keys))


async def _run(units: list[_Unit], fired: list[bool] | None = None) -> tuple[list[Any], list[bool]]:
    """The state after each unit and whether its failure fired. With `fired` (a
    run before), a unit whose failure fired and was retried is sent once, clean."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(revocation, "_revoked_jtis", frozenset())
        mp.setattr(revocation, "_revoked_before", {})
        mp.setattr(revocation, "_recent", [])
        world = _World(mp)
        for p, subjects in _REACHES.items():
            world.person(_PEOPLE[p], *subjects)
        run = _Run(world)
        states, fires = [], []
        for i, (op, fault, retried) in enumerate(units):
            if fired is not None and fired[i] and retried:
                if fault and fault[0] == "jit":
                    run.jit(op[1])  # what the race left, there before this time
                fires.append(await run.step(op, None))
            else:
                fires.append(await run.step(op, fault))
                await run.check()
                if retried:
                    await run.step(op, None)
            await run.check()
            states.append(run.state())
        return states, fires


async def test_every_sequence_keeps_the_invariants_and_a_retry_converges() -> None:
    # Two people sharing a subject, each with one of their own, through random
    # requests with a write failing in about half; most failures are retried.
    rng = random.Random(26)
    runs = 0
    for _ in range(10_000):
        units: list[_Unit] = []
        for _ in range(rng.randint(2, 7)):
            fault = rng.choice(_FAULTS) if rng.random() < 0.5 else None
            units.append((rng.choice(_OPS), fault, fault is not None and rng.random() < 0.7))
        try:
            states, fired = await _run(units)
            if any(fired):  # (d) a retry ends where a clean request does
                clean, _ = await _run(units, fired)
                assert states == clean
                runs += 1
        except AssertionError as e:
            raise AssertionError(f"{units}: {e}") from e
    assert runs > 1500  # failures fired and were compared, not just drawn
