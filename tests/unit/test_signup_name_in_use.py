"""Self-signup cannot claim a name that already holds data or grants.

A namespace can hold memory, documents or live grants without a loom.namespaces
row (auto-registration failed or was off, data older than it, a grant made before
the first write). Claiming such a name would make a stranger its admin, so both
sign-up routes refuse it exactly as they refuse a registered name. The route tests
fake ClickHouse; the statement itself runs on clickhouse-local against every table
it reads.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi import HTTPException

import server.auth.self_signup as ss
from server.erasure.store.census import census_tables, namespace_tables
from server.identity.store import namespace_census
from tests.unit import clickhouse_local


class _CH:
    """No registry rows; the in-use read answers for the names in `used`."""

    def __init__(self, used: set[str] | None = None, *, broken: bool = False) -> None:
        self.used = used or set()
        self.broken = broken
        self.inserts: list[str] = []
        self.registered: set[str] = set()

    async def query_one(self, sql: str, params: Any = None) -> Any:
        p = params or {}
        if "AS in_use" in sql:
            if self.broken:
                raise ConnectionError("clickhouse unreachable")
            return {"in_use": 1} if p.get("ns") in self.used else None
        if "count()" in sql:
            return {"n": 0}
        if "FROM loom.namespaces FINAL" in sql and p.get("ns") in self.registered:
            return {"owner": "someone", "x": 1}
        return None

    async def insert(self, table: str, *a: Any, **k: Any) -> None:
        self.inserts.append(table)


@pytest.fixture()
def ch(monkeypatch: pytest.MonkeyPatch, auth_attempts: Any) -> _CH:
    from server.settings import settings

    monkeypatch.setattr(settings, "self_signup", "open")
    monkeypatch.setattr(settings, "oauth_login_provider", "password")
    monkeypatch.setattr(settings, "self_signup_org", "dev")
    fake = _CH()
    monkeypatch.setattr(ss, "get_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def claims(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The deployment-wide claims, without a lock table in the fake."""
    from tests.unit.claims_fake import memory_claims

    return memory_claims(monkeypatch)


class _Req:
    headers: dict[str, str] = {"host": "loom.test"}
    client = type("C", (), {"host": "10.0.0.1"})()
    url = type("U", (), {"scheme": "http"})()


def _signup(namespace: str) -> Any:
    body = ss.SelfSignupInput(namespace=namespace, email="eve@evil.test", password="hunter2hunter2")
    return asyncio.run(ss.self_signup(body, _Req()))  # type: ignore[arg-type]


def _agent_signup(name: str) -> Any:
    return asyncio.run(ss.agent_signup(ss.AgentSignupInput(name=name), _Req()))  # type: ignore[arg-type]


def _refusal(call: Any, name: str) -> tuple[int, Any]:
    with pytest.raises(HTTPException) as ei:
        call(name)
    return ei.value.status_code, ei.value.detail


def test_a_name_holding_data_or_grants_is_refused_as_a_taken_one(ch: _CH) -> None:
    ch.registered = {"team"}
    taken = _refusal(_signup, "team")
    ch.registered, ch.used = set(), {"team"}
    assert _refusal(_signup, "team") == taken
    assert taken[0] == 409 and taken[1]["code"] == "NAMESPACE_CLAIMED"
    assert ch.inserts == []


def test_an_agent_name_holding_data_or_grants_is_refused_as_a_taken_one(ch: _CH) -> None:
    ch.registered = {"agent-triage"}
    taken = _refusal(_agent_signup, "triage")
    ch.registered, ch.used = set(), {"agent-triage"}
    assert _refusal(_agent_signup, "triage") == taken
    assert taken[0] == 409 and taken[1]["code"] == "NAMESPACE_CLAIMED"
    assert ch.inserts == []


# Each route with the name it signs up for and the namespace that name claims.
_ROUTES = [(_signup, "team", "team"), (_agent_signup, "team", "agent-team")]


