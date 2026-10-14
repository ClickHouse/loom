"""`loom claim-release` releases through the endpoint's write, never ALTER DELETE.

A released namespace still holds its memories. Deleting its loom.namespaces row
would free the name, so the next self-signup for it would become admin over the
previous owner's data. The CLI writes the same version rows as
`POST /v1/admin/claim_release`: grant tombstones and an inactive, ownerless
registry row that keeps the name reserved. Hermetic: the ClickHouse client is faked.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import server.core.ch
import server.identity.app.claim_release as cr
from server.cli.access import _cmd_claim_release
from server.settings import settings

pytestmark = pytest.mark.usefixtures("stub_model_command")

_ROW: dict[str, Any] = {
    "org": "acme",
    "namespace": "vault",
    "owner_email": "alice@acme.test",
    "description": "secrets",
    "active": True,
    "classification": "restricted",
}


class _FakeCH:
    def __init__(self, row: dict[str, Any] | None = _ROW) -> None:
        self.row = row
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.executed: list[str] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        if "loom.namespaces" in sql:
            if self.row is None:
                return None
            return dict(self.row)
        return {"c": 2}

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return [
            {"subject": "alice@acme.test", "role": "admin", "granted_by": "alice@acme.test"},
            {"subject": "bob@acme.test", "role": "writer", "granted_by": "alice@acme.test"},
        ]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))


def _args() -> argparse.Namespace:
    return argparse.Namespace(org="acme", namespace="vault", yes=True)


def test_cli_release_writes_version_rows_not_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH()
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)

    assert _cmd_claim_release(_args()) == 0

    assert fake.executed == [], "claim-release must not mutate: no ALTER ... DELETE"
    assert [t for t, _ in fake.inserts] == ["loom.namespace_grants", "loom.namespaces"]
    grants = fake.inserts[0][1]
    assert {g["subject"] for g in grants} == {"alice@acme.test", "bob@acme.test"}
    assert all(g["revoked_at"] is not None for g in grants)
    (ns_row,) = fake.inserts[1][1]
    assert ns_row["owner_email"] == ""
    assert ns_row["active"] is False
    assert ns_row["description"] == "Released by loom-cli"
    assert ns_row["classification"] == "restricted"


def test_cli_release_of_unclaimed_namespace_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeCH(row=None)
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)

    assert _cmd_claim_release(_args()) == 1
    assert fake.inserts == [] and fake.executed == []


def _post(monkeypatch: pytest.MonkeyPatch, fake: _FakeCH) -> cr.ClaimReleaseOutput:
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(cr, "get_client", lambda: fake)
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    return asyncio.run(
        cr.claim_release(cr.ClaimReleaseInput(org="acme", namespace="vault", reason="left"), req)
    )


def test_endpoint_release_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH()
    out = _post(monkeypatch, fake)

    assert out == cr.ClaimReleaseOutput(
        released=True, namespace="vault", previous_owner="alice@acme.test", grants_revoked=2
    )
    assert [t for t, _ in fake.inserts] == ["loom.namespace_grants", "loom.namespaces"]
    (ns_row,) = fake.inserts[1][1]
    assert ns_row["active"] is False and ns_row["owner_email"] == ""
    assert ns_row["description"] == "Released by local (left)"
    assert fake.executed == []


def test_endpoint_unclaimed_namespace_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH(row=None)
    with pytest.raises(HTTPException) as exc:
        _post(monkeypatch, fake)
    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "NAMESPACE_NOT_FOUND"
    assert fake.inserts == []


def test_cli_release_without_a_terminal_aborts_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeCH()
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)

    def _eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    args = argparse.Namespace(org="acme", namespace="vault", yes=False)
    assert _cmd_claim_release(args) == 1
    assert fake.inserts == [] and fake.executed == []


def test_cli_confirmation_names_the_owner_from_the_registry_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The prompt reads the owner through the namespaces repository; a "no"
    writes nothing."""
    fake = _FakeCH()
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    args = _args()
    args.yes = False
    assert _cmd_claim_release(args) == 1

    assert "currently owned by 'alice@acme.test'" in capsys.readouterr().out
    assert fake.inserts == []


def test_a_release_refused_for_too_many_spellings_says_what_to_release_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.core.errors import TooManySpellingsError

    async def _too_many(*_a: Any, **_k: Any) -> None:
        raise TooManySpellingsError("too many accounts", subject="bob@acme.test", limit=1000)

    fake = _FakeCH()
    monkeypatch.setattr(server.core.ch, "get_client", lambda: fake)
    monkeypatch.setattr(cr.grant_writes, "write_pair_cutoffs", _too_many)

    assert _cmd_claim_release(_args()) == 1
    assert fake.inserts == []
    err = capsys.readouterr().err
    assert "more than 1000 accounts name its grantee 'bob@acme.test'" in err
    assert "the ones 'bob@acme.test' holds no grant on first, then this one" in err
    assert "errors.md#too-many-spellings" in err


def test_an_endpoint_release_refused_for_too_many_spellings_answers_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.core.errors import TooManySpellingsError, register_exception_handler

    async def _too_many(*_a: Any, **_k: Any) -> None:
        raise TooManySpellingsError("too many accounts", subject="bob@acme.test", limit=1000)

    fake = _FakeCH()
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(cr, "get_client", lambda: fake)
    monkeypatch.setattr(cr.grant_writes, "write_pair_cutoffs", _too_many)
    app = FastAPI()
    register_exception_handler(app)
    cr.attach(app)

    r = TestClient(app).post("/v1/admin/claim_release", json={"org": "acme", "namespace": "vault"})

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "TOO_MANY_SPELLINGS"
    assert detail["subject"] == "bob@acme.test" and detail["limit"] == 1000
    assert "more than 1000 accounts name its grantee 'bob@acme.test'" in detail["message"]
    assert "#too_many_spellings" in r.headers["link"]
    assert fake.inserts == []
