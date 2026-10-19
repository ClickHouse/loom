"""What Okta and Entra ID send, against a live Loom: the checks of Okta's SCIM 2.0
SPEC test (Okta-SCIM-20-SPEC-Test.json) and the requests Entra's provisioning
service and SCIM validator make, in both of Entra's PATCH dialects.

Okta's SPEC test also asks the first listed user for `name.givenName` and
`name.familyName`; Loom keeps no names, so that check is left out.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from tests.integration import test_scim_deprovisioning as e2e

scim_server = e2e.scim_server  # the server, its org and a person's email
_SCIM = e2e._SCIM

_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


def _scim(base: str, method: str, path: str, **kw: Any) -> httpx.Response:
    return httpx.request(method, f"{base}/scim/v2{path}", headers=_SCIM, timeout=30, **kw)


def _patch(base: str, uid: str, *ops: dict[str, Any]) -> httpx.Response:
    return _scim(
        base, "PATCH", f"/Users/{uid}", json={"schemas": [_PATCH], "Operations": list(ops)}
    )


def _is_error(r: httpx.Response, status: int) -> None:
    assert r.status_code == status, r.text
    body = r.json()
    assert body["schemas"] == [_ERROR] and body["status"] == str(status) and body["detail"]


def test_okta_spec_test(scim_server) -> None:
    base, _, email, _, _ = scim_server
    okta = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "userName": email.capitalize(),  # kept as sent, matched in any case
        "name": {"givenName": "Lee", "familyName": "Okta"},
        "emails": [{"primary": True, "value": email, "type": "work"}],
        "password": "never-kept-1!",  # Okta sends one even without password sync
        "active": True,
    }
    created = _scim(base, "POST", "/Users", json=okta)
    assert created.status_code == 201, created.text
    user = created.json()
    assert user["userName"] == email.capitalize() and user["active"] is True and user["id"]
    _is_error(_scim(base, "POST", "/Users", json=okta), 409)

    listed = _scim(base, "GET", "/Users", params={"count": "1", "startIndex": "1"}).json()
    assert listed["schemas"] == [_LIST]
    assert all(isinstance(listed[k], int) for k in ("totalResults", "itemsPerPage", "startIndex"))
    first = listed["Resources"][0]
    assert first["id"] and first["userName"] and "active" in first and first["emails"][0]["value"]

    got = _scim(base, "GET", f"/Users/{user['id']}").json()
    assert got["id"] == user["id"]
    # Read back from ClickHouse, the times still say they are UTC.
    assert got["meta"]["created"].endswith("+00:00") and got["meta"]["lastModified"].endswith(
        "+00:00"
    )
    nobody = f'userName eq "nobody-{uuid.uuid4().hex}@corp.example"'
    empty = _scim(base, "GET", "/Users", params={"filter": nobody}).json()
    assert empty["totalResults"] == 0 and empty["Resources"] == []
    _is_error(_scim(base, "GET", f"/Users/{uuid.uuid4()}"), 404)
    upper = _scim(base, "GET", "/Users", params={"filter": f'userName eq "{email.upper()}"'})
    assert upper.status_code == 200 and upper.json()["Resources"][0]["id"] == user["id"]
    bad = httpx.get(f"{base}/scim/v2/Users", headers={"Authorization": "non-token"})
    _is_error(bad, 401)

    # Deactivated by a path-less replace (Okta OIN) or a full PUT (Okta AIW).
    off = _patch(base, user["id"], {"op": "replace", "value": {"active": False}})
    assert off.status_code == 200 and off.json()["active"] is False
    on = _scim(base, "PUT", f"/Users/{user['id']}", json={**okta, "active": True})
    assert on.status_code == 200 and on.json()["active"] is True
    assert _scim(base, "GET", "/Groups", params={"startIndex": "1", "count": "100"}).json() == {
        "schemas": [_LIST],
        "totalResults": 0,
        "startIndex": 1,
        "itemsPerPage": 0,
        "Resources": [],
    }


def test_entra_provisioning_and_validator(scim_server) -> None:
    base, _, email, _, _ = scim_server
    # The UPN, another address, in mixed case and not only ASCII.
    upn = f"Øyvind.UPN-{uuid.uuid4().hex[:6]}@Tenant.example"
    entra = {
        "schemas": [
            "urn:ietf:params:scim:schemas:core:2.0:User",
            "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User",
        ],
        "externalId": "lee",
        "userName": upn,
        "active": True,
        "emails": [{"primary": True, "type": "work", "value": email}],
        "meta": {"resourceType": "User"},
        "name": {"formatted": "Lee E", "familyName": "E", "givenName": "Lee"},
        "roles": [],
    }
    # Entra looks the user up by its matching attribute before it creates one.
    for match in (f'userName eq "{upn}"', f'emails[type eq "work"].value eq "{email}"'):
        found = _scim(base, "GET", "/Users", params={"filter": match}).json()
        assert found["totalResults"] == 0 and found["Resources"] == []
    created = _scim(base, "POST", "/Users", json=entra)
    assert created.status_code == 201, created.text
    uid = created.json()["id"]
    assert created.json()["userName"] == upn
    matches = (f'userName eq "{upn}"', f'userName eq "{upn.lower()}"')
    for match in (*matches, f'emails[type eq "work"].value eq "{email}"'):
        found = _scim(base, "GET", "/Users", params={"filter": match}).json()
        assert [u["id"] for u in found["Resources"]] == [uid], match
    _is_error(_scim(base, "POST", "/Users", json=entra), 409)

    # The default dialect: capitalised ops, one path each, `active` as a string.
    edits = _patch(
        base,
        uid,
        {"op": "Replace", "path": "name.givenName", "value": "Leigh"},
        {"op": "Add", "path": "nickName", "value": "L"},
        {"op": "Replace", "path": 'emails[type eq "work"].value', "value": email},
    )
    assert edits.status_code == 200, edits.text
    off = _patch(base, uid, {"op": "Replace", "path": "active", "value": "False"})
    assert off.status_code == 200 and off.json()["active"] is False
    assert _scim(base, "GET", f"/Users/{uid}").json()["active"] is False  # still listed
    # The aadOptscim062020 dialect: a path-less replace with dotted keys.
    on = _patch(
        base,
        uid,
        {"op": "replace", "value": {"displayName": "Lee", "name.givenName": "Lee", "active": True}},
    )
    assert on.status_code == 200 and on.json()["active"] is True
    renamed = _patch(base, uid, {"op": "Replace", "path": "userName", "value": f"new-{upn}"})
    assert renamed.json()["userName"] == f"new-{upn}"
    moved = _patch(
        base, uid, {"op": "Replace", "path": 'emails[type eq "work"].value', "value": f"x{email}"}
    )
    _is_error(moved, 400)  # another address is another person: refused

    assert _scim(base, "DELETE", f"/Users/{uid}").status_code == 204  # a hard delete
    _is_error(_scim(base, "GET", f"/Users/{uid}"), 404)