@pytest.mark.parametrize(("call", "name", "namespace"), _ROUTES)
def test_a_sign_up_holds_the_claim_namespace_register_holds(
    ch: _CH, claims: Any, monkeypatch: pytest.MonkeyPatch, call: Any, name: str, namespace: str
) -> None:
    """`namespace.register` and `namespace.restore` hold the account's claim from their
    free-name check to their write: a sign-up of the same name holds it too, so the two
    cannot both find the name free. It comes after the name claim, which never waits,
    and before the address's, in the claims' order."""

    @asynccontextmanager
    async def _account_change(org: str, ns: str, **_k: Any) -> AsyncIterator[None]:
        claims.taken.append(f"account:{org}/{ns}")
        yield

    monkeypatch.setattr(ss, "account_change", _account_change)
    call(name)
    assert claims.taken[:2] == [f"signup:dev:{namespace}", f"account:dev/{namespace}"]
    assert claims.taken[2:] in ([], [ss.email_claim("dev", "eve@evil.test")])


@pytest.mark.parametrize(("call", "name", "namespace"), _ROUTES)
def test_a_sign_up_does_not_proceed_while_a_register_holds_the_name(
    ch: _CH, claims: Any, monkeypatch: pytest.MonkeyPatch, call: Any, name: str, namespace: str
) -> None:
    """With the account's claim held (a `namespace.register` of the name in progress on
    another replica), the sign-up waits it out and gives up, writing nothing, with the
    answer another sign-up of the name gets."""
    from server.auth import grant_claims

    monkeypatch.setattr(grant_claims, "take", claims.take)
    claims.held.add("account:" + json.dumps(["dev", namespace]))
    assert _refusal(call, name)[1]["code"] == "SIGNUP_IN_PROGRESS"
    assert ch.inserts == []


@pytest.mark.parametrize(("call", "name", "namespace"), _ROUTES)
def test_a_configured_code_repository_is_in_use_before_its_first_row(
    ch: _CH, monkeypatch: pytest.MonkeyPatch, call: Any, name: str, namespace: str
) -> None:
    from server.code.domain.index import repos

    configured = {("dev", namespace)}
    monkeypatch.setattr(
        repos,
        "config_entry",
        lambda org, ns: object() if (org, ns) in configured else None,
    )
    status, detail = _refusal(call, name)
    assert status == 409 and detail["code"] == "NAMESPACE_CLAIMED"
    assert ch.inserts == []


@pytest.mark.parametrize(("call", "name", "namespace"), _ROUTES)
def test_a_configured_tracker_namespace_is_in_use_before_its_registry_row(
    ch: _CH, monkeypatch: pytest.MonkeyPatch, call: Any, name: str, namespace: str
) -> None:
    from server.identity.app import directory

    monkeypatch.setattr(
        directory,
        "tracker_namespace",
        lambda org, ns: object() if (org, ns) == ("dev", namespace) else None,
    )
    status, detail = _refusal(call, name)
    assert status == 409 and detail["code"] == "NAMESPACE_CLAIMED"
    assert ch.inserts == []


def test_an_unused_name_still_signs_up(ch: _CH) -> None:
    ch.used = {"someone-else"}
    assert _signup("fresh").namespace == "fresh"
    assert _agent_signup("fresh").namespace == "agent-fresh"
    assert ch.inserts.count("loom.namespaces") == 2


def test_an_unreadable_check_provisions_nothing(ch: _CH) -> None:
    ch.broken = True
    with pytest.raises(ConnectionError):
        _signup("fresh")
    with pytest.raises(ConnectionError):
        _agent_signup("fresh")
    assert ch.inserts == []


# ---- the statement, on clickhouse-local ----

needs_clickhouse = pytest.mark.skipif(
    clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed"
)


