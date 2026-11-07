"""The raw HTTP routes leave the restricted-access audit row an op leaves.

`knowledge.download`, `knowledge.upload`, `task/watch` and
`admin/claim_release` are not registry ops, so the transport gate
(`server.transport.gates.authorize`) never runs for them: each authorizes
itself and must write the `namespace.restricted_access` row the gate writes
for an admitted op. The contract test holds every raw
route to it: a route module that admits a caller to a namespace calls
`note_restricted_access`.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

import server.identity.app.namespace_classification as nc
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG, NS = "acme", "vault"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def audit_rows(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """`NS` is restricted, alice's bearer verifies, and every audit row lands here."""
    from server.settings import settings

    nc.reset()
    nc._cache[ORG] = (1e12, frozenset({NS}))
    rows: list[tuple[str, dict[str, Any]]] = []

    async def _emit(event_type: str, **kw: Any) -> str:
        rows.append((event_type, kw))
        return "hash"

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    monkeypatch.setattr(settings, "restricted_audit_inline", True)
    alice = AuthInfo(
        subject="alice", org=ORG, namespaces=(NS,), roles=(Role.WRITER,), is_service_account=False
    )
    monkeypatch.setattr("server.auth.bearer.verify_token", lambda token: alice)
    yield rows
    nc.reset()


def _admit_all(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    async def _allowed(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(module, "check_authorized", _allowed)


def _assert_one_row(rows: list[tuple[str, dict[str, Any]]], op: str, namespace: str = NS) -> None:
    ((event, kw),) = rows
    assert event == "namespace.restricted_access"
    assert (kw["org"], kw["namespace"]) == (ORG, namespace)
    assert kw["subject"]["op"] == op and kw["subject"]["transport"] == "http"


def _client(module: Any) -> AsyncClient:
    app = FastAPI()
    module.attach(app)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


BEARER = {"Authorization": "Bearer alice"}


@pytest.mark.asyncio
async def test_an_admitted_download_writes_the_audit_row(
    monkeypatch: pytest.MonkeyPatch, audit_rows: list[Any]
) -> None:
    from server.knowledge.app import download_routes
    from server.knowledge.store import documents as documents_repo

    _admit_all(monkeypatch, download_routes)

    async def _no_doc(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(documents_repo, "get_one", _no_doc)
    async with _client(download_routes) as c:
        r = await c.get(
            "/v1/knowledge.download",
            params={"org": ORG, "namespace": NS, "source_uri": "a.md"},
            headers=BEARER,
        )
    assert r.status_code == 404  # admitted: the miss is the document's, not the gate's
    _assert_one_row(audit_rows, "knowledge.download")


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

    async def _inbox(org: str, inbox: str) -> dict[str, Any]:
        return {"org": org, "inbox": inbox, "owner_namespace": NS}

    async def _in_scope(
        org: str, namespace: str, *, write: bool, hides_answer: bool = True
    ) -> bool:
        return True

    async def _no_events(org: str, **_k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(repo, "fetch_inbox_row", _inbox)
    monkeypatch.setattr(repo, "events_since", _no_events)
    monkeypatch.setattr(task_common, "_in_scope", _in_scope)
    async with _client(watch_routes) as c:
        r = await c.get(
            "/v1/task/watch",
            params={"org": ORG, "inbox": "bot", "max_seconds": 1},
            headers=BEARER,
        )
    assert r.status_code == 200
    _assert_one_row(audit_rows, "task.watch")


# What admits a caller to a namespace outside the op transport.
_ADMISSION = {"check_authorized", "row_in_scope", "_in_scope", "restricted_allows"}
# The op transports admit through `gates.authorize`, which writes the row
# (tests/unit/test_admission_parity.py).
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
        if names & _ADMISSION and "note_restricted_access" not in names:
            missing.append(str(path.relative_to(ROOT)))
    assert not missing, (
        "these route modules admit a caller to a namespace without "
        "server.identity.app.restricted_access.note_restricted_access: " + ", ".join(missing)
    )
