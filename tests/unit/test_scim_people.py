"""Provisioning follows each person's verified account and the complete ancestry."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from server.auth import authz, login, machines, scim
from server.core import cell, ch
from server.settings import settings

_DEACTIVATED = scim.deactivated


@dataclass
class People:
    accounts: list[tuple[str, str, str, bool]] = field(default_factory=list)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    bots: dict[str, bool] = field(default_factory=dict)
    reads: list[dict[str, Any]] = field(default_factory=list)
    unavailable: bool = False
    on_read: Callable[[], None] | None = None

    def provision(self, email: str, *, active=True, deleted=False, work=()):
        email = scim.normalized(email)
        self.users[scim.user_id(email)] = {
            "id": scim.user_id(email),
            "email": email,
            "active": active,
            "deleted": deleted,
            "work_addresses": set(work),
        }

    async def query_all(self, sql, params=None):
        p = params or {}
        self.reads.append(p)
        if self.on_read is not None:
            self.on_read()
        if self.unavailable:
            raise ConnectionError("identity store unavailable")
        if "loom.namespaces" in sql and "SELECT DISTINCT" in sql:
            owners = {
                owner.lower()
                for org, owner, email, verified in self.accounts
                if org == p["org"]
                and (owner.lower() == p["sub"] or (email.lower() == p["sub"] and verified))
            }
            return [
                {
                    "owner": o if len(o.encode()) <= p["bytes"] else "",
                    "oversized": len(o.encode()) > p["bytes"],
                }
                for o in sorted(owners)[:2]
            ]
        if "loom.scim_users" in sql and "missing" not in p:
            return [self.users[uid] for uid in p["ids"] if uid in self.users]
        raise AssertionError(sql)

    async def query_one(self, sql, params=None):
        p = params or {}
        self.reads.append(p)
        if self.on_read is not None:
            self.on_read()
        if self.unavailable:
            raise ConnectionError("identity store unavailable")
        if "loom.namespaces" in sql:
            accounts = [
                (email.lower(), verified)
                for org, owner, email, verified in self.accounts
                if org == p["org"] and owner.lower() == p["owner"]
            ]
            size = sum(len(email.encode()) for email, _ in accounts)
            return {
                "account_count": len(accounts),
                "account_bytes": size,
                "accounts": accounts if len(accounts) <= p["items"] and size <= p["bytes"] else [],
            }
        if "loom.machine_identities" in sql:
            return {"active": self.bots[p["name"]]} if p["name"] in self.bots else None
        if "loom.service_accounts" in sql:
            row = self.services.get(p["sub"])
            if row is None:
                return None
            above = row.get("lineage", [])
            creator = row.get("created_by", "")
            size = sum(len(s.encode()) for s in above) + len(creator.encode())
            return {
                "lineage_count": len(above),
                "subject_bytes": size,
                "inherit_creator_grants": row.get("inherit_creator_grants", False),
                "kind": "personal",
                "revoked_at": row.get("revoked_at"),
                "expires_at": row.get("expires_at"),
                "bounded_creator": creator if len(creator.encode()) <= p["bytes"] else "",
                "bounded_lineage": above if len(above) <= p["items"] and size <= p["bytes"] else [],
            }
        if "loom.scim_users" in sql and "missing" in p:
            qualified = set()
            for uid in p["ids"]:
                row = self.users.get(uid)
                if row and row["active"] and not row["deleted"] and row["email"] in p["witnesses"]:
                    qualified.update(row["work_addresses"] & set(p["missing"]))
            return {"qualified": sorted(qualified)}
        if "loom.scim_users" in sql:
            row = self.users.get(p["id"])
            return {"x": 1} if row and not row["active"] else None
        raise AssertionError(sql)


@pytest.fixture
def people(monkeypatch):
    world = People()
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "scim_token", "lsc_first_" + "a" * 64)
    monkeypatch.setattr(cell, "mode", lambda: "enforced")
    monkeypatch.setattr(cell, "deployment_id", lambda: "unit-deployment")
    monkeypatch.setattr(ch, "get_client", lambda: world)
    monkeypatch.setattr(scim, "get_client", lambda: world)
    monkeypatch.setattr(scim, "deactivated", _DEACTIVATED)
    monkeypatch.setattr(machines, "get_client", lambda: world)
    monkeypatch.setattr(authz, "_ENDED", {})
    return world


@pytest.mark.parametrize(
    "status,refused", [("active", False), ("missing", True), ("inactive", True), ("deleted", True)]
)
async def test_every_person_needs_a_live_own_id(people, status, refused):
    if status != "missing":
        people.provision(
            "alice@example.test", active=status != "inactive", deleted=status == "deleted"
        )
    assert await scim.refuses_subject("alice@example.test", org="acme") is refused


@pytest.mark.parametrize("verified,refused", [(False, True), (True, False)])
async def test_password_contact_cannot_supply_ownership(people, verified, refused):
    people.accounts.append(("acme", "outsider", "assigned@example.test", verified))
    people.provision("assigned@example.test")
    assert await scim.refuses_subject("outsider", org="acme") is refused


@pytest.mark.parametrize("subject", ["alice", "first@example.test"])
async def test_verified_spelling_checks_every_owned_account(people, subject):
    people.accounts.extend(
        [
            ("acme", "alice", "first@example.test", True),
            ("acme", "alice", "second@example.test", False),
        ]
    )
    people.provision("first@example.test")
    assert await scim.refuses_subject(subject, org="acme")


@pytest.mark.parametrize("subject", ["owner@example.test", "contact@example.test"])
@pytest.mark.parametrize("owner_state", ["missing", "inactive", "deleted", "work"])
async def test_email_canonical_owner_is_mandatory_and_an_alias_witness(
    people, subject, owner_state
):
    owner, contact = "owner@example.test", "contact@example.test"
    people.accounts.append(("acme", owner, contact, True))
    if owner_state == "work":
        people.provision(owner, work=(contact,))
    else:
        people.provision(contact)
        if owner_state != "missing":
            people.provision(
                owner, active=owner_state != "inactive", deleted=owner_state == "deleted"
            )
    assert await scim.refuses_subject(subject, org="acme") is (owner_state != "work")


async def test_tombstoned_own_id_cannot_borrow_a_work_alias(people):
    people.accounts.append(("acme", "alice", "primary@example.test", True))
    people.accounts.append(("acme", "alice", "alias@example.test", True))
    people.provision("primary@example.test", work=("alias@example.test",))
    people.provision("alias@example.test", deleted=True)
    assert await scim.refuses_subject("alice", org="acme")


async def test_unrelated_primary_cannot_lend_its_work_alias(people):
    people.provision("unrelated@example.test", work=("alice@example.test",))
    assert await scim.refuses_subject("alice@example.test", org="acme")


async def test_ambiguous_owners_refuse(people):
    people.accounts.extend(
        [
            ("acme", "alice", "shared@example.test", True),
            ("acme", "bob", "shared@example.test", True),
        ]
    )
    people.provision("shared@example.test")
    assert await scim.refuses_subject("shared@example.test", org="acme")


@pytest.mark.parametrize("org", ["other", "*", ""])
async def test_account_ownership_never_crosses_orgs(people, org):
    people.accounts.append(("acme", "alice", "alice@example.test", True))
    people.provision("alice@example.test")
    assert await scim.refuses_subject("alice", org=org)


@pytest.mark.parametrize(
    "also", [("missing@example.test",), ("alice@example.test", "missing@example.test")]
)
async def test_every_additional_root_qualifies_independently(people, also):
    people.provision("alice@example.test")
    assert await scim.refuses_subject("alice@example.test", org="acme", also=also)


async def test_primary_verified_email_does_not_provision_another_root(people):
    people.provision("alice@example.test")
    assert await scim.refuses_subject(
        "alice", org="acme", verified_email="alice@example.test", also=("outsider",)
    )


async def test_service_account_cannot_lend_a_verified_email_to_its_creator(people):
    people.provision("assigned@example.test")
    people.services["sa:personal:one"] = {"created_by": "outsider", "lineage": []}
    assert await scim.refuses_subject(
        "sa:personal:one", org="acme", verified_email="assigned@example.test"
    )


@pytest.mark.parametrize("inherits", [False, True])
async def test_every_personal_service_account_ancestor_qualifies(people, inherits):
    people.provision("alice@example.test")
    people.services["sa:personal:one"] = {
        "created_by": "alice@example.test",
        "lineage": ["missing@example.test"],
        "inherit_creator_grants": inherits,
    }
    assert await scim.refuses_subject("sa:personal:one", org="acme")


async def test_shared_ancestor_is_not_a_cycle(people):
    people.provision("alice@example.test")
    people.services["sa:personal:one"] = {"created_by": "alice@example.test", "lineage": []}
    people.services["sa:personal:two"] = {"created_by": "alice@example.test", "lineage": []}
    assert not await scim.refuses_subject("sa:personal:one", org="acme", also=("sa:personal:two",))


async def test_a_cycle_in_a_later_root_refuses(people):
    people.provision("alice@example.test")
    people.services["sa:personal:one"] = {"created_by": "sa:personal:two", "lineage": []}
    people.services["sa:personal:two"] = {"created_by": "sa:personal:one", "lineage": []}
    assert await scim.refuses_subject("alice@example.test", org="acme", also=("sa:personal:one",))


@pytest.mark.parametrize("extra", [1023, 1024])
async def test_repeated_roots_count_before_deduplication(people, extra):
    people.provision("alice@example.test")
    assert await scim.refuses_subject(
        "alice@example.test", org="acme", also=("alice@example.test",) * extra
    ) is (extra == 1024)
    if extra == 1024:
        assert not people.reads


async def test_root_bytes_refuse_before_reads(people):
    assert await scim.refuses_subject("x" * 65537, org="acme")
    assert not people.reads


async def test_a_larger_graph_cannot_reuse_a_single_root_permit(people):
    people.provision("alice@example.test")
    people.provision("bob@example.test")
    assert not await scim.refuses_subject("alice@example.test", org="acme")
    people.provision("alice@example.test", active=False)
    assert await scim.refuses_subject("alice@example.test", org="acme", also=("bob@example.test",))


@pytest.mark.parametrize("stored_first", [False, True])
async def test_memo_includes_the_verified_session_email(people, stored_first):
    people.provision("alice@example.test")
    emails = ["missing@example.test", ""] if stored_first else ["", "missing@example.test"]
    answers = [
        await scim.refuses_subject("alice@example.test", org="acme", verified_email=email)
        for email in emails
    ]
    assert answers == [bool(email) for email in emails]


async def test_extra_root_order_has_one_complete_memo_identity(people):
    for email in ("alice@example.test", "bob@example.test", "carol@example.test"):
        people.provision(email)
    assert not await scim.refuses_subject(
        "alice@example.test", org="acme", also=("bob@example.test", "carol@example.test")
    )
    people.unavailable = True
    assert not await scim.refuses_subject(
        "alice@example.test", org="acme", also=("carol@example.test", "bob@example.test")
    )


async def test_outage_is_retryable_and_never_cached(people):
    people.unavailable = True
    with pytest.raises(login.SignInUnavailable):
        await scim.refuses_subject("alice@example.test", org="acme")
    people.unavailable = False
    people.provision("alice@example.test")
    assert not await scim.refuses_subject("alice@example.test", org="acme")


@pytest.mark.parametrize("raw", ["", " ", " , ", ",,,"])
async def test_empty_parsed_token_policy_keeps_retained_deactivations(people, monkeypatch, raw):
    monkeypatch.setattr(settings, "scim_token", raw)
    assert not scim.requires_provisioning()
    people.provision("disabled@example.test", active=False)
    assert not await scim.refuses_subject("missing@example.test", org="acme")
    assert await scim.refuses_subject("disabled@example.test", org="acme")


@pytest.mark.parametrize("scim_on", [False, True])
async def test_standalone_keeps_existing_deactivation_behavior(people, monkeypatch, scim_on):
    monkeypatch.setattr(settings, "cell", "")
    if not scim_on:
        monkeypatch.setattr(settings, "scim_token", "")
    people.provision("disabled@example.test", active=False)
    assert not await scim.refuses_subject("missing@example.test", org="acme")
    assert await scim.refuses_subject("disabled@example.test", org="acme")


async def test_account_row_overflow_never_checks_a_prefix(people):
    people.accounts.extend(("acme", "alice", "alice@example.test", True) for _ in range(1025))
    people.provision("alice@example.test")
    assert await scim.refuses_subject("alice", org="acme")


async def test_one_oversized_stored_contact_refuses(people):
    people.accounts.append(("acme", "alice", "x" * 65537 + "@example.test", True))
    assert await scim.refuses_subject("alice", org="acme")


async def test_disjoint_graphs_share_the_subject_budget(people):
    roots = []
    for graph in range(65):
        email = f"p{graph}@example.test"
        people.provision(email)
        names = [f"sa:personal:{graph}-{node}" for node in range(16)]
        roots.append(names[0])
        for current, parent in zip(names, [*names[1:], email], strict=True):
            people.services[current] = {"created_by": parent, "lineage": []}
    assert await scim.refuses_subject(roots[0], org="acme", also=tuple(roots[1:]))


async def test_repeated_edges_count_as_work_even_when_the_person_is_shared(people):
    people.provision("alice@example.test")
    people.services["sa:personal:one"] = {
        "created_by": "alice@example.test",
        "lineage": ["alice@example.test"] * 1024,
    }
    assert await scim.refuses_subject("sa:personal:one", org="acme")


async def test_security_does_not_reuse_grant_inheritance_metadata(people, monkeypatch):
    import time

    from server.auth import service_accounts

    people.provision("alice@example.test")
    people.services["sa:personal:one"] = {"created_by": "missing@example.test", "lineage": []}
    monkeypatch.setattr(
        service_accounts,
        "_creators",
        {
            ("acme", "sa:personal:one"): (
                time.monotonic(),
                service_accounts.SACreator("alice@example.test", True),
            )
        },
    )
    assert await scim.refuses_subject("sa:personal:one", org="acme")


@pytest.mark.parametrize("delta,refused", [(-1, True), (0, True), (1, False)])
async def test_fresh_service_account_expiry_uses_the_clock(people, monkeypatch, delta, refused):
    from types import SimpleNamespace

    from server.auth import scim_people

    people.provision("alice@example.test")
    people.services["sa:personal:one"] = {
        "created_by": "alice@example.test",
        "lineage": [],
        "expires_at": datetime.fromtimestamp(1000 + delta, UTC),
    }
    monkeypatch.setattr(
        scim_people, "time", SimpleNamespace(monotonic=lambda: 0.0, time=lambda: 1000.0)
    )
    assert await scim.refuses_subject("sa:personal:one", org="acme") is refused


@pytest.mark.parametrize("configured", [0, -1, float("inf"), float("nan"), 60])
async def test_one_finite_deadline_covers_every_root(people, monkeypatch, configured):
    from server.auth import scim_people
    from tests.unit.clock_fake import ManualClock

    clock = ManualClock()
    monkeypatch.setattr(settings, "ch_timeout_read_s", configured)
    monkeypatch.setattr(scim_people, "_now", clock.monotonic)
    people.on_read = lambda: clock.advance(10)
    people.provision("alice@example.test")
    people.provision("bob@example.test")
    with pytest.raises(login.SignInUnavailable):
        await scim.refuses_subject("alice@example.test", org="acme", also=("bob@example.test",))
    assert len(people.reads) == 3
    assert clock.t == 30


async def test_a_smaller_positive_read_budget_wins(people, monkeypatch):
    from server.auth import scim_people
    from tests.unit.clock_fake import ManualClock

    clock = ManualClock()
    monkeypatch.setattr(settings, "ch_timeout_read_s", 0.25)
    monkeypatch.setattr(scim_people, "_now", clock.monotonic)
    people.on_read = lambda: clock.advance(0.25)
    people.provision("alice@example.test")
    with pytest.raises(login.SignInUnavailable):
        await scim.refuses_subject("alice@example.test", org="acme")
    assert len(people.reads) == 1


async def test_identity_invalidation_clears_a_complete_graph_answer(people):
    people.provision("alice@example.test")
    assert not await scim.refuses_subject("alice@example.test", org="acme")
    people.provision("alice@example.test", active=False)
    authz.forget_ended("identity", "alice")
    assert await scim.refuses_subject("alice@example.test", org="acme")


async def test_local_invalidation_prevents_overtaken_memo_population(people):
    people.provision("alice@example.test")
    people.on_read = lambda: authz.forget_ended("identity", "alice")
    assert not await scim.refuses_subject("alice@example.test", org="acme")
    people.on_read = None
    people.provision("alice@example.test", active=False)
    assert await scim.refuses_subject("alice@example.test", org="acme")


async def test_standalone_read_failure_retains_the_sign_in_exception(people, monkeypatch):
    monkeypatch.setattr(settings, "cell", "")
    people.unavailable = True
    with pytest.raises(login.SignInUnavailable):
        await scim.refuses_subject("alice@example.test", org="acme")