def _local_ch(*rows: str) -> clickhouse_local.LocalCH:
    """Every table the check reads, as schema.sql defines it, plus `rows`."""
    tables = [
        *(t.removeprefix("loom.") for t in namespace_census.STATE_TABLES),
        *(t.removeprefix("loom.") for t in namespace_tables()),
    ]
    return clickhouse_local.LocalCH(
        *clickhouse_local.EMBEDDINGS[:1],
        "SET allow_suspicious_codecs = 1",
        *map(clickhouse_local.ddl, tables),
        *rows,
    )


_MEMORY = (
    "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, value)"
    " VALUES ('dev', 'team', 'bot', '-', '-', 'k', 'v')"
)


def _grant(subject: str, version: int, revoked: bool = False) -> str:
    revoked_at = "now()" if revoked else "NULL"
    return (
        "INSERT INTO loom.namespace_grants (org, namespace, subject, role, granted_by,"
        f" revoked_at, version) VALUES ('dev', 'team', '{subject}', 'writer', 'op',"
        f" {revoked_at}, {version})"
    )


def _in_use(ch: clickhouse_local.LocalCH, org: str = "dev", namespace: str = "team") -> bool:
    return asyncio.run(namespace_census.in_use(org, namespace, namespace_tables(), ch=ch))  # type: ignore[arg-type]


@needs_clickhouse
def test_a_memory_row_marks_the_name_in_use_in_its_org_only() -> None:
    ch = _local_ch(_MEMORY)
    assert _in_use(ch)
    assert not _in_use(ch, namespace="other")
    assert not _in_use(ch, org="elsewhere")


@needs_clickhouse
def test_a_live_grant_marks_the_name_in_use_and_a_revoked_one_does_not() -> None:
    assert _in_use(_local_ch(_grant("kim", 1)))
    assert not _in_use(_local_ch(_grant("kim", 1), _grant("kim", 2, revoked=True)))


@needs_clickhouse
def test_a_pending_join_request_does_not_mark_the_name_in_use() -> None:
    pending = (
        "INSERT INTO loom.namespace_grants (org, namespace, subject, role, granted_by,"
        " version, status) VALUES ('dev', 'team', 'kim', 'viewer', 'kim', 1, 'pending')"
    )
    assert not _in_use(_local_ch(pending))


@needs_clickhouse
def test_a_tombstoned_row_and_a_document_alone_each_mark_the_name_in_use() -> None:
    tombstoned = (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, value,"
        " tombstoned_at) VALUES ('dev', 'team', 'bot', '-', '-', 'k', 'v', now())"
    )
    assert _in_use(_local_ch(tombstoned))
    blob = (
        "INSERT INTO loom.document_blobs (org, namespace, sha256, data, size)"
        f" VALUES ('dev', 'team', '{'0' * 64}', 'bytes', 5)"
    )
    assert _in_use(_local_ch(blob))


def test_the_lookup_leaves_out_only_the_tables_it_names() -> None:
    read = set(namespace_tables())
    left_out = set(census_tables("namespace")) - read
    assert left_out == {
        "loom.idempotency",  # counted org-wide: no namespace column
        "loom.memory_code_link_queue",  # a week-long queue, not ordered by org
        "loom.llm_response_cache",  # a week-long cache, not ordered by org
        "loom.memory_code_links",  # beside loom.memory rows
        "loom.blob_uploads",  # beside loom.document_blobs rows
        "loom.code_answers",  # its own predicate: an answer cache quoting loom.memory rows
    }
    assert {"loom.memory", "loom.documents", "loom.chunks", "loom.code_chunks"} <= read


