"""Person decisions use bounded projections on the actual ClickHouse client."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.auth import authz, oauth, scim, service_accounts
from server.core import cell, ch
from server.settings import settings
from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


class People:
    def __init__(self, client: ch.CHClient):
        self.client = client
        self.org = unique_org("scim-person")
        self.run = uuid.uuid4().hex[:10]
        self.emails: list[str] = []
        self.results: list[tuple[str, dict[str, Any], Any]] = []

    def email(self, label: str) -> str:
        email = f"{label}-{self.run}@example.test"
        self.emails.append(email)
        return email

    async def account(self, owner: str, email: str, *, password: bool = False) -> str:
        namespace = "account-" + uuid.uuid4().hex[:12]
        now = datetime.now(UTC)
        await self.client.insert(
            "loom.namespaces",
            [
                {
                    "org": self.org,
                    "namespace": namespace,
                    "owner_email": owner,
                    "contact_email": email,
                    "password_hash": "unit-hash" if password else "",
                    "active": True,
                    "created_at": now,
                    "updated_at": now,
                }
            ],
        )
        return namespace

    async def provision(
        self, email: str, *, active: bool = True, deleted: bool = False, work: tuple[str, ...] = ()
    ) -> None:
        now = datetime.now(UTC)
        await self.client.insert(
            "loom.scim_users",
            [
                {
                    "id": scim.user_id(email),
                    "user_name": email,
                    "email": email,
                    "emails": "[]",
                    "addresses": [email, *work],
                    "work_addresses": list(work),
                    "active": active,
                    "deleted": deleted,
                    "subjects": [],
                    "created_at": now,
                    "updated_at": now,
                    "version": time.time_ns(),
                }
            ],
        )

    async def personal(
        self,
        subject: str,
        creator: str,
        *,
        lineage: list[str] | None = None,
        expires_at: datetime | None = None,
        revoked_at: datetime | None = None,
    ) -> None:
        await self.client.insert(
            "loom.service_accounts",
            [
                {
                    "org": self.org,
                    "name": subject.removeprefix("sa:"),
                    "sa_subject": subject,
                    "namespaces": [],
                    "roles": [],
                    "created_by": creator,
                    "created_at": datetime.now(UTC),
                    "lineage": lineage or [],
                    "inherit_creator_grants": True,
                    "kind": "personal",
                    "expires_at": expires_at,
                    "revoked_at": revoked_at,
                    "version": time.time_ns(),
                }
            ],
        )

    async def refused(self, subject: str, **kwargs: Any) -> bool:
        authz.forget_ended("identity", "")
        return await scim.refuses_subject(subject, org=self.org, **kwargs)


@pytest.fixture
async def people(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[People]:
    client = ch.get_client()
    await client.connect()
    made = People(client)
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "scim_token", "unit-native-person-policy")
    monkeypatch.setattr(cell, "mode", lambda: "enforced")
    monkeypatch.setattr(cell, "deployment_id", lambda: "unit-native-person-deployment")
    monkeypatch.setattr(authz, "_ENDED", {})
    monkeypatch.setattr(service_accounts, "_creators", {})
    query_all, query_one = client.query_all, client.query_one

    async def all_rows(sql: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        answer = await query_all(sql, params=params, **kwargs)
        made.results.append((sql, ch.statement_settings(sql), answer))
        return answer

    async def one_row(sql: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        answer = await query_one(sql, params=params, **kwargs)
        made.results.append((sql, ch.statement_settings(sql), answer))
        return answer

    monkeypatch.setattr(client, "query_all", all_rows)
    monkeypatch.setattr(client, "query_one", one_row)
    try:
        yield made
    finally:
        first_error: BaseException | None = None
        try:
            if made.emails:
                await client.execute(
                    "ALTER TABLE loom.scim_users DELETE WHERE id IN {ids:Array(String)} "
                    "SETTINGS mutations_sync = 2",
                    params={"ids": [scim.user_id(e) for e in made.emails]},
                )
        except BaseException as exc:  # noqa: BLE001 — disconnect must still run
            first_error = exc
        try:
            await client.disconnect()
        except BaseException as exc:  # noqa: BLE001 — preserve the first cleanup failure
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


async def test_verified_reverse_owner_still_requires_its_own_email(people: People) -> None:
    owner, contact = people.email("owner"), people.email("contact")
    await people.account(owner, contact)
    await people.provision(contact)
    assert await people.refused(contact)
    await people.provision(owner, work=(contact,))
    assert not await people.refused(contact)
    await people.provision(owner, deleted=True)
    assert await people.refused(contact)


async def test_password_contact_only_refuses_until_a_verified_pair_exists(people: People) -> None:
    contact = people.email("password")
    namespace = await people.account("owner", contact, password=True)
    await people.provision(contact)
    assert await people.refused("owner")
    await people.client.insert(
        "loom.sso_links",
        [
            {
                "email": contact,
                "org": people.org,
                "namespace": namespace,
                "linked": 1,
                "linked_by": "test",
                "linked_at": datetime.now(UTC),
                "version": time.time_ns(),
            }
        ],
    )
    assert not await people.refused("owner")


async def test_owner_ambiguity_is_not_hidden_by_limit(people: People) -> None:
    contact = people.email("shared")
    await people.account("first-owner", contact)
    await people.account("second-owner", contact)
    await people.provision(contact)
    assert await people.refused(contact)


@pytest.mark.parametrize("count,refused", [(1024, False), (1025, True)])
async def test_native_contact_aggregate_does_not_accept_a_truncated_prefix(
    people: People, count: int, refused: bool
) -> None:
    email = people.email("bounded")
    now = datetime.now(UTC)
    await people.client.insert(
        "loom.namespaces",
        [
            {
                "org": people.org,
                "namespace": f"account-{i}",
                "owner_email": "owner",
                "contact_email": email,
                "active": True,
                "created_at": now,
                "updated_at": now,
            }
            for i in range(count)
        ],
    )
    await people.provision(email)
    assert await people.refused("owner") is refused
    aggregate = next(row for sql, _, row in people.results if "AS account_count" in sql)
    assert aggregate["account_count"] == count
    assert len(aggregate["accounts"]) == (0 if refused else count)


async def test_native_contact_bytes_return_only_overflow_metadata(people: People) -> None:
    await people.account("owner", "x" * 65537)
    assert await people.refused("owner")
    aggregate = next(row for sql, _, row in people.results if "AS account_count" in sql)
    assert aggregate["account_bytes"] == 65537
    assert aggregate["accounts"] == []
    assert len(json.dumps(aggregate).encode()) < 1024


async def test_work_alias_lookup_transfers_only_the_requested_intersection(people: People) -> None:
    owner, contact = people.email("owner"), people.email("work")
    await people.account("owner", owner)
    await people.account("owner", contact, password=True)
    unrelated = tuple(f"unused-{n}@example.test" for n in range(3000))
    await people.provision(owner, work=(*unrelated, contact))
    assert not await people.refused("owner")
    alias = next(row for sql, _, row in people.results if "groupUniqArray" in sql)
    assert alias == {"qualified": [contact]}
    assert len(json.dumps(alias).encode()) < 256


@pytest.mark.parametrize("lineage_count,refused", [(1023, False), (1024, True)])
async def test_native_repeated_edges_share_the_aggregate_budget(
    people: People, lineage_count: int, refused: bool
) -> None:
    creator = people.email("creator")
    await people.provision(creator)
    await people.personal("sa:personal:one", creator, lineage=[creator] * lineage_count)
    assert await people.refused("sa:personal:one") is refused


async def test_native_fresh_reader_does_not_reuse_the_inheritance_cache(people: People) -> None:
    creator = people.email("creator")
    await people.provision(creator)
    await people.personal("sa:personal:one", creator)
    cached = await service_accounts.sa_creator(people.org, "sa:personal:one")
    assert cached is not None and cached.subject == creator
    await people.personal("sa:personal:one", creator, revoked_at=datetime.now(UTC))
    assert await people.refused("sa:personal:one")
    current = await service_accounts._creator_row(people.org, "sa:personal:one")
    assert current is not None and current["revoked_at"] is not None


async def test_native_fresh_reader_projects_an_oversized_creator_to_metadata(
    people: People,
) -> None:
    await people.personal("sa:personal:one", "x" * 65537)
    assert await people.refused("sa:personal:one")
    current = await service_accounts._creator_row(people.org, "sa:personal:one")
    assert current is not None and current["created_by"] == ""
    assert current["subject_bytes"] == 65537
    assert current["lineage"] == []
    assert await service_accounts.sa_creator(people.org, "sa:personal:one") is None


@pytest.mark.parametrize("expired", [False, True])
async def test_native_expiry_uses_the_current_service_account_row(
    people: People, expired: bool
) -> None:
    creator = people.email("creator")
    await people.provision(creator)
    expires = datetime.now(UTC) + timedelta(minutes=-1 if expired else 1)
    await people.personal("sa:personal:one", creator, expires_at=expires)
    assert await people.refused("sa:personal:one") is expired


@pytest.mark.parametrize("read_timeout", [0, 2.5])
async def test_actual_client_accepts_finite_remaining_statement_budgets(
    people: People, monkeypatch: pytest.MonkeyPatch, read_timeout: float
) -> None:
    monkeypatch.setattr(settings, "ch_timeout_read_s", read_timeout)
    email = people.email("budget")
    await people.account("owner", email)
    await people.provision(email)
    people.results.clear()
    with ch.statement_timeout(1.75):
        assert not await people.refused("owner")
    assert len(people.results) >= 3
    for _, applied, _ in people.results:
        assert 0 < applied["max_execution_time"] <= 1.75


async def test_real_person_queries_report_their_physical_work(
    people: People, record_property: Callable[[str, Any], None]
) -> None:
    email = people.email("query-log")
    await people.account("owner", email)
    await people.provision(email)
    before = await people.client.query_one("SELECT now64(6) AS started")
    assert before is not None
    assert not await people.refused("owner")
    await people.client.execute("SYSTEM FLUSH LOGS")
    rows = await people.client.query_all(
        "SELECT query_duration_ms, read_rows, read_bytes, memory_usage FROM system.query_log "
        "WHERE type = 'QueryFinish' AND log_comment = 'auth.scim_people' "
        "AND event_time_microseconds >= {started:DateTime64(6)} "
        "ORDER BY event_time_microseconds LIMIT 20",
        params={"started": before["started"]},
    )
    assert len(rows) >= 3
    assert any(int(row["read_rows"]) > 0 for row in rows)
    record_property("scim_person_query_log", json.dumps(rows, default=str))


async def test_callback_resolves_verified_work_alias_before_enforcement(
    people: People, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, contact = people.email("callback-owner"), people.email("callback-contact")
    namespace = await people.account(owner, contact)
    await people.provision(owner, work=(contact,))
    monkeypatch.setattr(settings, "oidc_jit_provision", False)
    observed = await oauth._account_for(contact, {"kind": "dashboard"}, "unused-upstream")
    assert observed == (owner, people.org, [namespace], ["writer"])
