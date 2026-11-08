"""The personal-namespace auto-claim keeps working past its cache size, counts a
grant under any spelling of the person, and passes over a name already in use."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import personal_namespace as pn


class _CH:
    def __init__(self, owners: set[str]) -> None:
        self.owners = owners
        self.used: set[str] = set()
        self.inserts: list[str] = []

    async def query_one(self, sql: str, params: Any = None) -> Any:
        if "AS in_use" in sql:
            return {"in_use": 1} if params["ns"] in self.used else None
        if "namespace_grants" in sql:
            return {"x": 1} if set(params["subs"]) & self.owners else None
        return None

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return []

    async def insert(self, table: str, rows: list[dict[str, Any]], **k: Any) -> None:
        if table == "loom.namespaces":
            self.inserts.append(rows[0]["namespace"])


@pytest.fixture()
def ch(monkeypatch: pytest.MonkeyPatch) -> _CH:
    fake = _CH(owners={"alice@acme.io"})
    monkeypatch.setattr("server.core.ch.get_client", lambda: fake)

    async def aliases(org: str, subject: str, **_: Any) -> list[str]:
        return [subject, "alice@acme.io"] if subject == "alice" else [subject]

    monkeypatch.setattr("server.auth.aliases.subject_aliases", aliases)
    pn._seen.clear()
    return fake


def _ensure(subject: str) -> None:
    asyncio.run(
        pn.ensure_personal_namespace_cached(org="o", subject=subject, is_service_account=False)
    )


def test_a_full_cache_still_provisions_new_subjects(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pn, "_MAX_CACHE", 3)
    for i in range(5):
        _ensure(f"user{i}@x.io")
    assert ch.inserts == [f"personal-user{i}" for i in range(5)]


def test_a_grant_under_the_persons_email_counts_as_owned(ch: _CH) -> None:
    _ensure("alice")
    assert ch.inserts == []


def test_the_auto_claim_writes_the_shared_account_shape(ch: _CH) -> None:
    """A personal namespace is created by the one account builder every other
    path uses, so its row carries the same columns (classification,
    join_policy, contact_email, …) as a signed-up account's."""
    rows: dict[str, list[dict[str, Any]]] = {}

    async def insert(table: str, batch: list[dict[str, Any]], **k: Any) -> None:
        rows.setdefault(table, []).extend(batch)

    ch.insert = insert  # type: ignore[method-assign]
    assert asyncio.run(pn.ensure_personal_namespace(ch, "o", "bob@x.io")) == "personal-bob"
    ns_row = rows["loom.namespaces"][0]
    assert ns_row["owner_email"] == "bob@x.io" and ns_row["classification"] == "standard"
    assert ns_row["password_hash"] == "" and ns_row["join_policy"] == "inherit"
    (grant,) = rows["loom.namespace_grants"]
    assert (grant["subject"], grant["role"], grant["namespace"]) == (
        "bob@x.io",
        "admin",
        "personal-bob",
    )


def test_a_pending_join_request_is_not_an_owned_namespace(ch: _CH) -> None:
    """A subject whose only grant row is a pending request (namespace.join on
    an approval-gated namespace) holds nothing yet, so it still gets its
    personal namespace."""

    async def query_one(sql: str, params: Any = None) -> Any:
        if "namespace_grants" in sql:
            # the only row on file is status='pending'
            return None if "status = 'active'" in sql else {"x": 1}
        return None

    ch.query_one = query_one  # type: ignore[method-assign]
    _ensure("carol@x.io")
    assert ch.inserts == ["personal-carol"]


@pytest.fixture(autouse=True)
def _claims_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment-wide claims, without a lock table in the fake."""
    from tests.unit.claims_fake import memory_claims

    memory_claims(monkeypatch)


def test_a_name_holding_data_with_no_row_is_passed_over(ch: _CH) -> None:
    """Data or a grant under the name with no registry row is someone else's."""
    ch.used = {"personal-bob"}
    _ensure("bob@x.io")
    assert len(ch.inserts) == 1
    assert ch.inserts[0].startswith("personal-bob-")


def test_the_default_namespace_picks_the_name_the_auto_claim_will_provision(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default-namespace picker and the auto-claim pass over the same in-use name, so
    the caller's default is the namespace it is given."""
    from server.auth.jwt import AuthInfo
    from server.core.grant_cache import GrantMap
    from server.identity.app import caller_home

    async def _no_grants(org: str, subject: str) -> GrantMap:
        return GrantMap(ranks={})

    monkeypatch.setattr("server.core.grant_cache.grant_map", _no_grants)
    ch.used = {"personal-bob"}
    bob = AuthInfo(
        subject="bob@x.io", org="o", namespaces=("*",), roles=(), is_service_account=False
    )
    picked = asyncio.run(caller_home._provisioned(bob, "o"))
    _ensure("bob@x.io")
    assert picked.startswith("personal-bob-") and ch.inserts == [picked]


def test_the_auto_claim_holds_the_account_claim_without_waiting(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    """namespace.register holds the account claim, so the two cannot both find the name
    free; a busy one is retried on the next request, not waited for in the auth step."""
    from contextlib import asynccontextmanager

    from server.core.errors import GrantBusyError

    taken: list[tuple[str, bool]] = []
    busy = {"personal-bob"}

    @asynccontextmanager
    async def _account_change(
        org: str, ns: str, *, wait_s: float = 10.0, statement_s: float = 10.0
    ) -> Any:
        taken.append((ns, wait_s == 0 and statement_s < pn.CLAIM_TTL_S * 0.4 / 2))
        if ns in busy:
            raise GrantBusyError("busy")
        yield

    monkeypatch.setattr(pn, "account_change", _account_change)
    with pytest.raises(pn.PersonalNamespaceBusy):
        asyncio.run(pn.ensure_personal_namespace(ch, "o", "bob@x.io"))
    assert taken == [("personal-bob", True)] and ch.inserts == []  # no wait, short statements
    busy.clear()
    assert asyncio.run(pn.ensure_personal_namespace(ch, "o", "bob@x.io")) == "personal-bob"
