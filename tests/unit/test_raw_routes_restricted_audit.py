"""The raw HTTP routes leave the restricted-access audit row the op gate leaves.

`knowledge.download`, `knowledge.upload`, `task/watch`,
`admin/claim_release` and the token mints that grant a restricted namespace
are not registry ops, so the transport gate
(`server.transport.gates.authorize`) never runs for them: each authorizes
itself and must write the `namespace.restricted_access` row the gate writes
for an admitted op. The contract test holds every raw route to it: a route
module that names one of the namespace checks in `_ADMISSION` calls
`note_restricted_access` or `note_restricted_grants`. It reads names per
module, so a route that admits through a check under another name, or
through a module-level helper of another file, needs its name added there.
"""

from __future__ import annotations

import ast
import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

import server.identity.app.namespace_classification as nc
from server.auth.jwt import AuthInfo
from server.identity.app import token_grants
from server.memory.domain.trust import actor_for
from server.operations.base import Role

ORG, NS, PLAIN = "acme", "vault", "plain"
# the real grant write, before a fixture stubs it
_REAL_GRANT_TO_TOKEN = token_grants.grant_restricted_to_token
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """`NS` is restricted, alice's bearer verifies, and every audit row lands here."""
    from server.settings import settings

    # restored after the test, whatever the test does to it
    monkeypatch.setattr(nc, "_cache", {ORG: (1e12, frozenset({NS}))})
    rows: list[tuple[str, dict[str, Any]]] = []

    async def _emit(event_type: str, **kw: Any) -> str:
        from server.auth.context import current_auth

        # the principal bound to the request: what fills the row's token_id column
        rows.append((event_type, {**kw, "bound": current_auth()}))
        return "hash"

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    monkeypatch.setattr(settings, "restricted_audit_inline", True)
    alice = AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=(NS, PLAIN),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    monkeypatch.setattr("server.auth.bearer.verify_token", lambda token: alice)
    yield rows


def _admit_all(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(module, "check_authorized", _allowed)


def _assert_one_row(rows: list[tuple[str, dict[str, Any]]], op: str, namespace: str = NS) -> None:
    ((event, kw),) = rows
    assert event == "namespace.restricted_access"
    assert (kw["org"], kw["namespace"]) == (ORG, namespace)
    assert kw["subject"]["op"] == op and kw["subject"]["transport"] == "http"
    assert kw["actor"] == actor_for("alice", is_service_account=False)  # the caller


class _Directory:
    """A ClickHouse client that answers the classification read: `NS` is restricted."""

    _client = object()

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, str]]:
        return [{"namespace": NS}]


