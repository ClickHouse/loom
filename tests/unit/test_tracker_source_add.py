"""The tracker source write ops: a team registers its own tracker source
(`tracker.source.add`), checks it (`tracker.source.check`), rotates its credential
(`tracker.source.rotate`) and retires it (`tracker.source.remove`). Three mechanical
gates before anything is written — public host, read-only credential, the
record contract — the password encrypted at rest, the namespaces registered
with their join rule, the registrant made admin, and the registry merged
into the source map under the operator's JSON."""

from __future__ import annotations

import json
from typing import Any

import pytest

import server.core.hosts as hosts
import server.tracker.app.onboarding as ob
import server.tracker.app.registry as reg
import server.tracker.app.registry_sources as rs
import server.tracker.domain.sources as qs
import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo, Role
from server.core.errors import AuthInsufficientError, ValidationError
from server.identity.app import namespace_classification as nc
from server.settings import settings
from server.tracker.api.models import (
    TrackerSourceAddInput,
    TrackerSourceCheckInput,
    TrackerSourceListInput,
    TrackerSourceNamespaceSpec,
    TrackerSourceRemoveInput,
    TrackerSourceRotateInput,
)
from server.tracker.api.ops import tracker_source_add, tracker_source_remove
from server.tracker.app.sources import (
    handle_tracker_source_add,
    handle_tracker_source_check,
    handle_tracker_source_list,
    handle_tracker_source_remove,
    handle_tracker_source_rotate,
)
from server.tracker.domain import contract as rc
from server.tracker.store.source import contract_reports as reports
from tests.unit._hosts import resolves
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

REAL_HOST_REFUSAL = ob.host_refusal  # captured before the fixture replaces it
REAL_RESOLVE = hosts._resolve

ORG = "clickhouse"
PUB, PRIV = "tracker-clickhouse", "tracker-example-private"
REPO, PREPO = "ClickHouse/ClickHouse", "ClickHouse/example-private"

RO_GRANTS = [
    "GRANT CREATE TEMPORARY TABLE ON *.* TO loom_ro",
    "GRANT SHOW TABLES, SHOW COLUMNS, SELECT ON loom_qa.* TO loom_ro",
    "GRANT SELECT ON system.columns TO loom_ro",
    "GRANT analyst TO loom_ro",
]


def _full_schema(**overrides: dict[str, str]) -> rc.Described:
    live = {
        "String": "LowCardinality(String)",
        "UInt": "UInt8",
        "DateTime": "DateTime64(3)",
        "UUID": "UUID",
        "Array(String)": "Array(String)",
        "Array(Float)": "Array(Float32)",
    }
    out: rc.Described = {}
    for table, cols in {**rc.REQUIRED, **rc.OPTIONAL}.items():
        out[table] = {c: live[t] for c, t in cols.items()}
    for tables in rc.FEATURES.values():
        for table, cols in tables.items():
            out[table].update({c: live[t] for c, t in cols.items()})
    for table, cols in overrides.items():
        out[table] = cols
    return out


class _Ch:
    """loom's own ClickHouse: the directory, the grants and loom.qa_sources."""

    def __init__(self) -> None:
        self.inserted: dict[str, list[dict[str, Any]]] = {}
        self.namespace_rows: list[dict[str, Any]] = []
        self.grant_rows: list[dict[str, Any]] = []
        self.active_sources = 0

    def _sources(self) -> list[dict[str, Any]]:
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for r in self.inserted.get("loom.qa_sources", []):
            latest[(r["org"], r["name"])] = r  # a later row supersedes (RMT)
        return [r for r in latest.values() if r["active"]]

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        p = dict(params or {})
        if "FROM loom.qa_sources" in sql:
            return self._sources()
        if "classification = 'restricted'" in sql:  # the directory's restricted set
            return [r for r in self.namespace_rows if r.get("classification") == "restricted"]
        if "FROM loom.namespaces" in sql:
            return [r for r in self.namespace_rows if r["namespace"] in p.get("names", [])]
        if "FROM loom.namespace_grants" in sql:
            return [g for g in self.grant_rows if g["namespace"] in p.get("names", [])]
        raise AssertionError(sql)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict | None:
        p = dict(params or {})
        if "AS in_use" in sql:
            return None  # the name holds no data with no registry row
        if "count() AS n FROM loom.qa_sources" in sql:
            return {"n": self.active_sources + len(self._sources())}
        if "SELECT role FROM loom.namespace_grants" in sql:  # the gate's grant read
            live = [
                g
                for g in self.grant_rows
                if g["namespace"] == p["ns"]
                and g["subject"].lower() in p["subs"]
                and g.get("revoked_at") is None
            ]
            return {"role": live[0]["role"]} if live else None
        if "FROM loom.namespaces" in sql:
            ns = p.get("ns")
            return next((r for r in self.namespace_rows if r["namespace"] == ns), None)
        raise AssertionError(sql)

    def grant_admin(self, subject: str, namespace: str) -> None:
        self.grant_rows.append(
            {"subject": subject, "namespace": namespace, "role": "admin", "revoked_at": None}
        )

    def revoke_all(self) -> None:
        for g in self.grant_rows:
            g["revoked_at"] = "2026-01-02 00:00:00"

    async def insert(self, table: str, rows: list[dict[str, Any]], *a: Any, **kw: Any) -> None:
        self.inserted.setdefault(table, []).extend(rows)
        if table == "loom.namespaces":
            self.namespace_rows.extend(rows)
        if table == "loom.namespace_grants":
            self.grant_rows.extend(rows)


def _auth(subject: str = "alice", role: Role = Role.ADMIN) -> AuthInfo:
    return AuthInfo(
        subject=subject, org=ORG, namespaces=(PUB,), roles=(role,), is_service_account=False
    )


def _probe(grants: list[str] | None = None, described: rc.Described | None = None):
    async def _fake(**kw: Any) -> ob.ProbeResult:
        return ob.ProbeResult(
            grants=ob.review_grants(grants or RO_GRANTS, [{"role_name": "analyst"}]),
            described=described if described is not None else _full_schema(),
        )

    return _fake


async def _public(host: str) -> str:
    return ""


def _add_input(**over: Any) -> TrackerSourceAddInput:
    base: dict[str, Any] = {
        "org": ORG,
        "name": "clickstack",
        "host": "https://cs.clickhouse.cloud/",
        "port": 8443,
        "secure": True,
        "database": "loom_qa",
        "user": "loom_ro",
        "password": "s3cret",
        "namespaces": [
            TrackerSourceNamespaceSpec(
                namespace="tracker-clickstack", repos=["ClickHouse/clickstack"]
            ),
            TrackerSourceNamespaceSpec(
                namespace="tracker-stack-private",
                repos=["ClickHouse/stack-private"],
                restricted=True,
            ),
        ],
    }
    base.update(over)
    return TrackerSourceAddInput(**base)