def test_every_table_the_lookup_reads_is_ordered_by_org_and_namespace() -> None:
    """A free name reads each table to the end of its (org, namespace) range: a new
    census table keyed otherwise would be a scan on every sign-up."""
    schema = (clickhouse_local.ROOT / "schemas" / "schema.sql").read_text()
    # keyed otherwise, each a small table or the org's rows
    lead_of = {
        "loom.tasks": "org, inbox",
        "loom.task_events": "org, inbox",
        "loom.task_inboxes": "org, inbox",
        "loom.qa_sources": "org, name",
        "loom.erasure_requests": "id",
        "loom.sso_links": "email, org, namespace",
    }
    for table in [*namespace_tables(), *namespace_census.STATE_TABLES]:
        body = schema.split(f"CREATE TABLE IF NOT EXISTS {table}\n", 1)[1].split(";", 1)[0]
        key = re.search(r"ORDER BY \(?([^)\n]*)", body)
        assert key is not None, table
        assert key.group(1).startswith(lead_of.get(table, "org, namespace")), (table, key)


@needs_clickhouse
def test_an_empty_database_holds_no_name() -> None:
    assert not _in_use(_local_ch())


def test_the_in_use_read_is_sequential_for_every_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row or grant another replica wrote a moment ago counts, whichever path asks
    (the personal-namespace auto-claim holds no sequential scope of its own)."""
    from server.core.ch import sequential_reads_active
    from server.identity.app.directory import IdentityDirectory

    seen: list[bool] = []

    async def _in_use(*_a: Any, **_k: Any) -> bool:
        seen.append(sequential_reads_active())
        return False

    monkeypatch.setattr(namespace_census, "in_use", _in_use)
    assert not asyncio.run(IdentityDirectory().in_use("dev", "team"))
    assert seen == [True]


def test_a_memory_only_postgres_holds_marks_the_name_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the Postgres authority, a memory committed but not yet delivered to
    ClickHouse is in no census table there."""
    from server.core import pg
    from server.identity.app.directory import IdentityDirectory

    asked: list[tuple[Any, ...]] = []

    async def _fetchrow(sql: str, *args: Any) -> Any:
        asked.append(args)
        return {"?column?": 1} if args == ("dev", "team") else None

    async def _ch_finds_nothing(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "fetchrow", _fetchrow)
    monkeypatch.setattr(namespace_census, "in_use", _ch_finds_nothing)
    assert asyncio.run(IdentityDirectory().in_use("dev", "team"))
    assert not asyncio.run(IdentityDirectory().in_use("dev", "other"))
    assert asked == [("dev", "team"), ("dev", "other")]


def test_a_collection_name_is_not_signed_up(ch: _CH) -> None:
    """A collection is made only with collection.create, as namespace.register enforces."""
    status, detail = _refusal(_signup, "col-team")
    assert status == 422 and detail["code"] == "VALIDATION_FAILED"
    assert ch.inserts == []


@needs_clickhouse
def test_a_live_webhook_marks_the_name_in_use_and_a_deleted_one_does_not() -> None:
    """A webhook outlives the grant it was registered under: the next owner's writes would
    reach its URL."""
    hook = (
        "INSERT INTO loom.memory_webhooks (org, namespace, webhook_id, url, secret_ref,"
        " events, is_deleted, version) VALUES ('dev', 'team', 'w1', 'https://x.test', 'r',"
        " [], {deleted}, {version})"
    )
    assert _in_use(_local_ch(hook.format(deleted=0, version=1)))
    deleted = hook.format(deleted=1, version=2)
    assert not _in_use(_local_ch(hook.format(deleted=0, version=1), deleted))


@needs_clickhouse
def test_a_task_inbox_the_name_owns_marks_it_in_use() -> None:
    """An inbox outlives the grant it was registered under: its tasks and its webhook
    would reach the name's next admin."""
    inbox = (
        "INSERT INTO loom.task_inboxes (org, inbox, owner_agent, owner_namespace, version,"
        " is_deleted) VALUES ('dev', 'triage', 'bot', 'team', {version}, {deleted})"
    )
    assert _in_use(_local_ch(inbox.format(version=1, deleted=0)))
    gone = inbox.format(version=2, deleted=1)
    assert not _in_use(_local_ch(inbox.format(version=1, deleted=0), gone))


