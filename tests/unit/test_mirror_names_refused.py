"""No token reaches a mirror (a namespace named `acl-<24 hex>`), with auth on or off, and
no org-wide scope or export lists one (docs/architecture/document-sources.md, The mirror)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest

from server.auth import authz
from server.auth import context as auth_context
from server.auth.context import resolve_org_scope, set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError, ValidationError
from server.core.naming import is_mirror_name, not_mirror_sql
from server.core.paths import MIGRATIONS_DIR
from server.core.schema import migrate, prechecks
from server.identity.api.models import NamespaceRestoreInput, NamespaceSnapshotOutput
from server.identity.app import namespace_snapshot
from server.identity.app.namespace import refuse_unwritable_name
from server.insights.app import admin_routes
from server.insights.app.admin_models import AuditSummaryInput, EventsTailInput
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import gates
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH
from tests.unit.clickhouse_local import ddl as local_ddl

if TYPE_CHECKING:
    from clickhouse_connect.driver.client import Client

MIRROR = "acl-0123456789abcdef01234567"
NOT_MIRRORS = [
    "acl-0123456789abcdef0123456",  # 23 digits
    "acl-0123456789abcdef012345678",  # 25
    "acl-0123456789ABCDEF01234567",  # upper case
    "acl-0123456789abcdef0123456g",
    "xacl-0123456789abcdef01234567",  # 24 digits, but not at the start
    "team",
]


def _platform_admin() -> AuthInfo:
    return AuthInfo(
        subject="root",
        org="*",
        namespaces=("*",),
        roles=(Role.PLATFORM_ADMIN,),
        is_service_account=False,
    )


@pytest.fixture(autouse=True)
def _no_auth() -> Any:
    set_current_auth(None)
    yield
    set_current_auth(None)


def test_only_acl_and_24_lowercase_hex_digits_is_a_mirror_name() -> None:
    assert is_mirror_name(MIRROR)
    assert not any(is_mirror_name(n) for n in NOT_MIRRORS)


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_sql_term_leaves_out_exactly_the_mirror_names() -> None:
    names = [MIRROR, *NOT_MIRRORS]
    rows = await LocalCH().query_all(
        f"SELECT namespace FROM (SELECT arrayJoin({{names:Array(String)}}) AS namespace) "
        f"WHERE {not_mirror_sql()}",
        {"names": names},
    )
    assert sorted(r["namespace"] for r in rows) == sorted(NOT_MIRRORS)


@pytest.mark.parametrize("auth", [None, _platform_admin()])
async def test_the_gate_admits_no_token_to_a_mirror(auth: AuthInfo | None) -> None:
    with pytest.raises(PermissionError):
        await authz.check_authorized(auth, Role.VIEWER, org="o", namespace=MIRROR)


async def test_the_listings_gate_admits_no_token_to_a_mirror() -> None:
    gate = authz.CachedGate(_platform_admin(), "o")
    assert not await gate.admits(MIRROR, Role.VIEWER)


async def test_an_id_addressed_check_opens_no_mirror_with_auth_off() -> None:
    assert await authz.row_reach("o", MIRROR) is False
    assert await authz.row_in_scope("o", MIRROR) is False


async def test_a_call_naming_a_mirror_is_refused_before_the_auth_off_return() -> None:
    op = OPERATIONS_BY_NAME["knowledge.search"]
    target = gates.authz_target(op, org="o", namespace=MIRROR, auth=None)
    with pytest.raises(PermissionError):
        await gates.authorize(op, None, target, transport="http")


async def test_a_call_that_would_register_a_mirror_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An op that skips the namespace check and names none registers its plane's default.
    op = dataclasses.replace(OPERATIONS_BY_NAME["knowledge.search"], skip_namespace_authz=True)
    monkeypatch.setattr(gates, "_plane_default", lambda op, org: MIRROR)
    target = gates.authz_target(op, org="o", namespace=None, auth=None)
    assert target.namespace is None and target.named_namespace is None
    with pytest.raises(PermissionError):
        await gates.authorize(op, None, target, transport="http")


async def test_a_call_naming_a_mirror_is_refused_before_anything_else_with_auth_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached: list[str] = []

    async def _held(org: Any, auth: Any) -> None:
        reached.append("tenancy")

    monkeypatch.setattr("server.auth.tenancy.ensure_held", _held)
    op = OPERATIONS_BY_NAME["knowledge.search"]
    target = gates.authz_target(op, org="o", namespace=MIRROR, auth=_platform_admin())
    with pytest.raises(PermissionError):
        await gates.authorize(op, _platform_admin(), target, transport="http")
    assert reached == []


async def test_an_org_wide_scope_with_auth_off_leaves_out_the_mirrors() -> None:
    scope = await resolve_org_scope("o")
    assert scope.accessible and scope.where == f" AND {not_mirror_sql()}"


async def test_a_platform_admin_s_org_wide_scope_leaves_out_the_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _none(org: str) -> list[str]:
        return []

    monkeypatch.setattr(auth_context, "restricted_namespaces_hidden_from", _none)
    set_current_auth(_platform_admin())
    assert not_mirror_sql() in (await resolve_org_scope("o")).where


async def test_the_namespaces_listed_with_auth_off_leave_out_a_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _granted(org: str, subjects: Any) -> set[str]:
        return {"team", MIRROR}

    async def _aliases(org: str, subject: str) -> list[str]:
        return [subject]

    async def _no_creator(org: str, subject: str) -> None:
        return None

    monkeypatch.setattr(auth_context, "_granted_namespaces", _granted)
    monkeypatch.setattr("server.auth.aliases.subject_aliases", _aliases)
    monkeypatch.setattr("server.auth.service_accounts.inheriting_creator", _no_creator)
    assert await auth_context.resolve_my_namespaces("o", "alice") == ["team"]


def test_the_reserved_names_include_the_mirror_form() -> None:
    with pytest.raises(ValidationError, match="reserved for mirrors"):
        refuse_unwritable_name(MIRROR)
    refuse_unwritable_name(NOT_MIRRORS[0])


async def test_restore_refuses_a_mirror_as_its_target() -> None:
    # The gate sees `org` only: the handler is what refuses `target_namespace`.
    payload = NamespaceSnapshotOutput(
        snapshot_id="88888888-8888-8888-8888-888888888888",
        org="o",
        source_namespace="team",
        created_at=datetime.now(UTC),
        memory_count=0,
        type_count=0,
        grant_count=0,
        audit_tail_count=0,
        include_embeddings=False,
        memories=[],
        types=[],
        grants=[],
        audit_tail=[],
    )
    restore = NamespaceRestoreInput(org="o", target_namespace=MIRROR, payload=payload)
    with pytest.raises(ValidationError, match="reserved for mirrors"):
        await namespace_snapshot.handle_namespace_restore(restore)


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.result_rows = [tuple(r.values()) for r in rows]


class _LocalServer:
    """The migration client's `query`, run on a clickhouse-local fake."""

    def __init__(self, local: LocalCH) -> None:
        self.local = local

    def query(self, sql: str, parameters: Any = None) -> _Result:
        return _Result(asyncio.run(self.local.query_all(sql, parameters or {})))