@pytest.fixture
def cold_directory(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Call to empty the classification cache and answer its read from `_Directory`."""
    import server.core.ch as ch

    def _install() -> None:
        monkeypatch.setattr(nc, "_cache", {})
        monkeypatch.setattr(ch, "get_client", lambda: _Directory())

    return _install


async def _no_doc(*_a: Any, **_k: Any) -> None:
    return None


async def _inbox(org: str, inbox: str) -> dict[str, Any]:
    return {"org": org, "inbox": inbox, "owner_namespace": NS}


class _Rendered:
    """An empty profile render."""

    section_count = 0
    rendered = ""


def _client(module: Any) -> AsyncClient:
    app = FastAPI()
    if hasattr(module, "attach"):
        module.attach(app)
    else:
        app.include_router(module.router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _one_frame(**_k: Any) -> Any:
    yield "retry: 1000\n\n"


BEARER = {"Authorization": "Bearer alice"}


@pytest.mark.asyncio
async def test_an_admitted_download_writes_the_audit_row(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]
) -> None:
    from server.knowledge.app import download_routes
    from server.knowledge.store import documents as documents_repo

    _admit_all(monkeypatch, download_routes)

    monkeypatch.setattr(documents_repo, "get_one", _no_doc)
    async with _client(download_routes) as c:
        r = await c.get(
            "/v1/knowledge.download",
            params={"org": ORG, "namespace": NS, "source_uri": "a.md"},
            headers=BEARER,
        )
    assert r.status_code == 404  # admitted: the miss is the document's, not the gate's
    _assert_one_row(audit_rows, "knowledge.download")
    assert audit_rows[0][1]["bound"].subject == "alice"


@pytest.mark.asyncio
async def test_a_refused_download_writes_no_audit_row(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]
) -> None:
    from server.knowledge.app import download_routes

    async def _refused(*_a: Any, **_k: Any) -> None:
        raise PermissionError("cannot access")

    monkeypatch.setattr(download_routes, "check_authorized", _refused)
    async with _client(download_routes) as c:
        r = await c.get(
            "/v1/knowledge.download",
            params={"org": ORG, "namespace": NS, "source_uri": "a.md"},
            headers=BEARER,
        )
    assert r.status_code == 403
    assert audit_rows == []


@pytest.mark.asyncio
async def test_an_admitted_upload_writes_the_audit_row(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]
) -> None:
    from server.knowledge.app import upload_routes

    _admit_all(monkeypatch, upload_routes)
    monkeypatch.setattr(upload_routes, "_admit", lambda *_a: None)

    async def _store(*_a: Any) -> JSONResponse:
        return JSONResponse({"upload_id": "u"}, status_code=201)

    monkeypatch.setattr(upload_routes, "_store", _store)
    async with _client(upload_routes) as c:
        r = await c.post(
            "/v1/knowledge.upload",
            params={"org": ORG, "namespace": NS, "filename": "a.md"},
            content=b"# a\n",
            headers=BEARER,
        )
    assert r.status_code == 201, r.text
    _assert_one_row(audit_rows, "knowledge.upload")


@pytest.mark.asyncio
async def test_an_admitted_inbox_watch_writes_the_audit_row_for_the_owner_namespace(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]
) -> None:
    from server.tasks.app import common as task_common
    from server.tasks.app import watch_routes
    from server.tasks.store import repo

    async def _in_scope(
        org: str, namespace: str, *, write: bool, hides_answer: bool = True
    ) -> bool:
        return True

    async def _no_events(org: str, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(repo, "fetch_inbox_row", _inbox)
    monkeypatch.setattr(repo, "events_since", _no_events)
    monkeypatch.setattr(watch_routes, "event_stream", _one_frame)  # no real-time wait
    monkeypatch.setattr(task_common, "_in_scope", _in_scope)
    async with _client(watch_routes) as c:
        r = await c.get(
            "/v1/task/watch",
            params={"org": ORG, "inbox": "bot", "max_seconds": 1},
            headers=BEARER,
        )
    assert r.status_code == 200
    _assert_one_row(audit_rows, "task.watch")


@pytest.mark.asyncio
async def test_the_real_check_fills_the_snapshot_the_audit_row_reads(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any], cold_directory: Any
) -> None:
    """Cold classification cache, real `check_authorized`: the check reads the
    directory, and the row is written from what it read."""
    import server.auth.authz as authz
    from server.knowledge.app import download_routes
    from server.knowledge.store import documents as documents_repo

    async def _direct_grant(org: str, namespace: str, subject: str) -> Role:
        return Role.WRITER

    cold_directory()
    monkeypatch.setattr(authz, "grant_role_for", _direct_grant)
    monkeypatch.setattr(documents_repo, "get_one", _no_doc)
    async with _client(download_routes) as c:
        r = await c.get(
            "/v1/knowledge.download",
            params={"org": ORG, "namespace": NS, "source_uri": "a.md"},
            headers=BEARER,
        )
    assert r.status_code == 404, r.text
    _assert_one_row(audit_rows, "knowledge.download")


@pytest.mark.asyncio
async def test_the_real_inbox_check_fills_the_snapshot_the_audit_row_reads(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any], cold_directory: Any
) -> None:
    """Cold classification cache, real `row_in_scope`: the owner namespace's
    classification is read by the check and the row is written for it."""
    import server.auth.authz as authz
    from server.tasks.app import watch_routes
    from server.tasks.store import repo

    async def _direct_grant(org: str, namespace: str, subject: str) -> Role:
        return Role.WRITER

    async def _no_events(org: str, **_k: Any) -> list[Any]:
        return []

    cold_directory()
    monkeypatch.setattr(authz, "grant_role_for", _direct_grant)
    monkeypatch.setattr(repo, "fetch_inbox_row", _inbox)
    monkeypatch.setattr(repo, "events_since", _no_events)
    monkeypatch.setattr(watch_routes, "event_stream", _one_frame)  # no real-time wait
    async with _client(watch_routes) as c:
        r = await c.get(
            "/v1/task/watch",
            params={"org": ORG, "inbox": "bot", "max_seconds": 1},
            headers=BEARER,
        )
    assert r.status_code == 200, r.text
    _assert_one_row(audit_rows, "task.watch")


@pytest.mark.asyncio
async def test_a_standard_namespace_writes_no_audit_row(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]
) -> None:
    from server.knowledge.app import download_routes
    from server.knowledge.store import documents as documents_repo

    _admit_all(monkeypatch, download_routes)

    monkeypatch.setattr(documents_repo, "get_one", _no_doc)
    async with _client(download_routes) as c:
        r = await c.get(
            "/v1/knowledge.download",
            params={"org": ORG, "namespace": PLAIN, "source_uri": "a.md"},
            headers=BEARER,
        )
    assert r.status_code == 404
    assert audit_rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("refusal", "filename", "status"),
    [("unauthorized", "a.md", 403), ("no filename", "/", 400), ("legacy Office", "a.doc", 415)],
)
async def test_a_refused_upload_writes_no_audit_row(
    monkeypatch: pytest.MonkeyPatch,
    audit_rows: list[Any],
    refusal: str,
    filename: str,
    status: int,
) -> None:
    from server.knowledge.app import upload_routes

    async def _refused(*_a: Any, **_k: Any) -> None:
        raise PermissionError("cannot access")

    if refusal == "unauthorized":
        monkeypatch.setattr(upload_routes, "check_authorized", _refused)
    else:
        _admit_all(monkeypatch, upload_routes)
    async with _client(upload_routes) as c:
        r = await c.post(
            "/v1/knowledge.upload",
            params={"org": ORG, "namespace": NS, "filename": filename},
            content=b"# a\n",
            headers=BEARER,
        )
    assert r.status_code == status, r.text
    assert audit_rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("reader", "status"), [(False, 404), (True, 403)])
async def test_a_refused_inbox_watch_writes_no_audit_row(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any], reader: bool, status: int
) -> None:
    from server.tasks.app import common as task_common
    from server.tasks.app import watch_routes
    from server.tasks.store import repo

    async def _in_scope(
        org: str, namespace: str, *, write: bool, hides_answer: bool = True
    ) -> bool:
        return reader and not write

    monkeypatch.setattr(repo, "fetch_inbox_row", _inbox)
    monkeypatch.setattr(task_common, "_in_scope", _in_scope)
    async with _client(watch_routes) as c:
        r = await c.get(
            "/v1/task/watch",
            params={"org": ORG, "inbox": "bot", "max_seconds": 1},
            headers=BEARER,
        )
    assert r.status_code == status
    assert audit_rows == []


class _Delegated:
    """What `delegate` hands a mint: a token naming the restricted namespace."""

    org = ORG
    namespaces = (NS,)
    ns_roles = {NS: "viewer"}

    def mint(self, subject: str) -> str:
        return "jwt"

    def expires_at(self, now: Any) -> None:
        return None

    def as_auth(self, subject: str) -> AuthInfo:
        return AuthInfo(
            subject=subject,
            org=ORG,
            namespaces=self.namespaces,
            roles=(Role.VIEWER,),
            is_service_account=True,
        )


@pytest.fixture
def mint_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Auth on; `delegate` admits, and the grant write grants `NS`."""
    from contextlib import asynccontextmanager

    import server.auth.grant_claims as grant_claims
    from server.identity.app import me_service_accounts, me_setup, token_grants
    from server.settings import settings

    async def _delegate(*_a: Any, **_k: Any) -> _Delegated:
        return _Delegated()

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def _granted(org: str, sa_subject: str, creator: str, ns_roles: Any) -> list[str]:
        return [ns for ns in ns_roles if ns == NS]

    @asynccontextmanager
    async def _change(*_a: Any) -> Any:
        yield

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(grant_claims, "service_account_change", _change)
    monkeypatch.setattr(token_grants, "grant_restricted_to_token", _granted)
    for module in (me_service_accounts, me_setup):
        monkeypatch.setattr(module, "delegate", _delegate)
    monkeypatch.setattr(me_service_accounts, "refuse_taken_name", _nothing)
    monkeypatch.setattr(me_service_accounts, "save_token", _nothing)
    return me_service_accounts, me_setup


@pytest.mark.asyncio
async def test_a_service_account_mint_onto_a_restricted_namespace_writes_the_audit_row(
    mint_env: Any, audit_rows: list[Any]
) -> None:
    me_service_accounts, _ = mint_env
    async with _client(me_service_accounts) as c:
        r = await c.post(
            "/v1/me/service_accounts.create",
            json={"name": "bot", "namespaces": [NS]},
            headers=BEARER,
        )
    assert r.status_code == 200, r.text
    _assert_one_row(audit_rows, "me.service_accounts.create")
    assert audit_rows[0][1]["subject"]["granted_to"] == r.json()["sa_subject"]


@pytest.mark.asyncio
async def test_a_setup_mint_onto_a_restricted_namespace_writes_the_audit_row(
    monkeypatch: pytest.MonkeyPatch, mint_env: Any, audit_rows: list[Any]
) -> None:
    from server.identity.app import me_callers

    _, me_setup = mint_env

    async def _restricted(org: str, subject: str, raw: object) -> list[str]:
        return [NS, "other"]

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def _role(*_a: Any) -> str:
        return "viewer"

    # the real `_save_mcp_token`; its grant write (mint_env) grants only NS
    monkeypatch.setattr(me_setup, "save_token", _nothing)
    monkeypatch.setattr(me_callers, "caller_role_on", _role)
    # both asked-for names are restricted: only the granted one may get a row
    monkeypatch.setitem(nc._cache, ORG, (1e12, frozenset({NS, "other"})))

    async def _no_default(*_a: Any) -> str:
        return ""

    monkeypatch.setattr(me_setup, "_validate_restricted_request", _restricted)
    monkeypatch.setattr(me_setup, "default_namespace", _no_default)
    monkeypatch.setattr(
        "server.auth.public_url.public_base_url", lambda request: "https://loom.example.com"
    )
    async with _client(me_setup) as c:
        r = await c.post(
            "/v1/me/setup/mcp_config",
            json={"client": "cursor", "restricted_namespaces": [NS, "other"]},
            headers=BEARER,
        )
    assert r.status_code == 200, r.text
    _assert_one_row(audit_rows, "me.setup.mcp_config")
    assert audit_rows[0][1]["subject"]["granted_to"] == r.json()["sa_subject"]


@pytest.mark.asyncio
async def test_the_mcp_primer_profile_read_writes_the_audit_row(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]
) -> None:
    """The primer renders the default namespace's profile outside the op gate."""
    from server.memory.app import profile
    from server.transport import mcp_primer

    async def _render(_input: Any) -> _Rendered:
        return _Rendered()

    monkeypatch.setattr(profile, "handle_profile_render", _render)
    alice = AuthInfo(
        subject="alice", org=ORG, namespaces=(NS,), roles=(Role.VIEWER,), is_service_account=False
    )
    await mcp_primer._loom_primer_text(alice, defaults={"org": ORG, "namespace": NS})
    ((event, kw),) = audit_rows
    assert kw["bound"] is alice
    assert (event, kw["namespace"], kw["subject"]["op"], kw["subject"]["transport"]) == (
        "namespace.restricted_access",
        NS,
        "mcp.primer",
        "mcp",
    )


