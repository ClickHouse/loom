"""The operator's bot actions: a bot is one org's, on namespaces that exist,
and replacing or disabling it ends the tokens it holds."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.auth import machines, revocation
from server.identity.app import account_admin
from server.identity.store import namespaces

_KEY = machines.generate_key()[1].decode()
_real_wait = revocation.wait_out


@pytest.mark.parametrize(
    ("org", "roles"),
    [
        ("*", ["shared=writer"]),
        ("", ["shared=writer"]),
        ("acme", ["*=writer"]),
        ("acme", ["a b=viewer"]),
    ],
)
def test_a_bot_is_one_orgs_on_named_namespaces(org: str, roles: list[str]) -> None:
    with pytest.raises(ValueError):
        account_admin.bot_from(name="b", org=org, namespace_roles=roles, public_key_pem=_KEY)


@pytest.mark.parametrize(
    ("name", "ok"), [("ci-a1b2c3", False), ("ci-a1b2c", True), ("triage", True)]
)
def test_a_bot_never_takes_a_service_accounts_subject(name: str, ok: bool) -> None:
    # A person's are `sa:<name>-<6 hex>`.
    def make() -> None:
        account_admin.bot_from(
            name=name, org="acme", namespace_roles=["a=writer"], public_key_pem=_KEY
        )

    if ok:
        make()
    else:
        with pytest.raises(ValueError, match="six hex"):
            make()


@pytest.mark.parametrize("ns", ["acme.prod", "Team_A", "u-lee-1a2b3c4d"])
def test_any_registered_namespace_name_will_do(ns: str) -> None:
    bot = account_admin.bot_from(
        name="b", org="acme", namespace_roles=[f"{ns}=writer"], public_key_pem=_KEY
    )
    assert bot["ns_roles"] == [(ns, "writer")]


def test_a_trust_issuer_is_kept_as_given_slash_and_all() -> None:
    # Some issuers' `iss` ends in a slash, and a token's `iss` is compared exactly.
    bot = account_admin.bot_from(
        name="deploy",
        org="acme",
        namespace_roles=["deploys=writer"],
        issuer="https://sts.example/tenant/",
        audience="loom",
        claims=["repository=acme/app"],
    )
    assert bot["issuer"] == "https://sts.example/tenant/"


@pytest.mark.parametrize(
    ("given", "ok"), [(None, True), (60, True), (86400, True), (59, False), (86401, False)]
)
def test_a_bots_longest_token_is_a_minute_to_a_day(given: int | None, ok: bool) -> None:
    def make() -> machines.Machine:
        lifetime = {} if given is None else {"max_access_token_s": given}
        return account_admin.bot_from(
            name="b", org="acme", namespace_roles=["a=writer"], public_key_pem=_KEY, **lifetime
        )

    if ok:
        assert make()["max_access_token_s"] == (3600 if given is None else given)
    else:
        with pytest.raises(ValueError, match="max_access_token_s is 60 to 86400 seconds"):
            make()


_real_ready = machines.access_token_maximum_ready


@pytest.fixture
def world(monkeypatch) -> dict[str, Any]:
    state: dict[str, Any] = {"bots": {}, "revoked": [], "outright": set(), "namespaces": {"triage"}}
    state |= {"audited": [], "fleet_ready": True}

    async def get(name: str, *, active_only: bool = True) -> machines.Machine | None:
        bot = state["bots"].get(name)
        return bot if bot and (bot["active"] or not active_only) else None

    async def save(m: machines.Machine, *, by: str, active: bool = True) -> None:
        state["bots"][m["name"]] = {**m, "active": active}

    async def is_active(org: str, namespace: str, **kw: Any) -> bool:
        return namespace in state["namespaces"]

    async def revoked_outright(jti: str) -> bool:
        return jti in state["outright"]

    async def cut_off(subject: str, **kw: Any) -> None:
        if state.get("revoke_fails"):
            raise RuntimeError("ClickHouse away")
        state["revoked"].append((subject, kw["reason"]))
        state["cutoff"] = kw["issued_before"]

    async def no_wait(cutoff: Any) -> None:
        return None

    async def emit(event_type: str, **kw: Any) -> str:
        state["audited"].append((event_type, kw["subject"]))
        return ""

    async def fleet_ready(ch: Any) -> bool:
        return state["fleet_ready"]

    monkeypatch.setattr(machines, "get", get)
    monkeypatch.setattr(machines, "access_token_maximum_ready", fleet_ready)
    monkeypatch.setattr(machines, "save", save)
    monkeypatch.setattr(namespaces, "is_active", is_active)
    monkeypatch.setattr(revocation, "revoked_outright", revoked_outright)
    monkeypatch.setattr(revocation, "cut_off", cut_off)
    monkeypatch.setattr(account_admin.audit, "emit", emit)
    monkeypatch.setattr(revocation, "wait_out", no_wait)
    return state


def _bot(roles: list[str]) -> machines.Machine:
    return account_admin.bot_from(
        name="triage", org="acme", namespace_roles=roles, public_key_pem=_KEY
    )


def test_a_bot_on_a_namespace_that_does_not_exist_is_refused(world) -> None:
    with pytest.raises(ValueError, match="no namespace ghost"):
        asyncio.run(account_admin.create_bot(_bot(["ghost=writer"]), by="ops", replace=False))


def _bot_allowed(seconds: int) -> machines.Machine:
    return account_admin.bot_from(
        name="triage",
        org="acme",
        namespace_roles=["triage=writer"],
        public_key_pem=_KEY,
        max_access_token_s=seconds,
    )


def test_a_maximum_other_than_an_hour_waits_for_the_whole_fleet(world) -> None:
    world["fleet_ready"] = False
    with pytest.raises(ValueError, match=f"all run migration {machines.ACCESS_TOKEN_MIGRATION} "):
        asyncio.run(account_admin.create_bot(_bot_allowed(600), by="ops", replace=False))
    assert world["bots"] == {}
    asyncio.run(account_admin.create_bot(_bot_allowed(3600), by="ops", replace=False))
    world["fleet_ready"] = True
    asyncio.run(account_admin.create_bot(_bot_allowed(600), by="ops", replace=True))
    assert world["bots"]["triage"]["max_access_token_s"] == 600
    created = [s for e, s in world["audited"] if e == "auth.machine_created"]
    assert [s["max_access_token_s"] for s in created] == [3600, 600]


def test_an_unread_fleet_refuses_a_maximum_other_than_an_hour(world, monkeypatch) -> None:
    from server.core import instances

    async def unread(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        raise ConnectionError("offline")

    monkeypatch.setattr(machines, "access_token_maximum_ready", _real_ready)
    monkeypatch.setattr(instances, "read_live", unread)
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    with pytest.raises(ValueError, match=f"all run migration {machines.ACCESS_TOKEN_MIGRATION} "):
        asyncio.run(account_admin.create_bot(_bot_allowed(600), by="ops", replace=False))
    assert world["bots"] == {}


def test_a_subject_revoked_outright_makes_no_bot(world) -> None:
    world["outright"].add("sa:triage")
    with pytest.raises(ValueError, match="revoked outright"):
        asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))


def test_replacing_a_bot_ends_the_tokens_it_had(world) -> None:
    asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    assert world["revoked"] == []
    with pytest.raises(ValueError, match="exists"):
        asyncio.run(account_admin.create_bot(_bot(["triage=viewer"]), by="ops", replace=False))
    asyncio.run(account_admin.create_bot(_bot(["triage=viewer"]), by="ops", replace=True))
    assert world["revoked"] == [("sa:triage", "bot replaced")]
    assert asyncio.run(account_admin.disable_bot("triage", by="ops"))
    assert world["revoked"][-1] == ("sa:triage", "bot disabled")


def test_a_token_minted_from_the_old_row_during_a_replace_is_revoked(world, monkeypatch) -> None:
    # A request reads the bot while its replacement is being saved: its token
    # is issued as of that read, and the replace's cutoff, taken after the
    # save, must be later.
    asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    save, read_during_save = machines.save, []

    async def slow_save(m: machines.Machine, *, by: str, active: bool = True) -> None:
        read_during_save.append(int(time.time()))  # `since` of a request now
        await save(m, by=by, active=active)

    monkeypatch.setattr(machines, "save", slow_save)
    monkeypatch.setattr(revocation, "wait_out", _real_wait)  # the real wait
    monkeypatch.setattr(revocation, "CLOCK_SKEW_S", 0)  # and a short one
    asyncio.run(account_admin.create_bot(_bot(["triage=viewer"]), by="ops", replace=True))
    assert read_during_save[0] < world["cutoff"].timestamp()


def test_a_saved_bot_stands_when_its_audit_row_fails(world, monkeypatch) -> None:
    async def down(event_type: str, **kw: Any) -> str:
        raise RuntimeError("ClickHouse away")

    monkeypatch.setattr(account_admin.audit, "emit", down)
    asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    assert "triage" in world["bots"]


def test_a_cutoff_covers_a_replica_whose_clock_runs_ahead(world) -> None:
    asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    asyncio.run(account_admin.disable_bot("triage", by="ops"))
    ahead = time.time() + revocation.CLOCK_SKEW_S  # `since` on that replica
    assert world["cutoff"].timestamp() > ahead


def test_the_wait_covers_a_replica_whose_clock_runs_behind(monkeypatch) -> None:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(revocation, "asyncio", SimpleNamespace(sleep=sleep))
    cutoff = datetime.fromtimestamp(time.time() + 6, UTC)
    asyncio.run(_real_wait(cutoff))
    # Past the cutoff on a clock that many seconds behind this one too.
    assert slept[0] > 5 + revocation.CLOCK_SKEW_S


def test_a_disable_waits_out_its_cutoff(world, monkeypatch) -> None:
    waited: list[Any] = []

    async def wait(cutoff: Any) -> None:
        waited.append(cutoff)

    asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    monkeypatch.setattr(revocation, "wait_out", wait)
    asyncio.run(account_admin.disable_bot("triage", by="ops"))
    assert waited == [world["cutoff"]]


def test_adopting_ends_tokens_from_any_replica_but_not_the_next_sign_in(world, monkeypatch) -> None:
    async def link(email: str, accounts: list[tuple[str, str]], by: str) -> list[tuple[str, str]]:
        return accounts

    monkeypatch.setattr(account_admin.sso_links, "link_live", link)
    monkeypatch.setattr(account_admin.settings, "oauth_login_provider", "oidc")
    waited: list[Any] = []

    async def wait(cutoff: Any) -> None:
        waited.append(cutoff)

    monkeypatch.setattr(revocation, "wait_out", wait)
    plan = [{"email": "lee@acme.io", "org": "acme", "namespace": "lee", "owner": "lee", "skip": ""}]
    asyncio.run(account_admin.adopt(plan, by="ops", keep_tokens=False))
    # Past every token issued so far, a replica's clock ahead included, as a
    # bot's cutoff is; and waited out, so a sign-in once adopt returns is past it.
    assert world["cutoff"].timestamp() >= time.time() + revocation.CLOCK_SKEW_S
    assert waited == [world["cutoff"]]


def test_a_disable_that_stopped_part_way_is_finished_by_another(world) -> None:
    asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    world["revoke_fails"] = True
    with pytest.raises(RuntimeError):
        asyncio.run(account_admin.disable_bot("triage", by="ops"))
    assert world["revoked"] == []  # saved inactive, its tokens alive
    world["revoke_fails"] = False
    assert asyncio.run(account_admin.disable_bot("triage", by="ops"))
    assert world["revoked"] == [("sa:triage", "bot disabled")]


def test_adopting_waits_for_sso_to_be_on(world, monkeypatch) -> None:
    # Before, the password still signs in and mints a token past the cutoff.
    monkeypatch.setattr(account_admin.settings, "oauth_login_provider", "password")
    plan = [{"email": "lee@acme.io", "org": "acme", "namespace": "lee", "owner": "lee", "skip": ""}]
    with pytest.raises(ValueError, match="SSO on first"):
        asyncio.run(account_admin.adopt(plan, by="ops", keep_tokens=False))
    assert "cutoff" not in world


def test_an_unlink_in_one_org_leaves_the_others(world, monkeypatch) -> None:
    unlinked: list[tuple[str, str]] = []

    async def linked(email: str) -> list[tuple[str, str]]:
        return [("acme", "lee"), ("beta", "lee")]

    async def unlink(email: str, accounts: list[tuple[str, str]], by: str) -> None:
        unlinked.extend(accounts)

    monkeypatch.setattr(account_admin.sso_links, "linked", linked)
    monkeypatch.setattr(account_admin.sso_links, "unlink", unlink)
    assert asyncio.run(account_admin.unlink("lee@acme.io", "beta", by="ops")) == [("beta", "lee")]
    assert unlinked == [("beta", "lee")]


def test_two_creates_at_once_make_one_bot(world, monkeypatch) -> None:
    free = machines.get

    async def slow_get(name: str, **kw: Any) -> Any:
        found = await free(name, **kw)
        await asyncio.sleep(0.01)  # the other create runs here, unless it waits its turn
        return found

    monkeypatch.setattr(machines, "get", slow_get)

    async def both() -> list[Any]:
        make = [
            account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False)
            for _ in range(2)
        ]
        return await asyncio.gather(*make, return_exceptions=True)

    outcomes = asyncio.run(both())
    assert sorted(type(o).__name__ for o in outcomes) == ["NoneType", "ValueError"]


def test_an_agent_trusts_shared_namespaces_must_fit_every_agents_token() -> None:
    trust = {"issuer": "https://ci.example", "audience": "loom", "claims": ["tenant_id=t1"]}
    roles = [f"t{i:02}" + "x" * 125 + "=viewer" for i in range(15)]
    with pytest.raises(ValueError, match=r"4218 bytes .* over 4096"):
        account_admin.bot_from(
            name="fleet", org="acme", namespace_roles=roles, agent_claim="sub", **trust
        )
    fleet = account_admin.bot_from(
        name="fleet", org="acme", namespace_roles=roles[:12], agent_claim="sub", **trust
    )
    assert len(fleet["ns_roles"]) == 12


def test_an_agent_trust_needs_a_workload_trust_and_may_share_nothing() -> None:
    trust = {"issuer": "https://ci.example", "audience": "loom", "claims": ["tenant_id=t1"]}
    fleet = account_admin.bot_from(
        name="fleet", org="acme", namespace_roles=[], agent_claim="sub", **trust
    )
    assert fleet["agent_claim"] == "sub" and fleet["ns_roles"] == []
    for wrong in (
        {"public_key_pem": _KEY, "agent_claim": "sub"},  # a key is one bot
        {"public_key_pem": _KEY},  # a plain bot still names a namespace
        {"jwks_uri": "http://keys.example/jwks.json", "agent_claim": "sub", **trust},
    ):
        with pytest.raises(ValueError):
            account_admin.bot_from(name="fleet", org="acme", namespace_roles=[], **wrong)


def test_a_replace_running_meanwhile_cannot_undo_a_disable(world, monkeypatch) -> None:
    asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    free_get, free_save = machines.get, machines.save
    steps: list[str] = []

    async def slow_get(name: str, **kw: Any) -> Any:
        steps.append("read")
        found = await free_get(name, **kw)
        await asyncio.sleep(0.01)  # the other change runs here, unless it waits its turn
        return found

    async def save(m: Any, **kw: Any) -> None:
        steps.append("write")
        await free_save(m, **kw)

    monkeypatch.setattr(machines, "get", slow_get)
    monkeypatch.setattr(machines, "save", save)

    async def both() -> None:
        replace = account_admin.create_bot(_bot(["triage=viewer"]), by="ops", replace=True)
        await asyncio.gather(replace, account_admin.disable_bot("triage", by="ops"))

    asyncio.run(both())
    assert steps == ["read", "write", "read", "write"]  # one after the other, never interleaved


def test_agent_trusts_whose_namespaces_could_meet_are_refused(world, monkeypatch) -> None:
    trust = {"issuer": "https://ci.example", "audience": "loom", "claims": ["tenant_id=t1"]}

    def fleet(name: str) -> machines.Machine:
        return account_admin.bot_from(
            name=name, org="acme", namespace_roles=[], agent_claim="agent_id", **trust
        )

    async def listed() -> list[dict[str, Any]]:
        return [
            {
                "name": "fleet",
                "agent_claim": "agent_id",
                "active": 1,
                "issuer": "https://other.example",
                "audience": "loom",
                "claims": {"tenant_id": "x"},
            }
        ]

    monkeypatch.setattr(machines, "listed", listed)
    for name in ("fleet-a", "fle"):  # fleet-a + "b" == fleet + "a-b"; "fle" is safe
        make = account_admin.create_bot(fleet(name), by="ops", replace=False)
        if name == "fle":
            asyncio.run(make)
        else:
            with pytest.raises(ValueError, match="trust 'fleet'"):
                asyncio.run(make)
    with pytest.raises(ValueError, match="SSO"):
        fleet("u")  # its agents' namespaces would be u-<agent>


@pytest.mark.parametrize(
    "claims",
    [["aud=loom"], ["iss=https://ci.example", "aud=loom"], ["iat=1*"], ["sub=tenants/*"]],
)
def test_a_trust_that_tells_no_workload_apart_is_refused(claims: list[str]) -> None:
    # It would take every token the issuer signs for Loom: every tenant, every job.
    with pytest.raises(ValueError, match="exact value"):
        account_admin.bot_from(
            name="fleet",
            org="acme",
            namespace_roles=["a=writer"],
            issuer="https://ci.example",
            audience="loom",
            claims=claims,
        )


@pytest.mark.parametrize("claims", [["sub=repo:acme/app:ref:refs/heads/main"], ["tenant_id=t1"]])
def test_one_exact_claim_that_names_a_workload_will_do(claims: list[str]) -> None:
    account_admin.bot_from(
        name="deploy",
        org="acme",
        namespace_roles=["a=writer"],
        issuer="https://ci.example",
        audience="loom",
        claims=claims,
    )


def test_an_agent_trust_no_token_could_tell_from_another_is_refused(world, monkeypatch) -> None:
    trust = {"issuer": "https://ci.example", "audience": "loom"}
    rows = [
        {
            "name": "tenant-a",
            "active": 1,
            "agent_claim": "agent_id",
            "claims": {"tenant_id": "a"},
            **trust,
        }
    ]

    async def listed() -> list[dict[str, Any]]:
        return rows

    monkeypatch.setattr(machines, "listed", listed)

    def create(name: str, claims: list[str]) -> None:
        bot = account_admin.bot_from(
            name=name,
            org="acme",
            namespace_roles=[],
            agent_claim="agent_id",
            claims=claims,
            **trust,
        )
        asyncio.run(account_admin.create_bot(bot, by="ops", replace=False))

    create("tenant-b", ["tenant_id=b"])  # another tenant: no token meets both
    with pytest.raises(ValueError, match="could also meet 'tenant-a'"):
        create("team-x", ["repository=x"])  # tenant-a's tokens would meet it too


@pytest.mark.parametrize("replace", [False, True])
def test_a_bot_holds_its_namespaces_from_their_check_to_its_save(
    world, monkeypatch, replace: bool
) -> None:
    # As a release holds one: a release cannot finish between the two and
    # leave the bot naming the namespace it dropped from every bot.
    from contextlib import asynccontextmanager

    world["namespaces"] |= {"docs"}
    if replace:
        asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    held: list[str] = []
    seen: list[list[str]] = []

    @asynccontextmanager
    async def account(org: str, ns: str) -> Any:
        held.append(ns)
        yield
        held.remove(ns)

    is_active, save = namespaces.is_active, machines.save

    async def checked(org: str, namespace: str, **kw: Any) -> bool:
        seen.append(sorted(held))
        return await is_active(org, namespace, **kw)

    async def saved(m: machines.Machine, *, by: str, active: bool = True) -> None:
        seen.append(sorted(held))
        await save(m, by=by, active=active)

    monkeypatch.setattr(account_admin, "account_change", account)
    monkeypatch.setattr(namespaces, "is_active", checked)
    monkeypatch.setattr(machines, "save", saved)
    bot = _bot(["triage=writer", "docs=viewer"])
    asyncio.run(account_admin.create_bot(bot, by="ops", replace=replace))
    assert seen == [["docs", "triage"]] * 3 and held == []


def test_an_operator_can_leave_a_bots_account_out_of_adoption(monkeypatch) -> None:
    # A bot's password account at the domain: adopting it would revoke the
    # service-account tokens it runs on, and no person signs in as it.
    async def unlinked(domain: str, org: str) -> list[dict[str, Any]]:
        return [
            {
                "contact_email": e,
                "org": "acme",
                "namespace": ns,
                "owner_email": ns,
                "created_at": "",
            }
            for e, ns in (("lee@acme.io", "lee"), ("CI-Bot@acme.io", "ci-bot"))
        ]

    async def on_sso(emails: list[str]) -> set[str]:
        return set()

    monkeypatch.setattr(account_admin.sso_links, "unlinked_in_domain", unlinked)
    monkeypatch.setattr(account_admin.sso_links, "reached_by_sso", on_sso)
    plan = asyncio.run(account_admin.adopt_plan("acme.io", skip=["ci-bot@acme.io"]))
    assert {a["namespace"]: a["skip"] for a in plan} == {
        "lee": "",
        "ci-bot": "left out by the operator",
    }
    with pytest.raises(ValueError, match="ci-bot@acme.co"):  # mistyped: the bot would be in
        asyncio.run(account_admin.adopt_plan("acme.io", skip=["ci-bot@acme.co"]))
    with pytest.raises(ValueError, match="needs an email"):
        asyncio.run(account_admin.adopt_plan("acme.io", skip=[" "]))


def test_the_cli_refuses_an_adoption_whose_skip_matches_nothing(monkeypatch, capsys) -> None:
    import argparse

    from server.cli import sso

    async def plan(domain: str, org: str = "", skip: Any = ()) -> list[dict[str, Any]]:
        raise ValueError("no unlinked account at acme.io has email typo@acme.io")

    async def adopt(*a: Any, **k: Any) -> int:
        raise AssertionError("nothing may be adopted")

    monkeypatch.setattr(account_admin, "adopt_plan", plan)
    monkeypatch.setattr(account_admin, "adopt", adopt)
    monkeypatch.setattr(sso, "run_connected", lambda run: asyncio.run(run(None)))
    args = argparse.Namespace(
        domain="acme.io", org="", skip=["typo@acme.io"], keep_tokens=False, dry_run=False
    )
    assert sso._cmd_adopt(args) == 2
    assert "typo@acme.io" in capsys.readouterr().err


def test_making_a_disabled_bot_again_ends_its_old_tokens(world, monkeypatch) -> None:
    # A disable that saved the row and stopped before its cutoff: the old
    # key's tokens must not outlive the bot made again under its name.
    asyncio.run(account_admin.create_bot(_bot(["triage=writer"]), by="ops", replace=False))
    world["bots"]["triage"]["active"] = False  # disabled, its cutoff never written
    world["revoked"].clear()
    asyncio.run(account_admin.create_bot(_bot(["triage=viewer"]), by="ops", replace=False))
    assert world["revoked"] == [("sa:triage", "bot replaced")]


@pytest.mark.parametrize(
    ("versions", "ready"),
    [
        ([], False),
        ([76], False),
        ([77, 76], False),
        ([None], False),
        ([77], True),
        ([77, 78], True),
    ],
)
async def test_the_maximum_waits_for_every_replica_at_migration_77(
    versions: list[int | None], ready: bool
) -> None:
    from tests.unit.test_platform_admin_rollout import Fleet

    fleet = Fleet([{"deployment": 0, "schema_version": v} for v in versions])
    assert await _real_ready(fleet) is ready


def test_the_maximum_floor_matches_the_physical_migration() -> None:
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2] / "schemas/migrations/077_machine_access_token_ttl.sql"
    )
    assert path.is_file()
    assert int(path.name.split("_", 1)[0]) == machines.ACCESS_TOKEN_MIGRATION


@pytest.mark.parametrize(
    ("linked", "advice"),
    [
        # A linked old address's grants admit the person: carry them to the subject.
        ([("acme", "lee")], "grant them to lee and revoke them first"),
        # An unlinked password account's address was never verified: they admit no one.
        ([], "they admit no one"),
    ],
)
def test_a_link_refuses_an_old_address_with_live_grants(
    monkeypatch: pytest.MonkeyPatch, linked: list[tuple[str, str]], advice: str
) -> None:
    import server.core.ch as ch_mod

    row = {"active": True, "password_hash": "h", "contact_email": "lee@home.io"}
    row |= {"owner_email": "lee", "org": "acme", "namespace": "lee"}
    grant = {"namespace": "team", "role": "writer", "status": "active", "revoked_at": None}

    class _CH:
        async def query_one(self, *_a: Any, **_k: Any) -> None:
            return None  # no other account names the new address

        async def query_all(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return [grant]

    async def _row(org: str, namespace: str) -> dict[str, Any]:
        return row

    async def _linked(email: str) -> list[tuple[str, str]]:
        return linked

    monkeypatch.setattr(account_admin.settings, "oauth_login_provider", "oidc")
    monkeypatch.setattr(namespaces, "fetch_row", _row)
    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(account_admin.sso_links, "linked", _linked)
    with pytest.raises(ValueError, match=advice):
        asyncio.run(account_admin.link_plan("lee@corp.io", "acme", "lee"))


@pytest.mark.parametrize("status", ["active", "pending", "revoked", "none"])
def test_a_link_leaves_an_old_address_another_account_holds_verified(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    # The old address is an SSO account's verified email too: its grants and its
    # requests are that account's, and linking this one away from the address
    # leaves them. The link says so whatever rows the address holds, none too.
    import server.core.ch as ch_mod

    row = {"active": True, "password_hash": "h", "contact_email": "lee@home.io"}
    row |= {"owner_email": "lee", "org": "acme", "namespace": "lee"}
    revoked_at = datetime(2026, 1, 1) if status == "revoked" else None
    grant = {"namespace": "team", "role": "writer", "status": "active", "revoked_at": revoked_at}
    if status == "pending":
        grant["status"] = "pending"
    rows = [] if status == "none" else [grant]

    class _CH:
        async def query_one(self, *_a: Any, **_k: Any) -> None:
            return None  # no other account names the new address

        async def query_all(self, sql: str, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return [] if "DISTINCT namespace" in sql else rows  # lee has no row of its own

    async def _row(org: str, namespace: str) -> dict[str, Any]:
        return row

    async def _linked(email: str) -> list[tuple[str, str]]:
        return []

    async def _held(org: str, namespace: str, email: str) -> bool:
        return email == "lee@home.io"  # an SSO account holds the old address

    monkeypatch.setattr(account_admin.settings, "oauth_login_provider", "oidc")
    monkeypatch.setattr(namespaces, "fetch_row", _row)
    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(account_admin.sso_links, "linked", _linked)
    monkeypatch.setattr(account_admin, "_verified_elsewhere", _held)
    plan = asyncio.run(account_admin.link_plan("lee@corp.io", "acme", "lee"))
    assert plan["was"] == "lee@home.io" and plan["elsewhere"] is True
    # The row capped lee's tokens at rank 0 while the tie held: it is carried to
    # lee, revoked, so a token minted under the tie stays capped.
    assert [g["namespace"] for g in plan["revoked"]] == ([] if status == "none" else ["team"])


_ELSEWHERE = "is another account's verified email: its grants stay theirs"


@pytest.mark.parametrize(
    ("dry_run", "elsewhere", "line"),
    [
        (True, True, f"lee@home.io {_ELSEWHERE}"),
        (True, False, "revoked under lee@home.io, so revoked for lee too: team"),
        # The applied link names the address it read, not the plan's.
        (False, True, f"lee@old.io {_ELSEWHERE}"),
        (False, False, "  revoked for lee too: team"),
    ],
)
def test_the_link_names_whose_grants_it_caps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
    elsewhere: bool,
    line: str,
) -> None:
    import argparse

    from server.cli import sso

    async def plan(email: str, org: str, namespace: str) -> dict[str, Any]:
        row = {"owner_email": "lee"}
        revoked = [{"namespace": "team"}]
        # The applied link reports what it read under the claims, not this plan.
        seen = elsewhere if dry_run else not elsewhere
        return {"row": row, "was": "lee@home.io", "revoked": revoked, "elsewhere": seen}

    async def link(*a: Any, **k: Any) -> tuple[str, list[str], bool]:
        assert not dry_run, "a dry run links nothing"
        return "lee@old.io", ["team"], elsewhere

    monkeypatch.setattr(account_admin, "link_plan", plan)
    monkeypatch.setattr(account_admin, "link_account", link)
    monkeypatch.setattr(sso, "run_connected", lambda run: asyncio.run(run(None)))
    args = argparse.Namespace(email="lee@corp.io", org="acme", namespace="lee", dry_run=dry_run)
    assert sso._cmd_link(args) == 0
    out = capsys.readouterr().out
    assert line in out and (_ELSEWHERE in out) is elsewhere