@needs_clickhouse
def test_a_tracker_source_feeding_the_name_marks_it_in_use() -> None:
    """A self-service source outlives its creator's grant and keeps feeding its
    namespaces; one it no longer names, or a retired one, does not count."""
    source = (
        "INSERT INTO loom.qa_sources (org, name, namespaces_json, active, version) VALUES"
        " ('dev', 'stack', '{{\"team\": {{\"repos\": []}}}}', {active}, {version})"
    )
    assert _in_use(_local_ch(source.format(active="true", version=1)))
    retired = source.format(active="false", version=2)
    assert not _in_use(_local_ch(source.format(active="true", version=1), retired))


@needs_clickhouse
def test_another_subjects_live_grant_holds_back_the_owners_completion() -> None:
    """`grants.held_by_another`: the owner's own grant, under any case, and another's
    revoked grant do not count; another's live grant does."""
    from server.identity.store import grants as grants_repo

    def _held(*rows: str) -> bool:
        ch = _local_ch(*rows)
        subjects = ["kim", "kim@corp.test"]  # one person's spellings
        return asyncio.run(grants_repo.held_by_another("dev", "team", subjects, ch=ch))  # type: ignore[arg-type]

    assert not _held(_grant("Kim", 1))
    assert not _held(_grant("Kim@Corp.test", 1))
    assert not _held(_grant("lee", 1), _grant("lee", 2, revoked=True))
    assert _held(_grant("lee", 1))


@needs_clickhouse
@pytest.mark.parametrize(
    ("row", "live"),
    [
        (
            "INSERT INTO loom.knowledge_sources (org, namespace, name, active, version)"
            " VALUES ('dev', 'team', 's', {flag}, 1)",
            ("true", "false"),
        ),
        (
            "INSERT INTO loom.code_repos (org, namespace, repo, active, version)"
            " VALUES ('dev', 'team', 'o/x', {flag}, 1)",
            ("true", "false"),
        ),
        (
            "INSERT INTO loom.collections (org, namespace, owner, deleted, version)"
            " VALUES ('dev', 'team', 'kim', {flag}, 1)",
            ("false", "true"),
        ),
        (
            "INSERT INTO loom.sso_links (email, org, namespace, linked, version)"
            " VALUES ('kim@x.test', 'dev', 'team', {flag}, 1)",
            ("1", "0"),
        ),
        (
            "INSERT INTO loom.erasure_requests (id, org, scope_type, scope_value, requester,"
            " status, version) VALUES (generateUUIDv4(), 'dev', 'namespace', 'team', 'op',"
            " {flag}, 1)",
            ("'in_progress'", "'completed'"),
        ),
        (
            "INSERT INTO loom.erasure_requests (id, org, scope_type, scope_value, requester,"
            " status, version) VALUES (generateUUIDv4(), 'dev', 'namespace_list',"
            " '[\"other\", \"team\"]', 'op', {flag}, 1)",
            ("'failed'", "'completed'"),
        ),
        (
            "INSERT INTO loom.erasure_requests (id, org, scope_type, scope_value, requester,"
            " status, version) VALUES (generateUUIDv4(), 'dev', 'namespace_list',"
            " '{{\"namespaces\": [\"team\"]}}', 'op', {flag}, 1)",
            ("'queued'", "'completed'"),
        ),
    ],
)
def test_a_state_row_counts_only_while_live(row: str, live: tuple[str, str]) -> None:
    on, off = live
    assert _in_use(_local_ch(row.format(flag=on)))
    assert not _in_use(_local_ch(row.format(flag=off)))


@needs_clickhouse
def test_a_promoted_shared_row_marks_the_name_in_use() -> None:
    shared = (
        "INSERT INTO loom.shared (org, namespace, memory_key, value, reversible_until, version)"
        " VALUES ('dev', 'team', 'k', 'v', now(), 1)"
    )
    assert _in_use(_local_ch(shared))