def _server(*rows: str, tables: Iterable[str] = tuple(prechecks._LIVE_ROWS)) -> Client:
    """The tables the check reads (every one by default, so each liveness condition
    runs) holding `rows`, as the migration client the checks take."""
    made = [local_ddl(t) for t in tables]
    local = LocalCH("SET enable_quantized_codec = 1", *made, *rows)
    return cast("Client", _LocalServer(local))


def _live_row(table: str, ns: str) -> str:
    """A live row of `ns` in `table`, with only the columns it needs."""
    columns, values = {
        "namespaces": ("", ""),
        "code_repos": ("", ""),
        "knowledge_sources": ("", ""),
        "memory": (", agent, user_id, session_id, memory_key, value", ", 'a', '-', '-', 'k', 'v'"),
        "documents": (", source_uri", ", 'https://d/1'"),
        "conversation_threads": (", thread_id", ", 't'"),
        "conversations": ("", ""),
    }[table]
    return f"INSERT INTO loom.{table} (org, namespace{columns}) VALUES ('o', '{ns}'{values})"


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_the_migration_refuses_while_a_namespace_of_the_form_holds_rows() -> None:
    check = prechecks.PRECHECKS["mirror_names_reserved.sql"]
    check(_server(_live_row("memory", "team"), _live_row("memory", "acl-short")))
    rows = [_live_row(table, MIRROR) for table in prechecks._LIVE_ROWS]
    with pytest.raises(RuntimeError) as refused:
        check(_server(*rows))
    named = re.search(rf"o/{MIRROR} \(([^)]*)\)", str(refused.value))
    assert named, refused.value
    assert sorted(named.group(1).split(", ")) == sorted(prechecks._LIVE_ROWS)


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_a_live_name_among_many_dead_ones_is_still_found() -> None:
    dead = (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, value,"
        " tombstoned_at) SELECT 'o', concat('acl-', leftPad(lower(hex(number)), 24, '0')), 'a', '-',"
        " '-', 'k', '', now() FROM numbers(150)"
    )
    with pytest.raises(RuntimeError, match=MIRROR):
        prechecks.PRECHECKS["mirror_names_reserved.sql"](_server(dead, _live_row("memory", MIRROR)))


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_an_erased_and_deleted_namespace_of_the_form_passes_the_check() -> None:
    erased = (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key,"
        f" value, tombstoned_at) VALUES ('o', '{MIRROR}', 'a', '-', '-', 'k', '', now())"
    )
    deleted = f"INSERT INTO loom.namespaces (org, namespace, active) VALUES ('o', '{MIRROR}', 0)"
    prechecks.PRECHECKS["mirror_names_reserved.sql"](_server(erased, deleted))


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_the_refusal_names_ten_and_counts_the_rest() -> None:
    live = (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, value)"
        " SELECT 'o', concat('acl-', leftPad(lower(hex(number)), 24, '0')), 'a', '-', '-', 'k',"
        " 'v' FROM numbers(15)"
    )
    with pytest.raises(RuntimeError, match="and 5 more"):
        prechecks.PRECHECKS["mirror_names_reserved.sql"](_server(live))


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_a_table_a_dry_run_has_not_made_yet_is_skipped() -> None:
    # A new server's dry run: only the registry exists so far.
    server = _server(tables=["namespaces"])
    prechecks.PRECHECKS["mirror_names_reserved.sql"](server)