@pytest.mark.asyncio
async def test_a_mint_from_a_cold_snapshot_writes_the_audit_row(
    monkeypatch: pytest.MonkeyPatch, mint_env: Any, audit_rows: list[Any], cold_directory: Any
) -> None:
    """The real grant write reads the classification, which fills the snapshot
    the row reads."""
    from server.identity.store import grants as grants_repo

    me_service_accounts, _ = mint_env

    async def _written(rows: list[dict[str, Any]]) -> None:
        return None

    monkeypatch.setattr(token_grants, "grant_restricted_to_token", _REAL_GRANT_TO_TOKEN)
    monkeypatch.setattr(grants_repo, "insert_rows", _written)
    cold_directory()
    async with _client(me_service_accounts) as c:
        r = await c.post(
            "/v1/me/service_accounts.create",
            json={"name": "bot", "namespaces": [NS]},
            headers=BEARER,
        )
    assert r.status_code == 200, r.text
    _assert_one_row(audit_rows, "me.service_accounts.create")
    assert audit_rows[0][1]["subject"]["granted_to"] == r.json()["sa_subject"]


@pytest.mark.asyncio
async def test_the_mcp_primer_leaves_the_profile_out_when_the_classification_fails(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]
) -> None:
    from server.auth.directory import ClassificationUnavailable
    from server.memory.app import profile
    from server.transport import mcp_primer

    rendered: list[Any] = []

    async def _render(input: Any) -> Any:
        rendered.append(input)
        raise AssertionError("rendered without a classification")

    async def _unreadable(org: str, namespace: str) -> bool:
        raise ClassificationUnavailable("down")

    monkeypatch.setattr(profile, "handle_profile_render", _render)
    monkeypatch.setattr(nc, "is_restricted", _unreadable)
    alice = AuthInfo(
        subject="alice", org=ORG, namespaces=(NS,), roles=(Role.VIEWER,), is_service_account=False
    )
    text = await mcp_primer._loom_primer_text(alice, defaults={"org": ORG, "namespace": NS})
    assert rendered == [] and audit_rows == []
    assert "always-loaded context" not in text