async def _no_tracker_service(credential: str) -> Any:
    """A read the test did not fake fails the way an unreachable tracker
    does, without resolving the configured host."""
    raise qi.QaIntelUnavailable(f"no tracker service in unit tests ({credential})")


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    reports.reset_for_tests()
    rs.reset_for_tests()
    reg.reset_for_tests()
    qs.reset_warnings_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG, raising=False)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json({REPO: PUB, PREPO: PRIV}, restricted=PRIV),
        raising=False,
    )
    monkeypatch.setattr(
        settings, "tracker_credentials_json", tracker_credentials_json(), raising=False
    )
    monkeypatch.setattr(settings, "tracker_self_service_enabled", True, raising=False)
    monkeypatch.setattr(settings, "tracker_self_service_max_sources", 10, raising=False)
    monkeypatch.setattr(settings, "tracker_sources_refresh_seconds", 60.0, raising=False)
    if not settings.jwt_secret:
        monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    monkeypatch.setattr(qi, "_build_client", _no_tracker_service)
    # A public address for any host the test names; a test about resolution
    # patches the lookups itself.
    resolves(monkeypatch, lambda host: ["93.184.215.14"])
    ch = _Ch()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    nc.reset()  # the directory's restricted set, read from this fake
    monkeypatch.setattr(ob, "host_refusal", _public)
    monkeypatch.setattr(ob, "probe_credential", _probe())
    token = current_auth_var.set(_auth())
    yield ch
    current_auth_var.reset(token)
    nc.reset()
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    reports.reset_for_tests()
    rs.reset_for_tests()
    reg.reset_for_tests()


# ---------------------------------------------------------------- the gates (pure)


def test_grant_review_names_what_it_refuses_and_the_roles() -> None:
    rev = ob.review_grants(RO_GRANTS, [{"role_name": "analyst"}])
    assert rev.read_only and rev.roles == ["analyst"] and rev.refused == []
    rev = ob.review_grants(["GRANT SELECT, INSERT ON loom_qa.* TO u", "GRANT ALL ON *.* TO u"], [])
    assert not rev.read_only and rev.refused == ["INSERT", "ALL"]
    rev = ob.review_grants(
        [
            "GRANT ALTER TABLE, DROP TABLE ON loom_qa.* TO u",
            "GRANT CREATE TEMPORARY TABLE ON *.* TO u",
        ],
        [],
    )
    assert rev.refused == ["ALTER TABLE", "DROP TABLE"]
    assert ob.review_grants(["GRANT CREATE TEMPORARY TABLE ON *.* TO u"], []).read_only


def test_grant_review_refuses_what_it_cannot_classify_as_read_only() -> None:
    refused = [
        "GRANT displaySecretsInShowAndSelect ON *.* TO u",
        "GRANT SELECT ON loom_qa.* TO u WITH GRANT OPTION",
        "GRANT analyst TO u WITH ADMIN OPTION",
        "GRANT SHOW NAMED COLLECTIONS ON *.* TO u",
        "GRANT SOME FUTURE TYPE ON *.* TO u",
        "GRANT CREATE ARBITRARY TEMPORARY TABLE ON *.* TO u",
        "GRANT SELECT(`a(`), INSERT ON db.t TO u",
        "GRANT SELECT(`x ON y`), INSERT ON db.t TO u",
        "GRANT SELECT(`a) INSERT`) ON db.t TO `u",
        "GRANT INSERT",
        "GRANT INSERT TO u ON *.*",
        "GRANT on TO u",
        "GRANT to TO u",
    ]
    for line in refused:
        assert not ob.review_grants([line], [{"role_name": "analyst"}]).read_only, line
    rev = ob.review_grants(["GRANT SELECT(id, title), SHOW TABLES ON loom_qa.t TO u"], [])
    assert rev.read_only and rev.refused == []
    assert ob.review_grants(
        ["GRANT analyst TO u", "REVOKE SELECT ON loom_qa.x FROM u"], [{"role_name": "analyst"}]
    ).read_only
    rev = ob.review_grants(
        ["GRANT SELECT(`a, b`, `c ON d`) ON db.t TO `loom-ro`", "GRANT `my, role` TO `loom-ro`"],
        [{"role_name": "my, role"}],
    )
    assert rev.read_only and rev.roles == ["my, role"]
    # a direct role's admin option shows in the line and the enabled roles: named once
    enabled = [{"role_name": "analyst", "with_admin_option": 1}]
    rev = ob.review_grants(["GRANT analyst TO u WITH ADMIN OPTION"], enabled)
    assert rev.roles == ["analyst"] and rev.refused == ["WITH ADMIN OPTION (analyst)"]
    # a line with two scopes is one the check does not parse
    assert not ob.review_grants(["GRANT SELECT ON db.t, INSERT ON db.x TO u"], []).read_only


def test_role_review_refuses_a_role_that_is_not_enabled_or_holds_an_admin_option() -> None:
    # SHOW GRANTS prints `a\\\`b` for a role named a\`b
    enabled = [{"role_name": "analyst"}, {"role_name": "my  role"}, {"role_name": r"a\`b"}]
    rev = ob.review_grants([r"GRANT analyst, `my  role`, `a\\\`b` TO u"], enabled)
    assert rev.read_only
    # a role that is not a default role is not enabled, so its chain is unseen
    enabled = [{"role_name": "analyst", "with_admin_option": 0}]
    rev = ob.review_grants(["GRANT analyst, other TO u"], enabled)
    assert rev.not_enabled == ["other"] and not rev.read_only
    assert rev.refusal("u").endswith("Run: SET DEFAULT ROLE ALL TO u")
    assert rev.refusal("loom-ro").endswith("Run: SET DEFAULT ROLE ALL TO `loom-ro`")
    # both causes: each gets its sentence
    rev = ob.review_grants(["GRANT INSERT ON db.* TO u", "GRANT other TO u"], [])
    assert rev.refusal("u").startswith("'u' holds more than read access (INSERT); ")
    assert "'u' holds roles that are not default roles (other)" in rev.refusal("u")
    # an admin option on a nested role shows only in the enabled roles
    enabled = [{"role_name": "analyst"}, {"role_name": "nested", "with_admin_option": 1}]
    rev = ob.review_grants(["GRANT analyst TO u"], enabled)
    assert rev.refused == ["WITH ADMIN OPTION (nested)"]


