"""SCIM Groups (server/auth/scim_groups.py): a gateway pushes its org's groups;
members are its user ids, held as `user:<id>`; every change rewrites the row
and moves the org's grant watermark."""

from __future__ import annotations

import functools
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token, verify_token
from server.identity.app import scim_groups

ORG = "acme"
SECRET = "g" * 32


class _Store:
    """loom.principal_groups as the routes read and write it."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.watermarks: list[str] = []

    def _listed(self, sql: str, p: dict[str, Any]) -> list[dict]:
        rows = [
            r for (org, _), r in sorted(self.rows.items()) if org == p["org"] and not r["deleted"]
        ]
        if "v" in p:
            column = "display_name" if "display_name =" in sql else "external_id"
            rows = [r for r in rows if r[column] == p["v"]]
        return rows

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict | None:
        p = params or {}
        if "count()" in sql:
            return {"n": len(self._listed(sql, p))}
        row = self.rows.get((p["org"], p["gid"]))
        return None if row is None or row["deleted"] else row

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        p = params or {}
        if "loom.principal_groups" not in sql:  # the grants a deleted group named: none here
            return []
        return self._listed(sql, p)[p["off"] : p["off"] + p["lim"]]

    async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
        for r in rows:
            if table == "loom.acl_versions":
                self.watermarks.append(r["org"])
            elif table == "loom.principal_groups":
                self.rows[(r["org"], r["group_id"])] = r


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    s = _Store()
    monkeypatch.setattr(scim_groups, "get_client", lambda: s)
    monkeypatch.setattr("server.core.ch.get_client", lambda: s)

    async def _emit(*_a: Any, **_k: Any) -> str:
        return "h"

    monkeypatch.setattr(scim_groups.audit, "emit", _emit)
    monkeypatch.setattr(scim_groups, "verify_token", functools.partial(verify_token, secret=SECRET))
    return s


def _client(org: str = ORG) -> TestClient:
    app = FastAPI()
    scim_groups.attach(app)
    c = TestClient(app)
    token = mint_token("sa:chat", org, [], [], gateway=True, secret=SECRET)
    c.headers["Authorization"] = f"Bearer {token}"
    return c


def test_a_group_is_created_read_and_listed_by_its_external_id(store: _Store) -> None:
    c = _client()
    r = c.post(
        "/scim/v2/Groups",
        json={
            "externalId": "finance",
            "displayName": "Finance",
            "members": [{"value": "bob"}, {"value": "amy"}],
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "finance"
    assert store.rows[(ORG, "finance")]["members"] == ["user:amy", "user:bob"]
    assert store.watermarks == [ORG]
    got = c.get("/scim/v2/Groups/finance").json()
    assert {m["value"] for m in got["members"]} == {"amy", "bob"}
    listed = c.get("/scim/v2/Groups", params={"filter": 'displayName eq "Finance"'}).json()
    assert [g["id"] for g in listed["Resources"]] == ["finance"]
    assert c.post("/scim/v2/Groups", json={"externalId": "finance"}).status_code == 409


def test_members_are_replaced_patched_and_removed(store: _Store) -> None:
    c = _client()
    c.post("/scim/v2/Groups", json={"externalId": "ops", "members": [{"value": "bob"}]})
    r = c.put("/scim/v2/Groups/ops", json={"displayName": "Ops", "members": [{"value": "cat"}]})
    assert r.status_code == 200 and store.rows[(ORG, "ops")]["members"] == ["user:cat"]
    patch = {"Operations": [{"op": "add", "path": "members", "value": [{"value": "dan"}]}]}
    assert c.patch("/scim/v2/Groups/ops", json=patch).status_code == 200
    assert store.rows[(ORG, "ops")]["members"] == ["user:cat", "user:dan"]
    patch = {"Operations": [{"op": "remove", "path": 'members[value eq "cat"]'}]}
    assert c.patch("/scim/v2/Groups/ops", json=patch).status_code == 200
    assert store.rows[(ORG, "ops")]["members"] == ["user:dan"]
    patch = {"Operations": [{"op": "remove", "path": "members"}]}
    assert c.patch("/scim/v2/Groups/ops", json=patch).status_code == 200
    assert store.rows[(ORG, "ops")]["members"] == []
    assert c.delete("/scim/v2/Groups/ops").status_code == 204
    assert c.get("/scim/v2/Groups/ops").status_code == 404
    assert len(store.watermarks) == 6, "every change moves the watermark"


def test_a_member_is_a_user_id_not_a_group(store: _Store) -> None:
    r = _client().post("/scim/v2/Groups", json={"externalId": "g", "members": [{"value": "a b"}]})
    assert r.status_code == 400


def test_a_group_belongs_to_the_token_s_org(store: _Store) -> None:
    _client().post("/scim/v2/Groups", json={"externalId": "hr", "members": [{"value": "bob"}]})
    other = _client("globex")
    assert other.get("/scim/v2/Groups/hr").status_code == 404
    assert other.get("/scim/v2/Groups").json()["totalResults"] == 0


def test_a_token_that_is_not_a_gateway_s_is_refused(store: _Store) -> None:
    c = _client()
    c.headers["Authorization"] = "Bearer " + mint_token(
        "sa:x", ORG, ["*"], ["admin"], secret=SECRET
    )
    assert c.get("/scim/v2/Groups").status_code == 403


def test_the_group_routes_served() -> None:
    from fastapi.routing import APIRoute

    served = {
        (method, r.path)
        for r in scim_groups.router.routes
        if isinstance(r, APIRoute)
        for method in r.methods
    }
    assert served == {
        ("GET", "/scim/v2/Groups"),
        ("POST", "/scim/v2/Groups"),
        ("GET", "/scim/v2/Groups/{group_id}"),
        ("PUT", "/scim/v2/Groups/{group_id}"),
        ("PATCH", "/scim/v2/Groups/{group_id}"),
        ("DELETE", "/scim/v2/Groups/{group_id}"),
    }