@pytest.mark.asyncio
async def test_the_mcp_primer_profile_read_of_a_standard_namespace_writes_no_row(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]
) -> None:
    from server.memory.app import profile
    from server.transport import mcp_primer

    async def _render(_input: Any) -> _Rendered:
        return _Rendered()

    monkeypatch.setattr(profile, "handle_profile_render", _render)
    alice = AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=(PLAIN,),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    await mcp_primer._loom_primer_text(alice, defaults={"org": ORG, "namespace": PLAIN})
    assert audit_rows == []


# The checks that admit a caller to a namespace outside the op transport.
_ADMISSION = {
    "check_authorized",
    "row_in_scope",
    "_in_scope",
    "restricted_allows",
    "_may_release",
    "grant_restricted_to_token",
    "delegate",
    "admits",
    "may_be_restricted",
    "authorize_op",
}
_AUDIT = {"note_restricted_access", "note_restricted_grants"}
# The op transports admit through `gates.authorize`, which writes the row
# (tests/unit/test_admission_parity.py). The MCP primer's profile read is the one
# read there outside the gate; its own test above holds it to the row.
_OP_TRANSPORTS = {"server.transport.http", "server.transport.mcp"}


def _names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


def _endpoint_modules(routes: list[Any]) -> set[str]:
    """The modules of every endpoint, through included routers."""
    out: set[str] = set()
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            out |= _endpoint_modules(included.routes)
        elif getattr(route, "endpoint", None) is not None:
            out.add(route.endpoint.__module__)
    return out


def test_every_raw_route_that_admits_to_a_namespace_writes_the_audit_row() -> None:
    from server.main import app

    modules = _endpoint_modules(app.routes) - _OP_TRANSPORTS
    assert "server.knowledge.app.download_routes" in modules  # the walk sees the raw routes
    missing = []
    for name in sorted(modules):
        path = Path(inspect.getfile(sys.modules[name]))
        names = _names(path)
        if names & _ADMISSION and not names & _AUDIT:
            missing.append(str(path.relative_to(ROOT)))
    assert not missing, (
        "these route modules name a namespace check but never write the row through "
        "server.identity.app.restricted_access (checked per module, not per route): "
        + ", ".join(missing)
    )