@pytest.mark.asyncio
async def test_host_refusal_rejects_private_and_local_addresses(monkeypatch) -> None:
    import socket

    table = {
        "cs.example": "3.16.142.177",
        "tail.example": "100.73.16.50",
        "ten.example": "10.1.2.3",
        "lo.example": "127.0.0.1",
    }

    def _gai(host, *a, **kw):
        if host not in table:
            raise socket.gaierror("no such host")
        return [(2, 1, 6, "", (table[host], 0))]

    async def _in_loop(host: str) -> list[str]:
        return [_gai(host)[0][4][0]]

    # the system resolver (an allowlisted host) over a fake getaddrinfo, the in-loop
    # one (any other) over the same table
    monkeypatch.setattr(socket, "getaddrinfo", _gai)
    monkeypatch.setattr(hosts, "_resolve", REAL_RESOLVE)
    monkeypatch.setattr(hosts, "_resolve_in_loop", _in_loop)
    real = REAL_HOST_REFUSAL
    assert await real("cs.example") == ""
    assert "private or local" in await real("tail.example")
    assert "private or local" in await real("ten.example")
    assert "private or local" in await real("lo.example")
    assert "does not resolve" in await real("nope.example")
    assert await real("") == "host is required"
    monkeypatch.setattr(settings, "tracker_self_service_allow_private_hosts", True, raising=False)
    assert await real("ten.example") == ""


def test_password_roundtrips_encrypted() -> None:
    enc = rs.encrypt_password("s3cret")
    assert enc and enc != "s3cret" and "s3cret" not in enc
    assert rs.decrypt_password(enc) == "s3cret" and rs.decrypt_password("") == ""


# ---------------------------------------------------------------- add


@pytest.mark.asyncio
async def test_add_registers_the_source_its_namespaces_and_the_registrant(_cfg) -> None:
    ch = _cfg
    out = await handle_tracker_source_add(_add_input())
    assert out.name == "clickstack" and out.host == "cs.clickhouse.cloud" and out.my_role == "admin"
    assert out.contract["ok"] is True and out.roles == ["analyst"]
    assert [(n["namespace"], n["classification"], n["join_policy"]) for n in out.namespaces] == [
        ("tracker-clickstack", "standard", "open"),
        ("tracker-stack-private", "restricted", "approval"),
    ]
    # the row: password encrypted, never plain; namespaces as JSON
    (row,) = ch.inserted["loom.qa_sources"]
    assert row["user"] == "loom_ro" and row["password_enc"] != "s3cret"
    assert "s3cret" not in json.dumps(row, default=str)
    assert rs.decrypt_password(row["password_enc"]) == "s3cret"
    assert json.loads(row["namespaces_json"])["tracker-stack-private"]["restricted"] is True
    assert row["created_by"] == "alice" and row["active"] is True
    # the source map now serves it, under the operator's JSON
    assert qs.endpoints()["clickstack"].host == "cs.clickhouse.cloud"
    assert qs.credentials_for("clickstack") == ("loom_ro", "s3cret")
    assert qs.namespace_entries()["tracker-stack-private"].restricted
    assert qs.source_origin("clickstack") == "registry" and qs.source_origin("qa") == "config"
    assert qs.credential_configured("clickstack")
    # the directory rows and the registrant's admin grants
    by_ns = {r["namespace"]: r for r in ch.inserted["loom.namespaces"]}
    assert by_ns["tracker-clickstack"]["join_policy"] == "open"
    assert by_ns["tracker-clickstack"]["classification"] == "standard"
    assert by_ns["tracker-stack-private"]["join_policy"] == "approval"
    assert by_ns["tracker-stack-private"]["classification"] == "restricted"
    grants = {
        (g["subject"], g["namespace"], g["role"]) for g in ch.inserted["loom.namespace_grants"]
    }
    assert grants == {
        ("alice", "tracker-clickstack", "admin"),
        ("alice", "tracker-stack-private", "admin"),
    }
    # the verdict is cached: the first read does not describe again
    assert reports.cached("clickstack") is not None and reports.cached("clickstack").ok
    # and the list shows it with the registrant as admin
    listed = await handle_tracker_source_list(TrackerSourceListInput(org=ORG))
    cs = next(s for s in listed.sources if s["name"] == "clickstack")
    assert cs["origin"] == "registry"
    assert cs["configured"] and {n["namespace"] for n in cs["namespaces"]} == {
        "tracker-clickstack",
        "tracker-stack-private",
    }
    assert "s3cret" not in json.dumps(listed.model_dump())


@pytest.mark.asyncio
async def test_a_namespace_taken_during_the_probe_writes_nothing(_cfg, monkeypatch) -> None:
    """The free-name check runs again under each namespace's account claim, after the
    remote probe: a sign-up or a register that took a name meanwhile wins."""
    from contextlib import asynccontextmanager

    import server.tracker.app.sources as sources_app

    ch = _cfg
    held: list[list[str]] = []
    asked: list[tuple[str, bool]] = []

    @asynccontextmanager
    async def _accounts_change(org: str, namespaces: list[str]) -> Any:
        held.append(sorted(namespaces))
        yield
        held.append([])

    async def _taken(org: str, namespace: str) -> bool:
        asked.append((namespace, bool(held and held[-1])))
        return len(asked) > 2  # free at admission (two namespaces), taken on the re-check

    monkeypatch.setattr(sources_app, "accounts_change", _accounts_change)
    monkeypatch.setattr(sources_app, "_namespace_taken", _taken)
    with pytest.raises(ValidationError) as e:
        await handle_tracker_source_add(_add_input())
    assert e.value.to_detail()["code"] == "NAMESPACE_TAKEN"
    assert held[0] == ["tracker-clickstack", "tracker-stack-private"]
    assert [under for _ns, under in asked] == [False, False, True]  # the re-check, held
    assert "loom.qa_sources" not in ch.inserted
    assert "loom.namespace_grants" not in ch.inserted


@pytest.mark.asyncio
async def test_a_busy_namespace_claim_writes_nothing(_cfg, monkeypatch) -> None:
    from contextlib import asynccontextmanager

    import server.tracker.app.sources as sources_app
    from server.core.errors import GrantBusyError

    @asynccontextmanager
    async def _busy(org: str, namespaces: list[str]) -> Any:
        raise GrantBusyError("another change to the account is running; retry shortly")
        yield

    monkeypatch.setattr(sources_app, "accounts_change", _busy)
    with pytest.raises(GrantBusyError):
        await handle_tracker_source_add(_add_input())
    assert "loom.qa_sources" not in _cfg.inserted
    assert "loom.namespace_grants" not in _cfg.inserted