def test_every_precheck_is_keyed_by_a_migration_file() -> None:
    names = {migrate._unnumbered(p.name) for p in MIGRATIONS_DIR.glob("*.sql")}
    assert set(prechecks.PRECHECKS) <= names


@pytest.mark.parametrize(
    ("route", "body"),
    [
        (admin_routes.admin_audit_summary, AuditSummaryInput),
        (admin_routes.admin_events_tail, EventsTailInput),
    ],
)
async def test_the_admin_ledger_reads_refuse_a_mirror_s_name(route: Any, body: Any) -> None:
    with pytest.raises(AuthInsufficientError):
        await route(body(namespace=MIRROR))


class _Runner:
    """The migration client as the runner uses it: no ledger, nothing pending."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
        return _Result([])

    def command(self, sql: str, settings: Any = None) -> None:
        self.commands.append(sql)

    def insert(self, *a: Any, **k: Any) -> None:
        pass


@pytest.mark.parametrize("dry", [True, False])
@pytest.mark.parametrize("refuses", [True, False])
def test_the_runner_checks_a_file_before_it_dry_runs_or_applies_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, dry: bool, refuses: bool
) -> None:
    checked: list[bool] = []

    def _check(ch: Any) -> None:
        checked.append(True)
        if refuses:
            raise RuntimeError(f"{MIRROR} exists")

    monkeypatch.setitem(migrate.PRECHECKS, "checked.sql", _check)
    monkeypatch.setattr(migrate, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(migrate, "_codec_settings", lambda ch: migrate.NO_CODEC)
    monkeypatch.setattr(migrate, "_sync_settings_accepted", lambda ch: frozenset())
    monkeypatch.setattr(migrate, "_sync_level", lambda ch: 2)
    f = tmp_path / "1999_checked.sql"
    f.write_text("CREATE TABLE IF NOT EXISTS loom.t (x UInt8) ENGINE = Memory;\n")
    ch = _Runner()
    rc = migrate._apply_pending(ch, [f], dry=dry, lease=None)
    assert checked == [True] and rc == (1 if refuses else 0)
    if refuses or dry:
        assert ch.commands == []
    else:
        assert ch.commands


_VIOLATION = json.dumps({"operation": "memory.get"})


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_admin_ledger_reads_leave_out_a_mirror_s_rows() -> None:
    # One row per namespace in each ledger the admin routes read.
    from server.insights.store import admin as admin_store

    rows = [
        stmt
        for ns in ("team", MIRROR)
        for stmt in (
            "INSERT INTO loom.agent_events (ts, org, namespace, operation, kind)"
            f" VALUES (now64(3), 'o', '{ns}', 'memory.get', 'request')",
            "INSERT INTO loom.audit_log (ts, org, namespace, event_type)"
            f" VALUES (now64(3), 'o', '{ns}', 'e')",
            "INSERT INTO loom.events (kind, org, namespace, payload)"
            f" VALUES ('budget_violation', 'o', '{ns}', '{_VIOLATION}')",
        )
    ]
    rows += [
        "INSERT INTO loom.code_search_outcome (ts, org, namespace, source, action)"
        f" VALUES (now(), 'o', '{ns}', 'agent', 'opened_file')"
        for ns in ("team", MIRROR)
    ]
    tables = ("agent_events", "audit_log", "events", "code_search_outcome")
    ch = LocalCH(*(local_ddl(t) for t in tables), *rows)
    tail = await admin_store.events_tail(3600, 10, ch=ch)
    assert {r["namespace"] for r in tail} == {"team"}
    [summary] = await admin_store.audit_summary(3600, ch=ch)
    assert int(summary["cnt"]) == 1
    cost = await admin_store.fleet_cost(3600, 10, "namespace", ch=ch)
    assert [r["key"] for r in cost] == ["team"]
    violations = await admin_store.budget_violations(3600, 10, ch=ch)
    assert [r["namespace"] for r in violations] == ["team"]
    [consumer] = await admin_store.consumers(3600, 10, ch=ch)
    assert int(consumer["namespaces"]) == 1
    [tagging] = await admin_store.consumer_tagging(3600, 10, ch=ch)
    assert int(tagging["calls"]) == 1
    [outcomes] = await admin_store.consumer_outcomes(3600, 10, ch=ch)
    assert int(outcomes["outcome_rows"]) == 1
