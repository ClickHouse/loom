"""The provisioning census uses actual bounded ClickHouse and Keeper projections."""

from __future__ import annotations

import time
import uuid

import pytest

from server.auth import claims
from server.auth.oauth_store import token_id
from server.cli import cell_scim
from tests.integration.test_cell_attach import (
    cell_clickhouse_databases as cell_clickhouse_databases,
)
from tests.integration.test_cell_attach import cell_stores as cell_stores
from tests.integration.test_cell_attach import genuine_cell_processes as genuine_cell_processes
from tests.integration.test_scim_person_live_ch import People
from tests.integration.test_scim_sessions_live_ch import Sessions
from tests.integration.test_scim_sessions_live_ch import sessions as sessions

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.fixture
async def people(cell_stores):
    # The existing native owner creates the database pair and removes it at teardown.
    # Reuse only the row builders; that owner alone closes its connected client.
    yield People(cell_stores["ch"])


async def _page(people: People, kind: str, *, after0="", after1="", bytes_=65536):
    return await cell_scim._query(
        people.client,
        cell_scim._page_sql(kind),
        {"started": 1, "after0": after0, "after1": after1, "bytes": bytes_},
        time.monotonic() + 30,
    )


async def test_account_census_projects_actual_keys_and_refuses_oversized_transfer(people: People):
    owner = people.email("census-owner")
    namespace = await people.account(owner, owner, password=True)
    ownerless = "resource-" + uuid.uuid4().hex
    await people.client.insert(
        "loom.namespaces",
        [{"org": people.org, "namespace": ownerless, "owner_email": "", "active": True}],
    )
    page = await _page(people, "accounts", after0=people.org, after1=namespace[:-1])
    resources = [
        tuple(row) for row in page["identities"] if tuple(row[0:2]) == (people.org, ownerless)
    ]
    assert len(resources) == 1
    assert resources[0][:7] == (people.org, ownerless, people.org, "", "", "", 0)
    identities = [
        tuple(row) for row in page["identities"] if tuple(row[0:2]) == (people.org, namespace)
    ]
    assert len(identities) == 1
    assert identities[0][:7] == (people.org, namespace, people.org, owner, "", "", 0)
    assert int(identities[0][7]) > 0
    assert len(page["identities"]) == page["n"] <= 128
    assert page["bytes"] <= 65536

    overflow = await _page(people, "accounts", after0=people.org, after1=namespace[:-1], bytes_=1)
    assert overflow["n"] > 0
    assert overflow["bytes"] > 1
    assert overflow["identities"] == []


async def test_refresh_census_keeps_verified_email_and_detects_actual_retirement(
    sessions: Sessions,
):
    person = sessions.people
    email = person.email("census-session")
    grant = sessions.grant(email=email)
    token = uuid.uuid4().hex
    rid = token_id(token)
    await sessions.save_refresh(token, grant)
    page = await _page(person, "sessions", after1=rid[:-1])
    identities = [tuple(row) for row in page["identities"] if row[1] == rid]
    assert len(identities) == 1
    identity = identities[0]
    assert identity[:7] == ("", rid, person.org, grant["subject"], email, grant["family"], 0)
    assert await cell_scim._current_session(person.client, identity, time.monotonic() + 30)

    key = "family:" + grant["family"]
    current = await claims.get(person.client, key)
    assert current == "0:" + rid
    await sessions.retire_refresh({**grant, "held": {key: current}})
    assert not await cell_scim._current_session(person.client, identity, time.monotonic() + 30)


async def test_personal_census_uses_the_actual_fresh_creator_graph(people: People):
    email = people.email("census-creator")
    name = "census-personal-" + uuid.uuid4().hex
    subject = "sa:" + name
    await people.personal(subject, email)
    await people.provision(email)
    page = await _page(people, "services", after0=people.org, after1=name[:-1])
    identities = [tuple(row) for row in page["identities"] if tuple(row[0:2]) == (people.org, name)]
    assert len(identities) == 1
    assert identities[0][:7] == (people.org, name, people.org, subject, "", "", 0)
    assert not await cell_scim._refuses_person(people.org, subject, "", time.monotonic() + 30)
    await people.provision(email, active=False)
    assert await cell_scim._refuses_person(people.org, subject, "", time.monotonic() + 30)


async def test_account_keyset_census_keeps_the_row_after_a_full_page(people: People):
    owner = people.email("census-page")
    names = [f"census-{index:04}" for index in range(129)]
    await people.client.insert(
        "loom.namespaces",
        [{"org": people.org, "namespace": name, "owner_email": owner} for name in names],
    )
    first = await _page(people, "accounts", after0=people.org)
    assert first["n"] == 128
    assert {row[1] for row in first["identities"]} == set(names[:128])
    assert {row[0] for row in first["identities"]} == {people.org}
    last = max((row[0], row[1]) for row in first["identities"])
    second = await _page(people, "accounts", after0=last[0], after1=last[1])
    assert {row[1] for row in second["identities"] if row[0] == people.org} == {names[128]}