@pytest.mark.asyncio
async def test_the_registrants_grants_are_written_before_the_source(_cfg, monkeypatch) -> None:
    """A replica that has not loaded the new source sees its namespaces in use."""
    import server.tracker.app.sources as sources_app

    order: list[str] = []
    real_grant, real_insert = sources_app._make_admin, rs.insert_source

    async def _grant(*a: Any) -> None:
        order.append("grant")
        await real_grant(*a)

    async def _insert(*a: Any, **k: Any) -> None:
        order.append("source")
        await real_insert(*a, **k)

    monkeypatch.setattr(sources_app, "_make_admin", _grant)
    monkeypatch.setattr(rs, "insert_source", _insert)
    await handle_tracker_source_add(_add_input())
    assert order == ["grant", "grant", "source"]


@pytest.mark.asyncio
async def test_add_lets_the_registrant_choose_approval_for_a_public_namespace(_cfg) -> None:
    out = await handle_tracker_source_add(
        _add_input(
            namespaces=[
                TrackerSourceNamespaceSpec(
                    namespace="tracker-clickstack",
                    repos=["ClickHouse/clickstack"],
                    join_policy="approval",
                )
            ]
        )
    )
    assert out.namespaces[0]["join_policy"] == "approval"
    assert _cfg.inserted["loom.namespaces"][0]["join_policy"] == "approval"


@pytest.mark.asyncio
async def test_add_refusals_name_what_to_fix(_cfg, monkeypatch) -> None:
    ch = _cfg

    async def refused(inp: TrackerSourceAddInput, code: str) -> None:
        with pytest.raises(ValidationError) as ei:
            await handle_tracker_source_add(inp)
        assert ei.value.to_detail()["code"] == code, (ei.value.code, str(ei.value))
        assert "loom.qa_sources" not in ch.inserted

    await refused(_add_input(name="tracker source"), "BAD_NAME")
    await refused(_add_input(name="qa"), "SOURCE_EXISTS")
    await refused(
        _add_input(namespaces=[TrackerSourceNamespaceSpec(namespace="clickstack", repos=["a/b"])]),
        "BAD_NAMESPACE",
    )
    await refused(
        _add_input(
            namespaces=[TrackerSourceNamespaceSpec(namespace="tracker-xy", repos=["not-a-repo"])]
        ),
        "BAD_REPO",
    )
    await refused(
        _add_input(namespaces=[TrackerSourceNamespaceSpec(namespace=PUB, repos=[REPO])]),
        "NAMESPACE_TAKEN",
    )
    ch.namespace_rows.append({"namespace": "tracker-taken", "active": True})
    await refused(
        _add_input(
            namespaces=[TrackerSourceNamespaceSpec(namespace="tracker-taken", repos=["a/b"])]
        ),
        "NAMESPACE_TAKEN",
    )
    ch.active_sources = 10
    await refused(_add_input(), "SOURCE_QUOTA")
    ch.active_sources = 0

    async def _private(host: str) -> str:
        return f"{host} resolves to 10.0.0.5, a private or local address"

    monkeypatch.setattr(ob, "host_refusal", _private)
    await refused(_add_input(), "HOST_REFUSED")
    monkeypatch.setattr(ob, "host_refusal", _public)

    async def _down(**kw: Any) -> ob.ProbeResult:
        raise ob.ProbeError("cs.clickhouse.cloud:8443/loom_qa did not accept 'loom_ro'")

    monkeypatch.setattr(ob, "probe_credential", _down)
    await refused(_add_input(), "SOURCE_UNREACHABLE")

    async def _rebound(**kw: Any) -> ob.ProbeResult:
        raise ob.HostRefused("cs.clickhouse.cloud resolves to 169.254.169.254")

    monkeypatch.setattr(ob, "probe_credential", _rebound)
    await refused(_add_input(), "HOST_REFUSED")
    monkeypatch.setattr(
        ob, "probe_credential", _probe(grants=["GRANT SELECT, INSERT ON loom_qa.* TO loom_ro"])
    )
    await refused(_add_input(), "CREDENTIAL_NOT_READONLY")
    described = _full_schema()
    del described["edges"]
    del described["github_issues"]["error_code"]
    monkeypatch.setattr(ob, "probe_credential", _probe(described=described))
    with pytest.raises(ValidationError, match="missing tables edges") as ei:
        await handle_tracker_source_add(_add_input())
    assert ei.value.to_detail()["code"] == "CONTRACT_FAILED" and "error_code" in str(ei.value)
    monkeypatch.setattr(ob, "probe_credential", _probe())
    monkeypatch.setattr(settings, "tracker_self_service_enabled", False, raising=False)
    await refused(_add_input(), "SELF_SERVICE_OFF")
    monkeypatch.setattr(settings, "tracker_self_service_enabled", True, raising=False)
    token = current_auth_var.set(
        AuthInfo(
            subject="bob",
            org="other",
            namespaces=("x",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    try:
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_add(_add_input())
    finally:
        current_auth_var.reset(token)
    assert tracker_source_add.required_role == Role.VIEWER and tracker_source_add.audit
    assert tracker_source_add.skip_namespace_authz and not tracker_source_add.idempotent


@pytest.mark.asyncio
async def test_a_configured_entry_wins_over_a_registered_one_by_name(_cfg, monkeypatch) -> None:
    rs.reset_for_tests(
        [
            qs.DbSource(
                org=ORG,
                name="qa",
                host="rogue.example",
                port=8443,
                secure=True,
                database="x",
                user="u",
                password="p",
            )
        ]
    )
    assert qs.endpoints()["qa"].host == "qa.example" and qs.source_origin("qa") == "config"
    assert qs.credentials_for("qa") == ("ro", "pw")
    monkeypatch.setattr(settings, "tracker_self_service_enabled", False, raising=False)
    assert qs.registered() == []  # self-service off: rows are ignored


# ---------------------------------------------------------------- check / rotate / remove


@pytest.mark.asyncio
async def test_check_runs_the_contract_now_and_says_unreachable(monkeypatch) -> None:
    described = _full_schema()

    async def _rows(sql, params=None, *, credential="qa", **kw):
        assert credential == "qa"
        return [
            {"table": t, "name": c, "type": ty}
            for t, cols in described.items()
            for c, ty in cols.items()
        ]

    monkeypatch.setattr(qi, "query_rows", _rows)
    out = await handle_tracker_source_check(TrackerSourceCheckInput(org=ORG, name="qa"))
    assert out.reachable and out.contract["ok"] is True and out.database == "qa_intelligence"

    async def _down(sql, params=None, *, credential="qa", **kw):
        raise RuntimeError("timeout")

    monkeypatch.setattr(qi, "query_rows", _down)
    reports.reset_for_tests()
    out = await handle_tracker_source_check(TrackerSourceCheckInput(org=ORG, name="qa"))
    assert not out.reachable and out.contract["ok"] is None
    with pytest.raises(ValidationError) as ei:
        await handle_tracker_source_check(TrackerSourceCheckInput(org=ORG, name="nope"))
    assert ei.value.to_detail()["code"] == "NOT_A_SOURCE"


@pytest.mark.asyncio
async def test_rotate_needs_a_namespace_admin_and_reconnects(_cfg, monkeypatch):
    ch = _cfg
    await handle_tracker_source_add(_add_input())
    qi._slot("clickstack").client = object()  # a live connection with the old password
    out = await handle_tracker_source_rotate(
        TrackerSourceRotateInput(org=ORG, name="clickstack", user="loom_ro2", password="n3w")
    )
    assert out.contract["ok"] is True and out.rotated_at
    assert qs.credentials_for("clickstack") == ("loom_ro2", "n3w")
    assert qi._slot("clickstack").client is None  # dropped: the next read reconnects
    rows = ch.inserted["loom.qa_sources"]
    assert len(rows) == 2 and rows[-1]["user"] == "loom_ro2" and rows[-1]["created_by"] == "alice"
    # bob: no admin grant on any of its namespaces → refused; on the standard one
    # only → refused (a change reaches the restricted one too); on both → allowed
    token = current_auth_var.set(_auth("bob"))
    try:
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_rotate(
                TrackerSourceRotateInput(org=ORG, name="clickstack", user="x", password="y")
            )
        ch.grant_admin("bob", "tracker-clickstack")
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_rotate(
                TrackerSourceRotateInput(org=ORG, name="clickstack", user="x", password="y")
            )
        ch.grant_admin("bob", "tracker-stack-private")
        out = await handle_tracker_source_rotate(
            TrackerSourceRotateInput(org=ORG, name="clickstack", user="x", password="y")
        )
        assert qs.credentials_for("clickstack") == ("x", "y")
    finally:
        current_auth_var.reset(token)
    # a configured source is never rotated from the page
    with pytest.raises(ValidationError) as ei:
        await handle_tracker_source_rotate(
            TrackerSourceRotateInput(org=ORG, name="qa", user="a", password="b")
        )
    assert ei.value.to_detail()["code"] == "SOURCE_FROM_CONFIG"


@pytest.mark.asyncio
async def test_a_registrant_whose_admin_grants_were_revoked_holds_the_source_no_more(
    _cfg, monkeypatch
) -> None:
    """Registering made alice admin of the source's namespaces; those grants, read
    through the gate, not her name on the row, are her authority. She rotates the
    source; revoked, she can neither rotate, remap nor retire it, and retiring would
    have deleted its namespaces past their gate."""
    from server.tracker.api.models import TrackerSourceSetMappingInput
    from server.tracker.app.sources import handle_tracker_source_set_mapping

    ch = _cfg
    await handle_tracker_source_add(_add_input())
    assert ch.inserted["loom.qa_sources"][-1]["created_by"] == "alice"
    await handle_tracker_source_rotate(
        TrackerSourceRotateInput(org=ORG, name="clickstack", user="x", password="y")
    )
    assert qs.credentials_for("clickstack") == ("x", "y")
    ch.revoke_all()
    deleted: list[str] = []

    async def _delete(inp: Any) -> Any:
        deleted.append(inp.namespace)
        return type("O", (), {"deleted": True})()

    monkeypatch.setattr("server.identity.app.namespace.handle_namespace_delete", _delete)
    with pytest.raises(AuthInsufficientError):
        await handle_tracker_source_rotate(
            TrackerSourceRotateInput(org=ORG, name="clickstack", user="z", password="w")
        )
    assert qs.credentials_for("clickstack") == ("x", "y")
    with pytest.raises(AuthInsufficientError):
        await handle_tracker_source_set_mapping(
            TrackerSourceSetMappingInput(org=ORG, name="clickstack", mapping=None)
        )
    with pytest.raises(AuthInsufficientError):
        await handle_tracker_source_remove(
            TrackerSourceRemoveInput(org=ORG, name="clickstack", confirm="clickstack")
        )
    assert deleted == [] and rs.find(ORG, "clickstack") is not None


@pytest.mark.asyncio
async def test_remove_needs_an_admin_of_every_namespace_it_retires(_cfg, monkeypatch) -> None:
    """Retiring deletes every namespace of the source: an admin of one of them may
    rotate it, not retire it, and the refusal names no namespace."""
    ch = _cfg
    await handle_tracker_source_add(_add_input())
    deleted: list[str] = []

    async def _delete(inp: Any) -> Any:
        deleted.append(inp.namespace)
        return type("O", (), {"deleted": True})()

    monkeypatch.setattr("server.identity.app.namespace.handle_namespace_delete", _delete)
    ch.grant_admin("bob", "tracker-clickstack")
    token = current_auth_var.set(_auth("bob"))
    try:
        with pytest.raises(AuthInsufficientError) as ei:
            await handle_tracker_source_remove(
                TrackerSourceRemoveInput(org=ORG, name="clickstack", confirm="clickstack")
            )
    finally:
        current_auth_var.reset(token)
    assert "tracker-" not in str(ei.value)
    assert deleted == [] and rs.find(ORG, "clickstack") is not None


@pytest.mark.asyncio
async def test_rotate_changes_only_the_credential_and_keeps_the_mapping(_cfg, monkeypatch):
    import server.tracker.app.sources as h
    from server.tracker.domain import mapping as m

    ch = _cfg
    await handle_tracker_source_add(_add_input())
    mapped = m.parse(
        {
            "tables": {
                "github_issues": {
                    "source": "loom_qa.issues",
                    "columns": {"source_id": "id", "number": "id", "title": "t", "created_at": "c"},
                }
            }
        }
    ).to_json()
    ch.inserted["loom.qa_sources"][-1]["mapping_json"] = mapped  # set_mapping ran
    before = dict(ch.inserted["loom.qa_sources"][-1])
    seen: dict[str, Any] = {}
    real_probe = h._probe_or_refuse

    async def spy(**kw: Any) -> Any:
        seen.update(kw)
        return await real_probe(**{**kw, "mapping": None})

    monkeypatch.setattr(h, "_probe_or_refuse", spy)
    await handle_tracker_source_rotate(
        TrackerSourceRotateInput(org=ORG, name="clickstack", user="loom_ro2", password="n3w")
    )
    # the new credential is checked through the source's own mapping
    assert seen["mapping"] is not None and seen["mapping"].to_json() == mapped
    after = ch.inserted["loom.qa_sources"][-1]
    assert after["mapping_json"] == mapped and after["user"] == "loom_ro2"
    moved = {"user", "password_enc", "version", "updated_at"}
    assert {k: v for k, v in after.items() if k not in moved} == {
        k: v for k, v in before.items() if k not in moved
    }
    assert qs.credentials_for("clickstack") == ("loom_ro2", "n3w")


@pytest.mark.asyncio
async def test_remove_retires_the_row_and_the_namespaces(_cfg, monkeypatch) -> None:
    ch = _cfg
    await handle_tracker_source_add(_add_input())
    deleted: list[str] = []

    async def _delete(inp: Any) -> Any:
        deleted.append(inp.namespace)
        return type("O", (), {"deleted": True})()

    monkeypatch.setattr("server.identity.app.namespace.handle_namespace_delete", _delete)
    with pytest.raises(ValidationError) as ei:
        await handle_tracker_source_remove(
            TrackerSourceRemoveInput(org=ORG, name="clickstack", confirm="no")
        )
    assert ei.value.to_detail()["code"] == "CONFIRM_REQUIRED"
    out = await handle_tracker_source_remove(
        TrackerSourceRemoveInput(org=ORG, name="clickstack", confirm="clickstack")
    )
    assert out.deactivated and out.namespaces_retired == [
        "tracker-clickstack",
        "tracker-stack-private",
    ]
    assert deleted == ["tracker-clickstack", "tracker-stack-private"]
    assert ch.inserted["loom.qa_sources"][-1]["active"] is False
    assert "clickstack" not in qs.endpoints() and rs.find(ORG, "clickstack") is None
    with pytest.raises(ValidationError) as ei:
        await handle_tracker_source_remove(
            TrackerSourceRemoveInput(org=ORG, name="clickstack", confirm="clickstack")
        )
    assert ei.value.to_detail()["code"] == "NOT_A_SOURCE"
    assert tracker_source_remove.audit and tracker_source_remove.idempotent


@pytest.mark.asyncio
async def test_a_later_version_row_keeps_the_registration_time(_cfg, monkeypatch) -> None:
    """Rotate (and every later version row) carries created_at forward;
    only updated_at moves, even when the two writes straddle a second
    boundary."""
    from datetime import timedelta

    ch = _cfg
    await handle_tracker_source_add(_add_input())
    registered = ch.inserted["loom.qa_sources"][-1]["created_at"]
    later = registered + timedelta(seconds=5)
    monkeypatch.setattr(rs, "_now", lambda: later)
    await handle_tracker_source_rotate(
        TrackerSourceRotateInput(org=ORG, name="clickstack", user="loom_ro2", password="n3w")
    )
    after = ch.inserted["loom.qa_sources"][-1]
    assert after["user"] == "loom_ro2"
    assert after["created_at"] == registered
    assert after["updated_at"] == later


@pytest.mark.asyncio
async def test_a_check_slower_than_its_timeout_says_unreachable(monkeypatch) -> None:
    import asyncio

    from server.settings import settings

    async def _slow(sql, params=None, *, credential="qa", **kw):
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(qi, "query_rows", _slow)
    monkeypatch.setattr(settings, "tracker_source_check_timeout_s", 0.05)
    reports.reset_for_tests()
    out = await handle_tracker_source_check(TrackerSourceCheckInput(org=ORG, name="qa"))
    assert not out.reachable and out.contract["ok"] is None


def _restricted_only() -> TrackerSourceAddInput:
    return _add_input(
        namespaces=[
            TrackerSourceNamespaceSpec(
                namespace="tracker-stack-private",
                repos=["ClickHouse/stack-private"],
                restricted=True,
            )
        ]
    )


@pytest.mark.asyncio
async def test_a_restricted_only_source_is_managed_by_its_registrants_direct_grant(
    _cfg, monkeypatch
) -> None:
    """The registrant's admin token predates the restricted namespace and cannot
    name it; the direct admin grant the add wrote is what admits her, and its
    revocation ends it. Her viewer token is refused for all the grant, as the
    restricted gate caps a grant at the token's own role; an org-wide token with
    no grant there is refused."""
    ch = _cfg
    await handle_tracker_source_add(_restricted_only())
    await handle_tracker_source_rotate(
        TrackerSourceRotateInput(org=ORG, name="clickstack", user="x", password="y")
    )
    assert qs.credentials_for("clickstack") == ("x", "y")
    with pytest.raises(AuthInsufficientError):
        await _rotate_as(_auth("alice", Role.VIEWER))
    wildcard = AuthInfo(
        subject="ops", org=ORG, namespaces=("*",), roles=(Role.ORG_ADMIN,), is_service_account=False
    )
    token = current_auth_var.set(wildcard)
    try:
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_rotate(
                TrackerSourceRotateInput(org=ORG, name="clickstack", user="o", password="p")
            )
    finally:
        current_auth_var.reset(token)
    ch.revoke_all()
    with pytest.raises(AuthInsufficientError):
        await handle_tracker_source_rotate(
            TrackerSourceRotateInput(org=ORG, name="clickstack", user="z", password="w")
        )
    assert qs.credentials_for("clickstack") == ("x", "y")


@pytest.mark.asyncio
async def test_the_registrant_retires_a_mixed_source_and_both_its_namespaces(
    _cfg, monkeypatch
) -> None:
    """Through the gate: the standard namespace by the gate at admin, the restricted
    one by the registrant's direct grant; neither is left live without its source."""
    await handle_tracker_source_add(_add_input())
    deleted: list[str] = []

    async def _delete(inp: Any) -> Any:
        deleted.append(inp.namespace)
        return type("O", (), {"deleted": True})()

    monkeypatch.setattr("server.identity.app.namespace.handle_namespace_delete", _delete)
    out = await handle_tracker_source_remove(
        TrackerSourceRemoveInput(org=ORG, name="clickstack", confirm="clickstack")
    )
    assert out.deactivated
    assert deleted == out.namespaces_retired == ["tracker-clickstack", "tracker-stack-private"]


_PRIVATE = "tracker-stack-private"


async def _rotate_as(token: AuthInfo) -> None:
    reset = current_auth_var.set(token)
    try:
        await handle_tracker_source_rotate(
            TrackerSourceRotateInput(org=ORG, name="clickstack", user="o", password="p")
        )
    finally:
        current_auth_var.reset(reset)


def _token(**over: Any) -> AuthInfo:
    base: dict[str, Any] = {
        "subject": "sa:bot",
        "org": ORG,
        "namespaces": (),
        "roles": (Role.ADMIN,),
        "is_service_account": True,
    }
    base.update(over)
    return AuthInfo(**base)


@pytest.fixture()
def _active_creator(monkeypatch):
    import server.auth.delegation as delegation

    async def _active(org: str, creator: str) -> bool:
        return True

    monkeypatch.setattr(delegation, "creator_account_active", _active)


@pytest.mark.parametrize(
    "token",
    [
        # flat delegated admin whose claim does not reach the restricted namespace
        {"creator": "alice", "namespaces": ("team-a",)},
        # a flat delegated child whose own role there is below admin
        {"creator": "alice", "namespaces": (_PRIVATE,), "roles": (Role.WRITER,)},
        # a token of alice's own whose write claim leaves the namespace out
        {"subject": "alice", "is_service_account": False, "write_namespaces": ("team-a",)},
    ],
    ids=["delegated-flat-claim-misses", "delegated-writer", "own-write-claim-misses"],
)  # fmt: skip
@pytest.mark.asyncio
async def test_a_token_whose_own_ceiling_misses_the_restricted_namespace_is_refused(
    _cfg, _active_creator, token
) -> None:
    """The token's own role on the restricted namespace caps what the grant gives,
    as at the restricted gate: alice (the creator, or the subject) holds admin there."""
    await handle_tracker_source_add(_restricted_only())
    with pytest.raises(AuthInsufficientError):
        await _rotate_as(_token(**token))


@pytest.mark.asyncio
async def test_a_delegated_token_needs_its_own_grant_and_its_creators(
    _cfg, _active_creator
) -> None:
    """As at the restricted gate for a delegated token: the subject's own direct
    admin grant and its creator's, within the token's own role there."""
    ch = _cfg
    await handle_tracker_source_add(_restricted_only())  # alice holds admin there
    child = _token(creator="alice", namespaces=(_PRIVATE,))
    with pytest.raises(AuthInsufficientError):
        await _rotate_as(child)  # the child holds no grant of its own
    ch.grant_admin("sa:bot", _PRIVATE)
    await _rotate_as(child)
    assert qs.credentials_for("clickstack") == ("o", "p")
    ch.revoke_all()
    ch.grant_admin("sa:bot", _PRIVATE)  # its own grant alone, the creator's gone
    with pytest.raises(AuthInsufficientError):
        await _rotate_as(child)


@pytest.mark.asyncio
async def test_a_change_needs_every_namespace_and_inspect_needs_one(_cfg, monkeypatch) -> None:
    """set_mapping and rotate change what every namespace of the source reads, so
    they need all of them, the public one too; inspecting by name reads the
    credential, one."""
    from server.tracker.api.models import TrackerSourceInspectInput, TrackerSourceSetMappingInput
    from server.tracker.app.sources import (
        handle_tracker_source_inspect,
        handle_tracker_source_set_mapping,
    )

    ch = _cfg
    await handle_tracker_source_add(_add_input())
    ch.grant_admin("bob", "tracker-clickstack")
    token = current_auth_var.set(_auth("bob"))
    try:
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_set_mapping(
                TrackerSourceSetMappingInput(org=ORG, name="clickstack", mapping=None)
            )
        out = await handle_tracker_source_inspect(
            TrackerSourceInspectInput(org=ORG, name="clickstack")
        )
    finally:
        current_auth_var.reset(token)
    assert out.database == "loom_qa"
    # carol administers the private namespace only: no rotate either
    ch.grant_admin("carol", "tracker-stack-private")
    token = current_auth_var.set(_auth("carol"))
    try:
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_rotate(
                TrackerSourceRotateInput(org=ORG, name="clickstack", user="o", password="p")
            )
    finally:
        current_auth_var.reset(token)


@pytest.mark.asyncio
async def test_the_restricted_kill_switch_refuses_the_registrant(_cfg, monkeypatch) -> None:
    await handle_tracker_source_add(_restricted_only())
    monkeypatch.setattr(settings, "restricted_serve", False)
    with pytest.raises(AuthInsufficientError):
        await handle_tracker_source_rotate(
            TrackerSourceRotateInput(org=ORG, name="clickstack", user="o", password="p")
        )


@pytest.mark.asyncio
async def test_an_unreadable_classification_refuses_and_is_the_servers(_cfg, monkeypatch) -> None:
    from server.auth import fail_closed
    from server.auth.directory import ClassificationUnavailable
    from server.core import request_work

    await handle_tracker_source_add(_add_input())

    async def _unreadable(org: str, namespace: str) -> bool:
        raise ClassificationUnavailable(org)

    monkeypatch.setattr(nc, "is_restricted", _unreadable)
    request_work.begin()
    with pytest.raises(AuthInsufficientError):
        await handle_tracker_source_rotate(
            TrackerSourceRotateInput(org=ORG, name="clickstack", user="o", password="p")
        )
    assert fail_closed.answered_unverified()


@pytest.mark.asyncio
async def test_a_scoped_token_is_refused_at_the_transport_gate() -> None:
    """These ops name no namespace for the gate (`skip_namespace_authz`), and a token
    with `ns_roles` cannot be applied without one: it never reaches the handler."""
    from server.core.errors import IdentityIncompleteError
    from server.operations.registry import ALL_OPERATIONS
    from server.transport import gates

    op = next(o for o in ALL_OPERATIONS if o.name == "tracker.source.rotate")
    scoped = _token(
        subject="alice",
        is_service_account=False,
        ns_roles=((_PRIVATE, "admin"),),
        namespaces=(_PRIVATE,),
    )
    target = gates.authz_target(op, org=ORG, namespace=None, auth=scoped)
    with pytest.raises(IdentityIncompleteError):
        await gates.authorize(op, scoped, target, transport="http")


@pytest.mark.parametrize("restricted_only", [True, False], ids=["private", "mixed"])
@pytest.mark.asyncio
async def test_a_dashboard_session_cannot_change_or_retire_a_private_tracker(
    _cfg, monkeypatch, restricted_only
) -> None:
    """A dashboard session is a primary flat writer token; its person holds the admin
    grants the add wrote. The restricted namespace caps them at the token's role, so
    rotate, set_mapping and remove are refused; on a mixed source inspect still reads
    through the public namespace's grant."""
    from server.tracker.api.models import TrackerSourceInspectInput, TrackerSourceSetMappingInput
    from server.tracker.app.sources import (
        handle_tracker_source_inspect,
        handle_tracker_source_set_mapping,
    )

    await handle_tracker_source_add(_restricted_only() if restricted_only else _add_input())

    async def _delete(inp: Any) -> Any:
        raise AssertionError("a refused retire deletes nothing")

    monkeypatch.setattr("server.identity.app.namespace.handle_namespace_delete", _delete)
    session = current_auth_var.set(_auth("alice", Role.WRITER))
    try:
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_rotate(
                TrackerSourceRotateInput(org=ORG, name="clickstack", user="o", password="p")
            )
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_set_mapping(
                TrackerSourceSetMappingInput(org=ORG, name="clickstack", mapping=None)
            )
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_remove(
                TrackerSourceRemoveInput(org=ORG, name="clickstack", confirm="clickstack")
            )
        inspect = handle_tracker_source_inspect(
            TrackerSourceInspectInput(org=ORG, name="clickstack")
        )
        if restricted_only:
            with pytest.raises(AuthInsufficientError):
                await inspect
        else:
            assert (await inspect).database == "loom_qa"
    finally:
        current_auth_var.reset(session)


@pytest.mark.asyncio
async def test_a_namespace_the_source_registered_restricted_stays_so_without_its_row(
    _cfg, monkeypatch
) -> None:
    """Until the directory row of a restricted namespace is written, the source's own
    flag keeps it restricted, so an org-wide admin token with no grant there does
    not reach it the way a standard namespace would. Once the row is written and
    reads standard, the row decides, as it does for every read."""
    ch = _cfg
    await handle_tracker_source_add(_restricted_only())
    rows = list(ch.namespace_rows)
    ch.namespace_rows.clear()

    async def _not_restricted(org: str, namespace: str) -> bool:
        return False

    monkeypatch.setattr(nc, "is_restricted", _not_restricted)
    wildcard = AuthInfo(
        subject="ops", org=ORG, namespaces=("*",), roles=(Role.ORG_ADMIN,), is_service_account=False
    )
    with pytest.raises(AuthInsufficientError):
        await _rotate_as(wildcard)
    # a row another replica restricted, newer than this process's snapshot
    ch.namespace_rows.extend(rows)
    with pytest.raises(AuthInsufficientError):
        await _rotate_as(wildcard)
    for row in rows:
        row["classification"] = "standard"
    await _rotate_as(wildcard)


@pytest.mark.asyncio
async def test_a_trusted_caller_acting_for_the_registrant_keeps_the_naming_rule(_cfg) -> None:
    """Impersonation keeps the bearer's claims with the subject's grants: the
    caller reaches alice's private tracker only with a token naming it."""
    await handle_tracker_source_add(_restricted_only())
    acting = _token(subject="alice", impersonated_by="svc", is_service_account=False)
    with pytest.raises(AuthInsufficientError):
        await _rotate_as(acting)
    await _rotate_as(_token(subject="alice", impersonated_by="svc", namespaces=(_PRIVATE,)))
    assert qs.credentials_for("clickstack") == ("o", "p")


@pytest.mark.parametrize(
    ("results", "server_s"),
    [
        # one refused outright, one unreadable: refused either way, the caller's
        ({"tracker-clickstack": "no", "tracker-stack-private": "failed"}, False),
        # the only refusal is the unreadable one: the server's
        ({"tracker-clickstack": "yes", "tracker-stack-private": "failed"}, True),
    ],
    ids=["refused-and-failed", "only-failed"],
)
@pytest.mark.asyncio
async def test_a_refusal_is_the_servers_only_when_a_failed_read_decided_it(
    _cfg, monkeypatch, results, server_s
) -> None:
    import server.tracker.app.sources as h
    from server.auth import fail_closed
    from server.core import request_work

    await handle_tracker_source_add(_add_input())

    async def _admits(auth: Any, org: str, namespace: str, *, flagged: bool) -> bool:
        if results[namespace] == "failed":
            fail_closed.note_unreadable()
        return results[namespace] == "yes"

    monkeypatch.setattr(h, "_admits", _admits)
    request_work.begin()
    with pytest.raises(AuthInsufficientError):
        await handle_tracker_source_rotate(
            TrackerSourceRotateInput(org=ORG, name="clickstack", user="o", password="p")
        )
    assert fail_closed.answered_unverified() is server_s


@pytest.mark.parametrize("op", ["rotate", "set_mapping", "remove", "inspect"])
@pytest.mark.asyncio
async def test_a_source_whose_namespaces_did_not_parse_is_refused(_cfg, monkeypatch, op) -> None:
    """A damaged `namespaces_json` reads as no namespace; "every namespace" of none
    admits nobody, so each op refuses rather than reading `all([])` as admin."""
    from server.auth import fail_closed
    from server.core import request_work
    from server.tracker.api.models import TrackerSourceInspectInput, TrackerSourceSetMappingInput
    from server.tracker.app.sources import (
        handle_tracker_source_inspect,
        handle_tracker_source_set_mapping,
    )

    ch = _cfg
    await handle_tracker_source_add(_add_input())
    ch.inserted["loom.qa_sources"][-1]["namespaces_json"] = "{not json"
    request_work.begin()
    calls = {
        "rotate": lambda: handle_tracker_source_rotate(
            TrackerSourceRotateInput(org=ORG, name="clickstack", user="o", password="p")
        ),
        "set_mapping": lambda: handle_tracker_source_set_mapping(
            TrackerSourceSetMappingInput(org=ORG, name="clickstack", mapping=None)
        ),
        "remove": lambda: handle_tracker_source_remove(
            TrackerSourceRemoveInput(org=ORG, name="clickstack", confirm="clickstack")
        ),
        "inspect": lambda: handle_tracker_source_inspect(
            TrackerSourceInspectInput(org=ORG, name="clickstack")
        ),
    }
    with pytest.raises(AuthInsufficientError):
        await calls[op]()
    found = rs.find(ORG, "clickstack")
    assert found is not None and found.namespaces == {}  # still registered, no namespace
    assert fail_closed.answered_unverified() is False  # the caller's refusal, not the server's


@pytest.mark.asyncio
async def test_the_listing_reports_the_classification_the_checks_use(_cfg) -> None:
    """The card reads `classification` as `_admits` and every read do: the directory
    row once written, the source's own flag until then."""
    ch = _cfg
    await handle_tracker_source_add(_restricted_only())
    rows = list(ch.namespace_rows)
    ch.namespace_rows.clear()

    async def classifications() -> list[str]:
        listed = await handle_tracker_source_list(TrackerSourceListInput(org=ORG))
        [src_view] = [s for s in listed.sources if s["name"] == "clickstack"]
        return [n["classification"] for n in src_view["namespaces"]]

    assert await classifications() == ["restricted"]
    for row in rows:
        row["classification"] = "standard"
    ch.namespace_rows.extend(rows)
    assert await classifications() == ["standard"]
